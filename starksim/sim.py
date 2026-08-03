#!/usr/bin/env python3
"""
Discrete-event simulator of an EIP-8288 STARK mempool

Time in ms. Stdlib only. No crypto -- timing and bookkeeping. Pool objects
are defined in objects.py, the actors in actors.py; this file runs the
simulation: events, RNG, visibility, trace, metrics, CLI.

Model notes (interpretive choices):

- Networking: one link latency l. Anything newly created -- a tx broadcast by a user, a fold finished by an aggregator
  -- reaches every other actor l ms (plus a bit of jitter) after it is born; its creator sees it instantly. No
  topology: one hop, everyone at distance exactly l. l = 0 delivers every copy instantly.

- Fold time is r plus per-fold jitter: each fold takes r + U(0, r_jitter*r) ms, drawn independently per fold.

- Nothing is locked and nothing is deleted when folded. Every actor works on its own view of the one object store: an
  object is gone for a viewer once the viewer can see a fold that consumed it (a parent). Aggregators do not coordinate
  at all: an in-flight fold is invisible until its output's copy lands, so any aggregator deciding in that window can
  grab the same inputs. All folds run to completion (no aborts), so the pool can hold overlapping aggregates -- folds
  that raced to consume the same inputs and now cover some of the same txs -- and every repeat consumption of an
  object is counted as wasted work.

- Aggregators run one strategy: a fold is two uniform random draws over every object the aggregator can see and use. A
  raw tx, a bare sig and an aggregate are all just objects; whether a fold absorbs a fresh tx, merges two aggregates or
  pairs two raw txs falls out of the draw.

- Every arrival is one pool object: a privacy tx brings its own raw STARK, a PQ tx is a bare signature that needs to
  get aggregated.

- The builder does up to B of his own 2-to-1 folds per slot (default 5): he merges the up-to-(B+1) objects he has
  received (and not yet seen folded away) covering the most not-yet-included txs.

- An object with pending txs that have all been already included is considered dead -- discarded at the slot boundary
  (or at birth, for a fold that completes onto fully-included inputs). Nobody folds a dead object, the builder does not
  count it, and it leaves the store for good.
"""

import argparse
import heapq
import json
import math
import random

from starksim.actors import Aggregator, Builder
from starksim.objects import AggregatedSTARK, PQSig, PrivacyTx

SLOT_LENGTH_MS = 4000.0
EV_ARR, EV_FOLD, EV_SLOT, EV_VIS = 0, 1, 2, 3


