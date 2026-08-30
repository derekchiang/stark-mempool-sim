> Historical record, not a specification. Current material: see [docs/model.md](../model.md).

# The simulation model

> **Status: retired.** The code this describes was deleted at phase 6; v2 replaced
> it. The document is kept deliberately, because it is the clearest statement of
> the assumption v2 exists to remove — that visibility is a formula, `born + l`,
> rather than something each node has to be told. §3 is that assumption written
> down, and §9's list of non-goals is roughly the specification v2 was built
> against.
>
> What carried over: the tx-level metric definitions, so published v1 numbers stay
> interpretable. What did not: the mechanism, all of it. For v2 see
> [model.md](../model.md) and Part I of
> [networking-plan.md](networking-plan.md).
>
> **What `l` was hiding, now that v2 has measured it.** v1's `born + l` was not
> a neutral simplification of a network — it was the *most generous relay
> policy imaginable*: every object, a 263 kB privacy witness and a 256 kB
> aggregate alike, teleported to every actor for zero bytes after one uniform
> delay. Everything v2 has since found decision-relevant lived inside that
> constant:
>
> - **Which objects travel at all is a policy, and the policies differ by a
>   third of all traffic.** v2's relay corners — flood the raw witness
>   everywhere, or never ship it and let claims spread by re-aggregation
>   (`wrapper_only`) — bound throughput very differently under load, because a
>   witness is ~80× a transaction's wrapper footprint. v1 could not state the
>   question: under `born + l` the witness and the claim were equally free.
> - **The dominant cost is invisible under `l`.** One recursive proof per peer
>   per tick — 73–98% of all bytes in v2's measurements — is a *per-recipient,
>   per-interval* cost. A model where publication reaches everyone at once has
>   no per-recipient anything, so the wrapper constant, the single largest
>   term in the EIP's bandwidth budget, did not exist here.
> - **Throughput's ceiling is per-node bandwidth ÷ per-tx bytes, independent
>   of node count** — a statement about replication cost that cannot even be
>   phrased when visibility is free. v1's over-capacity runs (45 privacy tx/s
>   ≈ 11 MB/s of witnesses against what would be 1.25 MB/s uplinks) were
>   physically impossible and nothing in the model could notice.
> - **Arity was free here, priced in v2.** v1's published fan-out sweep
>   (`arity-sweep.html`, published to the upstream `gh-pages` branch and not in
  this tree) ran 4-to-1 and 8-to-1 folds at
>   the *same flat r as a 2-to-1* — "the fold costs the full r either way" —
>   and its headline (mean delay 5.68 → 2.05 → 1.34 slots at r = 1500 ms as
>   fan-out rises) was carried entirely by that assumption. Measured fold
>   times (2-to-1: 889 ms, 4-to-1: 1664, 8-to-1: 3824 on one machine; ratios
>   1 : 1.87 : 4.30) say cost is roughly *linear in the input count*: v1's
>   8-to-1 rows were ~4× underpriced, yet fan-out still genuinely wins —
>   an 8-ary fold costs ~40% less than the pairwise tree it replaces — so
>   v1's direction survives at a fraction of its claimed magnitude. Wider
>   folds also cost prover memory, which is why the priced benches cap
>   fan-in at 8 (`fold_plan="capped8"`). v2 prices a fold job by what it
>   actually folds (`prove_cost_basis="inputs"`, m/2 × the 1500 ms pairwise
>   anchor), so the plan-shape comparison pays for its arity instead of
>   assuming it away.
> - **Death was global here, per-node in reality.** `_publish` discarded a
>   fold's children from the one store for everyone at once; in v2 an object
>   leaves each node's view separately, by three counted exits (superseded,
>   pruned, evicted), and residency-vs-reach — a distinction v1 could not
>   express — is a headline instrument.
>
> [results.md](../results.md) carries the measurements;
> the point of keeping this note *here* is that every one of them is an
> answer to a question `born + l` made unaskable.

Everything here is an interpretive choice on top of
[EIP-8288](../eip-8288.md) — the EIP specifies formats and validity rules, not
timing. The module docstring of `starksim/sim.py` (deleted at phase 6; see git history) is the short version; this is
the long one.

Units: **milliseconds** throughout, as floats. Delays are reported in slots.

## 1. Time and the event loop

