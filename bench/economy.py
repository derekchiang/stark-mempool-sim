#!/usr/bin/env python3
"""
Wrapper economy: who is eating the bandwidth, and what do the levers buy?

The measurement that motivated this: under the default cadence, wrappers --
not raw witnesses -- are 73-98% of all offered bytes, because a recursive
proof is a constant 256 kB and every node sends one to every peer every tick
whether or not anything changed. Aggregation makes bandwidth constant in
transaction count; it does not make it small.

Three levers against that constant, each measured against the EIP's stated
cadence (`full`, one full wrapper per peer per tick):

- `skip_unchanged`: stay silent toward a peer that verifiably holds this
  exact proof, when there are no new bodies for it;
- `proof_by_ref`: send such a peer the proof's 32-byte hash instead;
- `wrapper_only` (a relay policy, not an economy): the raw witness never
  leaves its entry node, and claims spread purely by re-aggregation, one
  tick per hop -- bandwidth truly constant in transaction count, paid for
  in propagation latency.

    python -m bench.economy
    python -m bench.economy --tps 4 --seeds 3

`--prove-cost-per-fold` is milliseconds for a 2-to-1 fold. An m-input
fold costs m/2 times that anchor; this is different from triage's milliseconds
per dependency and sweep's flat milliseconds per proving job.

Every byte figure is *offered*, split by payload kind, alongside what
actually crossed (`wire`): the gap between them is queueing, and a lever
that shrinks offered bytes below the uplink ceiling closes that gap.
"""

import argparse
import sys

from bench.common import (emit_json_record, mean_field, print_table_header,
                          random_regular_overlay, schedule_horizon,
                          workload_parameters)
from starksim.arena import KIND_PRIVACY
from starksim.costs import CostTable
from starksim.netsim import NetworkSim
from starksim.network import SizeModel, bytes_per_ms
from starksim.strategies import names

SCENARIOS = [
    ("flood", "full"),
    ("announce_fetch", "full"),
    ("wrapper_only", "full"),
    ("flood", "lean"),
    # The two lean halves in isolation, on the policy where wrappers dominate
    # hardest: attributing an effect to "lean" without these would credit the
    # pair for whatever one of them does alone.
    ("announce_fetch", "skip_unchanged"),
    ("announce_fetch", "proof_by_ref"),
    ("announce_fetch", "lean"),
    ("wrapper_only", "lean"),
]


def one_run(args, policy, economy, seed, return_sim=False):
    overlay = random_regular_overlay(args.nodes, args.degree, args.latency,
                                     seed)
    # Proving priced per input, anchored on the pairwise fold. Measured fold
    # times on one machine -- 2-to-1: 889 ms, 4-to-1: 1664, 8-to-1: 3824 --
    # grow roughly linearly in the input count (ratios 1 : 1.87 : 4.30, and
    # only the ratios matter), so an m-input fold costs m/2 of the 2-to-1
    # anchor: NOT m-1 pairwise folds (folding wide is genuinely cheaper than
    # a pairwise tree, ~40% at fan-in 8), and NOT flat as v1's arity sweep
    # assumed. Linearity is measured only up to 8 inputs, and wider folds
    # cost memory a real prover does not have -- which is why the plan below
    # caps fan-in at 8. Zero keeps the original free-proving model.
    per_input_cost = args.prove_cost_per_fold / 2.0

    def prove_cost(input_count):
        return per_input_cost * input_count

    sim = NetworkSim(overlay, seed=seed,
                     capacity=bytes_per_ms(args.uplink_mbps),
                     size_model=SizeModel(), relay_policy=policy,
                     wrapper_economy=economy,
                     costs=CostTable(verify_recv=1.0,
                                     prove_recursive=prove_cost),
                     prove_cost_basis="inputs",
                     fold_plan=args.fold_plan,
                     egress_queue_bytes=args.egress_bytes or None)
    spacing = 1000.0 / args.tps
    births = max(1, int(args.window / spacing))
    for index in range(births):
        sim.schedule_birth(index * spacing, KIND_PRIVACY)
    horizon = args.window + args.drain
    schedule_horizon(sim, horizon)
    sim.run(until=horizon)
    metrics = sim.metrics()
    by_kind = dict(sim.offered_bytes_by_kind)
    result = {
        "offered": sum(by_kind.values()),
        "wrapper": by_kind.get("wrapper", 0),
        "object": (by_kind.get("object", 0) + by_kind.get("announce", 0)
                   + by_kind.get("want", 0)),
        "wire": sim.total_wire_bytes(),
        "suppressed": sim.wrapper_sends_suppressed,
        "included": metrics["included_txs"],
        "arrived": metrics["arrived_txs"],
        "delay_mean": metrics["delay_mean_slots"],
        "delay_p95": metrics["delay_p95_slots"],
        "dropped_bytes": metrics["dropped_bytes"],
    }
    if return_sim:
        return result, sim
    return result