class Simulation:
    def __init__(self, r_ms, n_agg, lam_priv, lam_pq, slots_n, seed,
                 trace=None, arrivals=None, paranoid=False,
                 slot_length=SLOT_LENGTH_MS, b_folds=5, l_ms=0.0,
                 r_jitter=0.0):
        # lam_priv / lam_pq are in txs per SECOND (converted to per-ms inside).
        # The constructor keeps the v1 defaults (l_ms=0, r_jitter=0); the
        # CLI opts into networking (--l 100) and jittered fold times
        # (--r-jitter 0.1).
        if r_jitter < 0:
            raise ValueError("r_jitter must be >= 0")
        if b_folds < 1:
            raise ValueError("b_folds must be >= 1: a lone bare PQ sig "
                             "needs its one sig-proving fold")
        self.r = float(r_ms)
        self.r_jitter = float(r_jitter)      # fold = r + U(0, r_jitter*r)
        self.l = float(l_ms)                 # one-hop latency; 0 = v1 pool
        self.slot_length = float(slot_length)
        self.n = int(n_agg)
        self.lam_priv = float(lam_priv)
        self.lam_pq = float(lam_pq)
        self.slots_n = int(slots_n)
        self.seed = seed
        self.b = int(b_folds)
        self.trace = trace
        self.paranoid = paranoid
        self.rng_priv = random.Random(f"{seed}:priv")
        self.rng_pq = random.Random(f"{seed}:pq")
        self.rng_sel = random.Random(f"{seed}:sel")   # random object draws
        self.rng_fold = random.Random(f"{seed}:fold")  # fold-duration jitter

        self.events = []
        self.seq = 0
        self.next_obj = 0
        self.aggs = [Aggregator(i) for i in range(self.n)]
        self.idle = set(range(self.n))
        self.builder = Builder(self.b)

        self.pool = {}                       # id -> object; an object leaves
                                             # once no actor still sees it
        self.singleton_ids = []              # raw txs, for the random draw
                                             # (lazily compacted)
        self.aggregate_ids = []              # folded objects, for the
                                             # random draw (same lazy
                                             # compaction)
        self.pending = 0                     # arrived-but-not-included txs

        self.arrived_txs = 0
        self.n_included = 0
        self.delay_sum = 0.0
        self.folds_started = 0
        self.wasted_consumptions = 0         # fold inputs that were repeats
        self.blocks = []                     # one Block per slot boundary
        self.done = False

        if arrivals is None:
            self.auto = True
            if self.lam_priv > 0:
                self._push(self.rng_priv.expovariate(self.lam_priv / 1000.0), EV_ARR, 0)
            if self.lam_pq > 0:
                self._push(self.rng_pq.expovariate(self.lam_pq / 1000.0), EV_ARR, 1)
        else:
            self.auto = False
            for t, kind in arrivals:
                self._push(float(t), EV_ARR, 0 if kind == "priv" else 1)
        self._push(self.slot_length, EV_SLOT, 1)
        self._tr({"e": "cfg", "r": self.r, "r_jitter": self.r_jitter,
                  "l": self.l, "n": self.n,
                  "slot_length": self.slot_length, "b": self.b,
                  "lam_priv": self.lam_priv, "lam_pq": self.lam_pq,
                  "slots_n": self.slots_n, "seed": str(seed)})

    # -- plumbing ---------------------------------------------------------

    def _push(self, t, code, payload):
        heapq.heappush(self.events, (t, self.seq, code, payload))
        self.seq += 1

    def _tr(self, obj):
        if self.trace is not None:
            self.trace.write(json.dumps(obj, separators=(",", ":")) + "\n")

    # -- visibility: who can see what, when ---------------------------
    #
    # One hop: anything born at time t is visible to its creator at t and
    # to every other actor when its copy lands, t + l. These are pure
    # timestamp tests, so "the pool as aggregator X sees it" is just a
    # filter over the one object store.

    def visible(self, obj, viewer, now):
        return obj.creator == viewer or obj.born + self.l <= now

    def superseded(self, obj, viewer, now):
        # gone for this viewer: it already sees a fold that consumed obj
        return any(self.visible(p, viewer, now) for p in obj.parents)

    def _available_to(self, obj, agg_id, now):
        return (self.visible(obj, agg_id, now)
                and not self.superseded(obj, agg_id, now))

    # -- the pool (one object store, per-actor views) ----------------------

    def _add_pool(self, obj):
        self.pool[obj.id] = obj
        if obj.total == 1:
            self.singleton_ids.append(obj.id)
        else:
            self.aggregate_ids.append(obj.id)

    def _discard(self, obj):
        # Dead: covers no pending txs any more. Nobody may fold it or
        # propagate it; it leaves the store (and the builder's counts)
        # for good.
        self.pool.pop(obj.id, None)
        self.builder.forget(obj)

    def _publish(self, t, obj):
        # The object's one hop completes for everyone at once (immediately
        # so at l = 0). The builder can pick it (unless it died first),
        # and the fold that created it (if any) is now globally known --
        # its inputs are seen folded away by every actor and leave the
        # store for good.
        if obj.id in self.pool:
            self.builder.sees(obj)
        if obj.kind == "a":
            for child in obj.children:
                self._discard(child)
        self._try_folds(t)

    def _pick_pair(self, agg_id, now):
        # Two uniform random draws over everything this aggregator can
        # see and use, kind-blind (see the module docstring: both the
        # uniformity and the kind-blindness are load-bearing).
        base = self._random_any(agg_id, now, None)
        second = self._random_any(agg_id, now, base)
        if base is None or second is None:
            return None
        return base, second

    def _random_any(self, agg_id, now, exclude):
        # One kind-blind uniform draw: pick a side proportionally to its
        # share of the store, then draw inside it; a miss re-rolls the
        # side so a stale-heavy list cannot starve the other one.
        for _ in range(4):
            n_singletons = len(self.singleton_ids)
            n_aggregates = len(self.aggregate_ids)
            if not n_singletons + n_aggregates:
                return None
            if self.rng_sel.randrange(n_singletons + n_aggregates) < n_singletons:
                obj = self._random_from(self.singleton_ids, agg_id, now, exclude)
            else:
                obj = self._random_from(self.aggregate_ids, agg_id, now, exclude)
            if obj is not None:
                return obj
        obj = self._random_from(self.singleton_ids, agg_id, now, exclude)
        if obj is None:
            obj = self._random_from(self.aggregate_ids, agg_id, now, exclude)
        return obj

    def _random_from(self, object_ids, agg_id, now, exclude):
        # Uniform draw over the listed objects this aggregator can see
        # and use, by rejection sampling on the lazily compacted id list.
        # Entries that left the store are dropped for good; a per-viewer
        # miss (copy not yet delivered, superseded for this viewer, or
        # the other slot's object) just re-draws. When the draws keep
        # missing -- near-empty pool -- fall back to a scan, then to the
        # caller's deterministic candidate (None here).
        probes = 0
        while object_ids and probes < 32:
            i = self.rng_sel.randrange(len(object_ids))
            obj = self.pool.get(object_ids[i])
            if obj is None:
                object_ids[i] = object_ids[-1]
                object_ids.pop()
                continue
            probes += 1
            if obj is not exclude and self._available_to(obj, agg_id, now):
                return obj
        for oid in object_ids:
            obj = self.pool.get(oid)
            if (obj is not None and obj is not exclude
                    and self._available_to(obj, agg_id, now)):
                return obj
        return None

    # -- aggregators ------------------------------------------------------

    def _try_folds(self, t):
        for agg_id in sorted(self.idle):
            self._try_fold_one(agg_id, t)

    def _try_fold_one(self, agg_id, t):
        if agg_id not in self.idle:
            return
        pair = self._pick_pair(agg_id, t)
        if pair is None:
            return
        a, b = pair
        self.idle.discard(agg_id)
        self.aggs[agg_id].begin(a, b)
        self.folds_started += 1
        for x in (a, b):
            # consumption (and waste) is booked at fold start -- the work
            # is spent either way
            if x.consumed:
                self.wasted_consumptions += 1
            x.consumed += 1
        # per-fold duration draw -- with a shared deterministic r the whole
        # fleet completes folds at the same instants
        duration = self.r + self.rng_fold.uniform(0.0, self.r_jitter * self.r)
        self._push(t + duration, EV_FOLD, agg_id)
        if self.trace is not None:
            # lane labels show useful txs, not `total`: folding two
            # overlapping aggregates (racing folds that consumed the same
            # inputs) keeps total additive (re-proved overlap), so it
            # explodes
            self._tr({"e": "fs", "t": round(t, 2), "d": round(duration, 2),
                      "agg": agg_id, "a": a.id, "b": b.id,
                      "as": a.useful_count(), "bs": b.useful_count()})

    def _fold_done(self, t, agg_id):
        a, b = self.aggs[agg_id].finish()
        obj = AggregatedSTARK(self.next_obj, a, b, t, agg_id)
        self.next_obj += 1
        self._add_pool(obj)
        self.idle.add(agg_id)
        self._tr({"e": "fd", "t": round(t, 2), "agg": agg_id, "item": obj.id,
                  "size": len(obj.pending), "tot": obj.total, "depth": obj.depth,
                  "a": a.id, "b": b.id})
        if not obj.pending:
            # born dead: everything it covers was already included --
            # while the fold was in flight, every tx its inputs covered
            # got included through other objects covering the same txs.
            # Discarded on the spot; its copy still travels so every
            # actor drops the consumed inputs.
            self._discard(obj)
            self._tr({"e": "dead", "t": round(t, 2), "items": [obj.id]})
        if self.l == 0:
            self._publish(t, obj)
            return
        self._try_folds(t)                     # the creator sees it now,
        self._push(t + self.l, EV_VIS, obj)    # everyone else one hop later

    # -- arrivals ---------------------------------------------------------

    def _arrival(self, t, kind):
        if kind:
            obj = PQSig(self.next_obj, t)
        else:
            obj = PrivacyTx(self.next_obj, t)
        self.next_obj += 1
        self.arrived_txs += 1
        self.pending += 1
        self._add_pool(obj)
        self._tr({"e": "arr", "t": round(t, 2), "kind": "pq" if kind else "priv",
                  "item": obj.id, "size": 1})
        if self.l == 0:
            self._publish(t, obj)
        else:
            self._push(t + self.l, EV_VIS, obj)    # nobody sees it before
        if self.auto:
            rng = self.rng_pq if kind else self.rng_priv
            lam = self.lam_pq if kind else self.lam_priv
            self._push(t + rng.expovariate(lam / 1000.0), EV_ARR, kind)

    # -- builder at slot boundary ----------------------------------------

    def _slot(self, t, k):
        blk = self.builder.build(self.pool, self.slot_length, t, k)
        if blk.object_ids:
            self._tr({"e": "inc", "t": round(t, 2), "items": blk.object_ids,
                      "txs": blk.n_priv + blk.n_pq, "folds": blk.folds_used})
        # ghost decay: this block also killed txs inside objects it did
        # NOT pick (racing folds consumed the same inputs, so unpicked
        # objects can cover some of the same txs as picked ones). An object
        # left covering nothing -- picked or ghosted -- is dead and is
        # discarded; the survivors whose useful count changed are traced
        # so the viewer can hollow out the ghosted share.
        dead = []
        sizes = {}
        for obj in self.pool.values():
            if obj.parents:
                continue    # a completed fold consumed it; the viewer
                            # already dropped it at that fold's "fd"
            useful = obj.useful_count()
            if useful == 0:
                dead.append(obj)
            elif useful != obj.traced_size:
                obj.traced_size = useful
                sizes[obj.id] = useful
        for obj in dead:
            self._discard(obj)
        if sizes:
            self._tr({"e": "upd", "t": round(t, 2), "sizes": sizes})
        if dead:
            self._tr({"e": "dead", "t": round(t, 2),
                      "items": [obj.id for obj in dead]})
        for d in blk.delays:                 # per-tx, in order: the float
            self.delay_sum += d              # addition order is traced
        self.pending -= len(blk.delays)      # included txs are no longer
                                             # pending
        self.n_included += len(blk.delays)
        self.blocks.append(blk)
        dm = self.delay_sum / self.n_included if self.n_included else 0.0
        self._tr({"e": "slot", "t": round(t, 2), "slot": k,
                  "items": blk.pool_objects, "pending": self.pending,
                  "inc_priv": blk.n_priv, "inc_pq": blk.n_pq,
                  "delay_mean": round(dm, 3)})

        if self.paranoid:
            # every un-included tx is covered by at least one root (an
            # object nobody has folded yet), and counted exactly once
            roots = [o for o in self.pool.values() if not o.parents]
            covered = set()
            for obj in roots:
                for tx in obj.pending:
                    if not tx.included:
                        covered.add(tx.id)
            assert self.pending == len(covered)
            assert self.builder.effective_objects == sum(
                1 for o in self.pool.values() if o.covering)
            assert all(o.consumed >= len(o.parents)
                       for o in self.pool.values())

        self._try_folds(t)
        if k < self.slots_n:
            self._push(t + self.slot_length, EV_SLOT, k + 1)
        else:
            self.done = True

    # -- driver / metrics -------------------------------------------------

    def run(self):
        ev = self.events
        while ev and not self.done:
            t, _, code, payload = heapq.heappop(ev)
            if code == EV_ARR:
                self._arrival(t, payload)
            elif code == EV_FOLD:
                self._fold_done(t, payload)
            elif code == EV_VIS:
                self._publish(t, payload)
            else:
                self._slot(t, payload)
        return self.metrics()

    def metrics(self):
        n_inc = self.n_included
        if n_inc:
            dmean = self.delay_sum / n_inc
            all_delays = []
            for blk in self.blocks:
                all_delays.extend(blk.delays)
            all_delays.sort()
            p95 = all_delays[max(0, math.ceil(0.95 * n_inc) - 1)]
        else:
            dmean = p95 = float("nan")
        fw = sum(blk.folds_8288 for blk in self.blocks) / self.slots_n
        fb = sum(blk.folds_baseline for blk in self.blocks) / self.slots_n
        if self.folds_started:
            waste_frac = self.wasted_consumptions / (2.0 * self.folds_started)
        else:
            waste_frac = 0.0
        return {
            "r_ms": int(self.r), "r_jitter_frac": self.r_jitter,
            "l_ms": int(self.l), "n_agg": self.n,
            "b_folds": self.b,
            "lam_priv_tps": self.lam_priv, "lam_pq_tps": self.lam_pq,
            "slots_n": self.slots_n, "seed": self.seed,
            "arrived_txs": self.arrived_txs, "included_txs": n_inc,
            "priv_per_block": sum(blk.n_priv for blk in self.blocks) / self.slots_n,
            "pq_per_block": sum(blk.n_pq for blk in self.blocks) / self.slots_n,
            "delay_mean_slots": dmean, "delay_p95_slots": p95,
            "builder_folds_8288": fw, "builder_folds_baseline": fb,
            "builder_folds_saved": fb - fw,
            "builder_folds_used": sum(blk.folds_used for blk in self.blocks) / self.slots_n,
            "agg_folds_total": self.folds_started,
            "wasted_consumptions": self.wasted_consumptions,
            "waste_frac": waste_frac,
            "end_pool_items": len(self.pool), "end_pending_txs": self.pending,
        }