The engine is a plain discrete-event loop over a binary heap of
`(t, seq, code, payload)` tuples (`Simulation._push`, `Simulation.run`). There
are four event codes:

| code | when it fires | handler |
| --- | --- | --- |
| `EV_ARR` | a user broadcasts a tx | `_arrival` |
| `EV_FOLD` | an aggregator's fold completes | `_fold_done` |
| `EV_VIS` | an object's one network hop completes | `_publish` |
| `EV_SLOT` | a slot boundary; the builder builds | `_slot` |

`seq` is a monotonic insertion counter, so **equal timestamps are processed in
enqueue order**. The practical consequence is that a slot boundary beats a fold
that completes at the very same instant: the boundary event was enqueued a full
slot earlier, so it carries the smaller `seq`. The builder therefore does *not*
see an aggregate that finishes exactly on the boundary (this is pinned by
`test_boundary_processed_before_same_time_fold_done`). The one exception is a
fold started exactly at a boundary whose duration equals `slot_length` exactly —
`_slot` calls `_try_folds` before enqueueing the next boundary — which cannot
happen with the default `r < slot_length`.

The run stops when the boundary for slot `slots_n` has been processed. Folds in
flight and arrivals already scheduled past that point are simply dropped, so
`arrived_txs` counts arrivals up to the last boundary.

## 2. Arrivals

Two independent Poisson processes, one per kind, with rates `lam_priv` and
`lam_pq` given in **txs per second** and converted to per-ms inside. Each arrival
schedules the next one with `rng.expovariate(lam / 1000.0)`; a rate of `0`
disables that stream entirely.

Each arriving tx *is* a pool object covering exactly itself:

- `PrivacyTx` (`kind == "p"`) — a privacy app's tx that already carries its own
  raw STARK. It is includable as-is: the builder needs no fold to put a single one
  in a block.
- `PQSig` (`kind == "q"`) — a bare post-quantum signature. It still needs one
  proving fold before it can go into a block's recursive STARK, so a lone PQ tx
  costs the builder one fold.

Tests can replace the Poisson processes with an explicit schedule:
`Simulation(..., arrivals=[(100.0, "priv"), (150.0, "pq")])`.

### RNG streams

Four independent `random.Random` instances, seeded with string keys derived from
`seed`: `"{seed}:priv"`, `":pq"`, `":sel"` (object draws), `":fold"` (duration
jitter). Because the arrival streams are separate, **the arrival sequence is a
function of `(seed, lam)` alone** — changing `N`, `B`, `l` or `r` does not
reshuffle the offered load, which is what makes parameter sweeps comparable. Runs
are bit-reproducible: same seed ⇒ byte-identical CSV row (`TestDeterminism`).

## 3. Networking: one hop, no topology

There is a single link latency `l`. Anything newly born — a tx broadcast by a
user, a fold finished by an aggregator — becomes visible to its creator
**immediately** and to **every** other actor exactly `l` ms later. There is no
topology, no bandwidth, no gossip fan-out: everyone sits at distance exactly `l`.
`l = 0` collapses this to one instantly-shared global pool.

Visibility is not stored, it is *computed* from timestamps, so "the pool as
aggregator X sees it at time t" is just a filter over the one object store:

```python
visible(obj, viewer, now)    = obj.creator == viewer or obj.born + l <= now
superseded(obj, viewer, now) = any(visible(parent, viewer, now) for parent in obj.parents)
available_to(obj, agg, now)  = visible(...) and not superseded(...)
```

An arrival's creator is the sentinel `USER = -1`, which matches no aggregator id,
so nobody — not even the builder — sees a fresh tx before its hop completes.

**Supersession** is the model's notion of "already folded away": an object leaves
a viewer's view once that viewer can see a fold that consumed it. The creator of
that fold drops the inputs at once; everyone else `l` later, which is exactly when
`_publish` discards the children from the store for good.

### How this compares to EIP-8288 propagation

In the EIP, a transaction's **content** and its **validity evidence** travel on
different schedules, and only the evidence is aggregated. The unit on the wire is
the **mempool wrapper** — not a valid transaction on its own
([eip-8288.md:214](../eip-8288.md), `:246`):

```text
[transactions, mode, content]
transactions = frame transactions (or their hashes if already broadcast)
mode         = 0 (direct dependencies) or 1 (recursive STARK)
content      = mode 0: [deps, proofs]   |   mode 1: [deps, recursive_stark]
```