def emit_json_runs(args):
    for policy, economy in SCENARIOS:
        for seed in range(args.seeds):
            summary, sim = one_run(args, policy, economy, seed,
                                   return_sim=True)
            workload = workload_parameters(
                args, relay_policy=policy, wrapper_economy=economy)
            emit_json_record("economy", workload, seed, sim, summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=40)
    parser.add_argument("--degree", type=int, default=6)
    parser.add_argument("--uplink-mbps", type=float, default=25.0)
    parser.add_argument("--latency", type=float, default=100.0)
    parser.add_argument("--tps", type=float, default=2.0,
                        help="transactions per second offered")
    parser.add_argument("--window", type=float, default=16000.0,
                        help="how long load is offered for")
    parser.add_argument("--drain", type=float, default=16000.0,
                        help="quiet time after the load, for inclusion")
    parser.add_argument("--egress-bytes", type=int, default=4_000_000,
                        help="send-buffer bound per node; 0 for unbounded")
    parser.add_argument("--seeds", type=int, default=2)
    parser.add_argument("--prove-cost-per-fold", type=float, default=0.0,
                        help="milliseconds per 2-to-1 fold; "
                             "an m-input fold costs m/2 of these")
    parser.add_argument("--fold-plan", default="capped8",
                        choices=names("fold_plan"),
                        help="fold plan shape; capped8 bounds fan-in at 8 "
                             "because wider folds cost prover memory")
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON record per seed and scenario")
    args = parser.parse_args()

    if args.json:
        emit_json_runs(args)
        return 0

    print(f"nodes={args.nodes} degree={args.degree} "
          f"uplink={args.uplink_mbps} Mbit/s latency={args.latency} ms "
          f"tps={args.tps:g} window={args.window:.0f} ms "
          f"drain={args.drain:.0f} ms seeds={args.seeds} "
          f"fold={args.prove_cost_per_fold:.0f} ms plan={args.fold_plan}")
    ceiling = (args.nodes * bytes_per_ms(args.uplink_mbps)
               * (args.window + args.drain))
    print(f"physical ceiling over the whole run: {ceiling / 1e6:.0f} MB")
    print()
    header = (f"{'relay':>14} {'economy':>14} {'offered':>8} {'wrapper':>8} "
              f"{'object':>7} {'wire':>7} {'drop MB':>8} {'skips':>6} "
              f"{'incl':>9} {'delay':>6} {'p95':>6}")
    print_table_header(header)
    for policy, economy in SCENARIOS:
        runs = []
        for seed in range(args.seeds):
            runs.append(one_run(args, policy, economy, seed))
        included = (f"{mean_field(runs, 'included'):.0f}/"
                    f"{mean_field(runs, 'arrived'):.0f}")
        print(f"{policy:>14} {economy:>14} "
              f"{mean_field(runs, 'offered') / 1e6:7.0f}M "
              f"{mean_field(runs, 'wrapper') / 1e6:7.0f}M "
              f"{mean_field(runs, 'object') / 1e6:6.0f}M "
              f"{mean_field(runs, 'wire') / 1e6:6.0f}M "
              f"{mean_field(runs, 'dropped_bytes') / 1e6:8.1f} "
              f"{mean_field(runs, 'suppressed'):6.0f} "
              f"{included:>9} "
              f"{mean_field(runs, 'delay_mean'):6.2f} "
              f"{mean_field(runs, 'delay_p95'):6.2f}")
    print()
    print("offered is what the send buffers *accepted* -- a tail-dropped")
    print("message is in drop MB, not here, so under saturation this is")
    print("admitted traffic rather than attempted demand. wire is what")
    print("crossed. delay is inclusion delay in slots, over included")
    print("transactions only -- compare it against incl, because a lever")
    print("that includes fewer transactions can look faster.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