CSV_COLUMNS = [
    "r_ms", "r_jitter_frac", "l_ms", "n_agg", "b_folds",
    "lam_priv_tps", "lam_pq_tps", "slots_n", "seed",
    "arrived_txs", "included_txs", "priv_per_block", "pq_per_block",
    "delay_mean_slots", "delay_p95_slots",
    "builder_folds_8288", "builder_folds_baseline", "builder_folds_saved",
    "builder_folds_used", "agg_folds_total", "wasted_consumptions",
    "waste_frac", "end_pool_items", "end_pending_txs",
]


def csv_header():
    return ",".join(CSV_COLUMNS)


def csv_row(m):
    out = []
    for c in CSV_COLUMNS:
        v = m[c]
        if isinstance(v, float):
            out.append("nan" if math.isnan(v) else "%.6f" % v)
        else:
            out.append(str(v))
    return ",".join(out)


def main():
    ap = argparse.ArgumentParser(description="EIP-8288 mempool sim (one run)")
    ap.add_argument("--r", type=float, default=1500.0, help="fold time in ms")
    ap.add_argument("--r-jitter", type=float, default=0.1,
                    help="per-fold time jitter as a fraction of r: each fold "
                         "takes r + U(0, frac*r), drawn independently per "
                         "fold (default 0.1; 0 = every fold exactly r)")
    ap.add_argument("--l", type=float, default=100.0,
                    help="one-hop link latency in ms (default 100; "
                         "0 = v1 instant global pool)")
    ap.add_argument("--n", type=int, default=100, help="number of aggregators")
    ap.add_argument("--lam-priv", type=float, default=3.0, help="privacy txs/s")
    ap.add_argument("--lam-pq", type=float, default=15.0, help="pq txs/s")
    ap.add_argument("--slots-n", type=int, default=1000,
                    help="run length in slots")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--b", type=int, default=5,
                    help="builder fold budget B per slot (default 5)")
    ap.add_argument("--slot-length", type=float, default=SLOT_LENGTH_MS,
                    help="slot length in ms (default 4000)")
    ap.add_argument("--trace", type=str, default=None, help="write trace.jsonl here")
    ap.add_argument("--csv", action="store_true", help="print CSV header+row")
    args = ap.parse_args()

    tf = open(args.trace, "w", buffering=1) if args.trace else None
    sim = Simulation(args.r, args.n, args.lam_priv, args.lam_pq, args.slots_n,
                     args.seed, trace=tf, slot_length=args.slot_length,
                     b_folds=args.b, l_ms=args.l, r_jitter=args.r_jitter)
    m = sim.run()
    if tf:
        tf.close()
    if args.csv:
        print(csv_header())
        print(csv_row(m))
    else:
        for k, v in m.items():
            print(f"{k}: {v}")


if __name__ == "__main__":
    main()