- **`transactions`** — the bodies. The 96-byte `(scheme, data_hash,
  verification_key)` triples ride *inside* the body, in the `data` of its
  `DEP_VERIFY_FRAME_MODE` frames, so the declaration of what needs proving is part
  of the tx and ends up on chain.
- **`deps`** — the same triples concatenated across the wrapper's txs; redundant
  with the bodies (receivers must check that, `:230`) but it is what the proof's
  `deps_hash` commits to.
- **`proofs` / `recursive_stark`** — the witnesses: one raw signature or STARK per
  dependency in mode 0, or a single recursive proof covering all of them in mode 1.

The flow: users broadcast **mode 0** with full bodies and raw witnesses — the only
point where witnesses hit the wire (`:244`). A receiving node validates them, then
every `AGGREGATION_INTERVAL` (1000 ms) emits **one mode 1 wrapper covering all its
still-valid transactions**, forwarding a single recursive STARK instead of the
witnesses it verified (`:256`). Bodies it has already sent are replaced by their
hashes, so **content crosses each link once and is referenced by hash thereafter**;
the recurring cost is the deps list plus one proof, which is what makes the
overhead constant per peer (`:324`, ~256 kB per interval toward the builder,
`:394`). Witnesses never land on chain at all — the block header carries only
`[stark_proof, block_deps_hash]`.

The sim models none of that machinery:

| EIP-8288 | this sim |
| --- | --- |
| wrapper carries bodies + `deps` + proof(s) | an object carries a coverage set; no bodies, no hashes, no wrapper |
| one wrapper per node per `AGGREGATION_INTERVAL`, covering **all** still-valid txs | one publish per **completed fold**, covering the two objects that were drawn |
| bodies sent once, then referenced by hash | no dedup; every object is a flat `l` ms delay |
| mode 0 (witnesses) vs mode 1 (recursive proof) | no distinction; a fold output is the only thing that propagates |
| witnesses verified on receipt, then discarded | receiving and checking is free (§9) |

The structural mismatch that matters most for reading the results: an
EIP-conformant node re-aggregates **its whole current view** once per tick, with a
suggested priority of "my previous aggregate → removals → other nodes' aggregates
by new-tx count → users' txs by fee" (`:258`). That yields roughly one wide
aggregate per node per tick. The sim's uniform-random 2-to-1 pairing (§5) is a much
weaker discipline, on purpose — it is the no-coordination baseline — so its fold
counts and `waste_frac` are not estimates of what a conformant node would do.

The EIP also leaves a gap here: it never specifies how a node that receives only a
*hash* obtains the body (no announce/request protocol, no timeout, no per-peer
"already sent" bookkeeping), so in practice that falls to devp2p.

## 4. The pool: one store, per-actor views

`Simulation.pool` is a single `id -> object` dict. Nothing is ever locked and
nothing is deleted at fold *start*; objects leave only when they are dead
(§6) or superseded for everybody (§3). For the random draw the ids are kept in
two lists — `singleton_ids` (`total == 1`) and `aggregate_ids` (everything
folded) — compacted lazily as stale entries are hit.

## 5. Aggregators

An `Aggregator` runs exactly one strategy, and it is deliberately the dumbest
possible one:

```text
loop forever:
    publish(fold(random object, random object))
```

Both draws are **uniform** and **kind-blind** over everything this aggregator can
see and use. Whether a fold absorbs a fresh tx, merges two deep aggregates or
pairs two raw signatures falls out of the draw; there is no size heuristic, no
pairing rule, no coordination. Both properties are load-bearing: this is the
no-coordination baseline the numbers are meant to characterise.

Mechanically (`_pick_pair` → `_random_any` → `_random_from`), a draw picks a side
proportionally to that side's share of the store, then rejection-samples inside it
(up to 32 probes), re-rolling the side up to 4 times so a stale-heavy list cannot
starve the other one; if the draws keep missing — a near-empty pool — it falls
back to a linear scan. It is therefore *approximately* uniform: the side weights
count not-yet-compacted stale ids, and a per-viewer miss (copy not yet delivered,
or superseded for this viewer) re-draws. The second draw excludes the first
object, so no object is folded with itself.

Each fold takes `r + U(0, r_jitter · r)` ms, drawn independently per fold. With
`r_jitter = 0` the whole fleet completes folds at the same instants, which is
useful for constructing races but not realistic; the CLI defaults to `0.1`.

An aggregator can hold one fold at a time (`N` aggregators ⇒ up to `N` folds in
flight) and is offered a new pair the moment anything could have changed:
`_try_folds` runs after every publish, every fold completion and every slot
boundary. Idle aggregators are polled in ascending id order, so with an almost
empty pool the low ids get first pick.

### Races and wasted work

A fold in flight is invisible to everyone, including its own creator, until it
completes. So for a window of roughly `r + l` its inputs still look fresh and
usable to every other aggregator, and any of them may grab the same pair. Nothing
aborts: **all folds run to completion**, and the pool can end up holding
*overlapping aggregates* — objects that cover some of the same transactions.

Waste is booked at fold **start**, because the proving time is spent either way:
each input's `consumed` counter is bumped, and every consumption after the first
increments `wasted_consumptions`. `waste_frac` divides that by the total number
of fold inputs (`2 · folds_started`).

### Overlapping aggregates: union coverage, additive total

`AggregatedSTARK.__init__` keeps two different sizes, and the distinction matters:

- `pending` — the **union** of the inputs' still-pending txs, bigger side first,
  each tx kept once, already-included txs dropped. This is real coverage: what the
  object can still get included.
- `total` — the plain **sum** of the inputs' totals, i.e. the number of leaves in
  the fold tree. It measures *embodied proving work*: a raced fold really did
  re-prove the overlap, so the number stays additive on purpose. It therefore
  doubles whenever two equal-sized aggregates merge (≈ `2^depth` for a balanced
  tree) and, once overlapping aggregates get merged, exceeds the number of distinct
  txs covered. It is **not** a proof size — a recursive STARK is succinct — which
  is why the viewer labels lanes with useful counts and never draws `total`.

A `pending` list is treated as **immutable once built**: the constructor always
builds a fresh one instead of aliasing an input's, and `mark_included()` rebinds to
a new empty list rather than clearing in place. Starting a fold neither locks nor
removes its inputs, so the same object is still read afterwards at other simulated
times — the builder may select it and walk its `pending` while the fold is in
flight, and the fold's own constructor (plus any racing fold on the same input)
reads it at *completion* time, `r` ms after the pair was picked. That last read is
how a **born-dead** aggregate arises (§6): an intervening block emptied the input.

The discipline is defensive rather than a fix for a reachable bug — the loop is
single-threaded and no reader caches a bare list reference. What actually
guarantees correctness is `tx.included`: every reader re-filters on it, so a stale
candidate list can never resurrect an included tx.

## 6. Death and discard

An object is **dead** when it covers no pending tx any more — every tx it covers
has already been included, whether through this object or through some other
object covering the same txs. `useful_count()` is the live measure (it skips
`tx.included`, and optionally an exclusion set).

Dead objects leave the store for good (`_discard`): nobody folds them, nobody
propagates them, and the builder drops them from its counts. Two places detect
death:

- `_fold_done` — **born dead**: while the fold was in flight, everything its
  inputs covered got included elsewhere. It is discarded on the spot, but its copy
  still travels, so every actor learns to drop the consumed inputs.
- `_slot` — the **ghost sweep** after a block. The block also killed txs inside
  objects it did *not* pick (overlapping aggregates again). Root objects — those
  with no parents, since objects with parents leave at the parent's publish — are
  re-counted: those at zero are discarded, and those whose count merely shrank are
  traced in an `upd` event so the viewer can hollow out the ghosted share.

Inclusion itself is idempotent per tx: `mark_included()` marks only the
not-yet-included txs and returns them, so a tx covered by several objects is
counted — and its delay recorded — exactly once, at its first covering pick.

### Objects die, transactions do not

Death is an *object*-level event only. At tx level there is no drop path at all:
`_discard` fires only on a dead object, and dead means every tx it covered is
already `included`. Coverage also always reaches the parent before the children
go, because `_publish` discards a fold's inputs only once that fold's own object
is globally visible, and the parent's `pending` is the union of its inputs'
still-pending txs. So an un-included tx can never be orphaned — which is exactly
the invariant the paranoid assert states (§8): every un-included tx is covered by
at least one root object, counted once.

The consequence is that transactions are **conserved**:

```text
arrived_txs == included_txs + end_pending_txs
```

Object-level drops without inclusion, by contrast, are routine and intended: the
long `dead` lists in a trace are duplicate aggregates from races, ghosts whose
coverage was included through a twin, and born-dead folds. The proving work is
still counted in `wasted_consumptions`; only the redundant object disappears.

### Transactions that are never included

Inclusion is the *only* exit from this mempool. There is no TTL or expiry, no
fee-based eviction, no pool size cap, no replacement-by-nonce, and no same-sender
invalidation — notable because same-sender inclusion is precisely what populates
the EIP's `discards` set. So a tx that is not included simply stays, and two
mechanisms keep it there:

1. **Capacity.** `B+1` picks per slot bound what a block can absorb; the rest
   queues, and with no pool cap the backlog grows monotonically once the offered
   load exceeds what the builder can sweep. `end_pool_items` / `end_pending_txs`
   are the diagnostic.
2. **Starvation by priority.** The builder maximises tx *count*, with no age or
   fee term. Its heap key `(-count, oid)` breaks ties toward lower ids, so among
   *equal* coverage the older object wins — but a 1-tx object loses to every larger
   object, every slot, indefinitely.

Measured over 200-slot runs (`paranoid=True`, defaults otherwise), the second
mechanism is not hypothetical:

| load | arrived | included | end_pending | mean delay | p95 | oldest still pending |
| --- | --- | --- | --- | --- | --- | --- |
| under (`N=100`, 2.5 tx/s) | 2041 | 2041 | 0 | 1.00 | 1.81 | — |
| moderate (CLI defaults) | 14422 | 14058 | 364 | 5.69 | 10.08 | 10.6 slots |
| over (`N=10`, 45 tx/s) | 35962 | 3508 | 32454 | 62.70 | 151.92 | **200.0 slots** |

In the overloaded run a tx that arrived in the first slot was still pending 200
slots later, while 3508 others were included around it. The heavy tail in
`delay_p95_slots` is the same effect seen from the included side, so a run whose
`end_pending_txs` is large should not be read as "delays were X" — it should be
read as "delays were X *for the txs that made it*".

Two truncation effects at the end of a run, for completeness: each arrival stream
keeps exactly one successor event queued, so at most one arrival per stream is
discarded uncounted, and up to `N` folds are in flight when the loop stops —
counted in `agg_folds_total` but never producing an object.

## 7. The builder

One builder, no builder competition. It keeps **its own view** of the store,
maintained by the sim: `sees(obj)` on every delivery, `forget(obj)` on every
removal. It is exactly as `l`-blind as everyone else — anything born within the
last `l` ms is not in the block.

At each boundary (`Builder.build`) it does:

```text
repeat up to B+1 times:
    pick the object covering the most pending txs that
    this slot's earlier picks do not already cover
fold the picks 2-to-1 into the block's single STARK
```

That is greedy maximum coverage, implemented as a **lazy greedy** over a max-heap
keyed by `-useful_count` at push time (`_get_best_objects_for_block`). A popped
entry whose key is stale (its true marginal count has dropped, because of
inclusions since, or because this slot's earlier picks already cover those txs) is
re-pushed with the corrected key instead of being trusted; entries that reach
marginal zero, or that are no longer `covering`, are dropped for good — they are
about to be swept as dead anyway.

`B+1` picks cost `B` folds (a binary tree over `B+1` leaves), so `folds_used ≤ B`.
The two special cases: a lone bare PQ signature still costs one fold (its
signature has to be proven), while a lone privacy STARK is free. This is why
`b_folds = 0` is rejected in the constructor — it would strand PQ txs forever.

The builder's three fold counters are described in
[results.md](../results.md); the two comparative ones (`folds_8288`,
`folds_baseline`) are snapshots of its whole view and are deliberately *not*
capped by `B`.

## 8. Consistency checks

Passing `paranoid=True` (all unit tests do) asserts at every boundary that:

- every un-included tx is covered by at least one root object, and the pending
  counter equals the size of that covered set;
- the builder's `effective_objects` matches the number of objects flagged
  `covering`;
- no object has more parents than recorded consumptions.

## 9. What is deliberately *not* modelled

Read the numbers with these in mind:

- **No topology or bandwidth.** One uniform hop `l`; no peer counts, no queueing,
  no fan-out. Nothing carries a byte size: an object crosses the link in `l` ms
  whatever it contains, so a wrapper with one signature and a deep aggregate
  propagate identically.
- **No proof-size model**, and therefore nothing to say about EIP-8288's
  bandwidth-reduction claim. The sim measures fold counts and inclusion delay;
  `total` is a proving-work proxy, not a wire size (§5).
- **No wrapper protocol.** No `AGGREGATION_INTERVAL` batching (aggregators fold
  continuously and publish each result at once), no mode 0/1 distinction, no tx
  bodies or dedup by hash, and no per-wrapper dependency limits
  (`MAX_LEANSIG_DEPS_PER_WRAPPER`, `MAX_LEANSTARK_DEPS_PER_WRAPPER`). §3 has the
  side-by-side comparison.
- **No verification cost.** Only folding takes time; receiving and checking a
  wrapper is free.
- **Uniform fold cost.** `r` does not depend on the kind, the size or the depth of
  the inputs, so proving a bare signature and merging two deep aggregates cost the
  same.
- **No priority ordering.** No fees, no gas, no block size limit — the builder's
  only cap is `B`, and it optimises tx count, not value. The EIP's suggested
  aggregator prioritisation is not implemented; the strategy is uniform random by
  design.
- **No FOCIL**, no attesters, no reorgs, no expiry/eviction rules beyond death,
  and no adversarial actors.

## 10. What v2 does differently, and what that costs

Added after v1 was retired, when a v2 configuration approximating this model was
built and the two were run on the same arrivals. Most of v1 is reachable by
configuration; `bench/v1_calibration.py` holds the mapping and its docstring
gives the command for the v1 side, taken from the commit that still has this
code.

At 20 aggregators, `r = 1500`, `l = 100`, `B = 5`, 3 + 15 tx/s over 20 slots,
seed 42:

| | v1 | v2 approximation |
| --- | --- | --- |
| arrived | 1520 | 1520 |
| folds | 1024 | 1001 |
| delay, mean | 6.53 slots | 6.90 |
| delay, p95 | 13.87 | 14.83 |
| **included** | **524** | **381** |
| per block | 26.2 | 19.1 |
| end pool | 694 objects | 2521 |

Identical arrivals are the check that this is a calibration rather than a
similar-looking run: both draw from `Random(f"{seed}:priv")` and `":pq"` at the
same rates, so the offered load is the same transactions at the same instants.
Fold count within 2% says the cadence mapping is right — `when_ready` folding,
one fold at a time, `r` per fold. Delay within 7%.

**The 27% inclusion gap is one difference, and it is deliberate.** v1 retires an
object as soon as a viewer can see a fold that consumed it, by reading
`parents`. v2 cannot: a wrapper exposes a dependency list and a proof, never the
identity of the recursive inputs behind it, so `parents` is simulator provenance
and consulting it is exactly the omniscience v2 was built to remove.

Where that acts is worth stating precisely, because the obvious guess is wrong.
It is *not* the builder wasting picks on overlapping aggregates — redundancy
among its picks measures 0%, since greedy selection already avoids it. It is the
pool: 24% of the aggregates a node holds are entirely subsumed by another
aggregate it also holds, and the widest claim anywhere is 5 transactions. In v1
those subsumed objects vanish from every view, so folding always works on the
frontier and deepens; in v2 they persist and stay drawable, so a quarter of the
folding effort re-proves coverage that already exists.

v2's own supersede policies do not close it. `newest_if_covered` recovers about
a fifth of the gap; `newest_per_peer` is worse than keeping everything. See
[results.md](../results.md).

Two smaller differences remain, both deliberate:

- **The builder.** v1 has `n_agg` aggregators plus a builder that only builds.
  v2's proposer is an ordinary mempool node, which is the EIP's shape;
  `proposer_mode="dedicated"` reproduces v1's when the comparison needs it.
- **Arrival visibility.** In v1 an arrival's creator is the sentinel `USER = -1`,
  which matches no aggregator, so nobody — not even the entry node, not the
  builder — sees a fresh transaction before `t + l`. In v2 the node that
  received the broadcast holds it at once. v2's reading is the realistic one and
  there is no configuration for v1's.
