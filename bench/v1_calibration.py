#!/usr/bin/env python3
"""
v1's workload, run under the v2 configuration that approximates it.

v1 of this simulator had one global object store and derived visibility from
`born + l`. v2 replaced that with per-node knowledge, and the question this
answers is what the replacement cost: run both on the same arrivals and compare.

Most of v1 is reachable by configuration. A complete graph puts every node one
hop away, which is v1's `born + l` exactly; an unbounded uplink makes bytes
free, which is v1's world; a constant proving cost with 10% jitter is literally
v1's `r + U(0, r_jitter*r)`; `when_ready` is v1's continuous folding; and
`random_pair` is v1's whole aggregation strategy, uniform and kind-blind. The
arrival streams are seeded the way v1 seeds them, so both simulators see the
same transactions at the same instants -- which is the check that this is a
calibration and not merely a similar-looking run.

What is *not* reachable is the point of the exercise. v1 retires an object once
a viewer can see a fold that consumed it, reading `parents`; v2 cannot, because
a wrapper exposes a dependency list and a proof, never the identity of the
recursive inputs it folded. That is the omniscience v2 exists to remove, and
this bench prices it.

    python -m bench.v1_calibration
    python -m bench.v1_calibration --supersede-policy newest_if_covered

The v1 side of the comparison comes from the commit that still has it:

    mkdir -p /tmp/v1/starksim
    for f in __init__ sim objects actors; do
        git show bd7883d:starksim/$f.py > /tmp/v1/starksim/$f.py
    done
    (cd /tmp/v1 && python3 -m starksim.sim --n 20 --r 1500 --l 100 --b 5 \\
        --lam-priv 3 --lam-pq 15 --slots-n 20 --seed 42)
"""

import argparse
import random
import sys

from bench.common import emit_json_record, workload_parameters
from starksim.arena import KIND_PQ, KIND_PRIVACY
from starksim.costs import CostTable
from starksim.netsim import NetworkSim
from starksim.network import Overlay
from starksim.presets import preset
from starksim.strategies import names


def v1_like(args, supersede_policy):
    """The v2 configuration that approximates v1."""
    # v1 has `n_agg` aggregators *and* a builder that never folds, so the node
    # count is one higher than the aggregator count.
    nodes = args.aggregators + 1
    adjacency = [[peer for peer in range(nodes) if peer != node]
                 for node in range(nodes)]
    overlay = Overlay.from_adjacency(adjacency, base_latency=args.latency,
                                     jitter_frac=0.0)
    costs = CostTable(verify_recv=0.0, build_block=0.0,
                      prove_recursive=lambda size: args.fold_ms,
                      jitter_frac=args.fold_jitter, seed=args.seed)
    # No bandwidth, no queues, no per-link stage: in v1 an object crosses
    # the link in `l` ms whatever it contains. v1 models no transaction
    # bodies at all, so a claim is enough. Those are the preset; the
    # constant fold cost and the complete graph above are the rest of v1.
    return NetworkSim(
        overlay, seed=args.seed, **preset("v1_equivalent"),
        costs=costs,
        slot_length=args.slot_length, block_budget=args.block_budget,
        supersede_policy=supersede_policy)


def poisson_arrivals(sim, horizon, args):
    """v1's two independent streams: one generator each, rates in tx/s.

    Seeded exactly as v1 seeds them, so the offered load is a function of
    `(seed, rate)` alone and both simulators face the same arrivals.
    """
    for kind, rate, stream in ((KIND_PRIVACY, args.lam_priv, "priv"),
                               (KIND_PQ, args.lam_pq, "pq")):
        if rate <= 0:
            continue
        rng = random.Random(f"{args.seed}:{stream}")
        when = rng.expovariate(rate / 1000.0)
        while when < horizon:
            sim.schedule_birth(when, kind)
            when += rng.expovariate(rate / 1000.0)


def one_run(args, supersede_policy):
    sim = v1_like(args, supersede_policy)
    horizon = args.slots * args.slot_length
    poisson_arrivals(sim, horizon, args)
    sim.schedule_slots(args.slots)
    sim.run(until=horizon)
    metrics = sim.metrics()

    builder = sim.nodes[0]
    held = []
    for object_id in builder.view.held:
        obj = sim.arena.objects.get(object_id)
        if obj is not None and not obj.is_leaf():
            held.append(sim.arena.claim(obj))
    # How much of what the builder holds is coverage some other held aggregate
    # already contains. In v1 these are superseded away as soon as the fold
    # that consumed them is visible; here they stay, and stay foldable.
    subsumed = 0
    frontier = []
    for claim in sorted(held, key=lambda mask: mask.bit_count(), reverse=True):
        if any(claim | other == other for other in frontier):
            subsumed += 1
        else:
            frontier.append(claim)
    return sim, metrics, held, subsumed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aggregators", type=int, default=20,
                        help="v1's --n; the node count is one higher")
    parser.add_argument("--latency", type=float, default=100.0)
    parser.add_argument("--fold-ms", type=float, default=1500.0,
                        help="v1's --r: one fold, whatever it folds")
    parser.add_argument("--fold-jitter", type=float, default=0.1,
                        help="v1's --r-jitter")
    parser.add_argument("--slot-length", type=float, default=4000.0)
    parser.add_argument("--block-budget", type=int, default=5,
                        help="v1's --b; both take budget+1 greedy picks")
    parser.add_argument("--lam-priv", type=float, default=3.0)
    parser.add_argument("--lam-pq", type=float, default=15.0)
    parser.add_argument("--slots", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--supersede-policy", default=None,
                        choices=names("supersede"),
                        help="one policy; omitted races all of them")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.supersede_policy is not None:
        policies = [args.supersede_policy]
    else:
        policies = names("supersede")

    if not args.json:
        print(f"aggregators={args.aggregators} (+1 builder) "
              f"latency={args.latency:.0f} ms fold={args.fold_ms:.0f} ms "
              f"b={args.block_budget} lam={args.lam_priv}+{args.lam_pq} tx/s "
              f"slots={args.slots} seed={args.seed}")
        print()
        print(f"{'supersede':>18} {'included':>12} {'per block':>10} "
              f"{'delay':>7} {'folds':>7} {'held':>7} {'subsumed':>9}")
        print("-" * 76)

    for policy in policies:
        sim, metrics, held, subsumed = one_run(args, policy)
        if args.json:
            emit_json_record("v1_calibration",
                             workload_parameters(args, supersede=policy),
                             args.seed, sim,
                             {"held_aggregates": len(held),
                              "subsumed_aggregates": subsumed})
            continue
        included = f"{metrics['included_txs']}/{metrics['arrived_txs']}"
        share = 100.0 * subsumed / max(1, len(held))
        print(f"{policy:>18} {included:>12} {metrics['txs_per_block']:>10.2f} "
              f"{metrics['delay_mean_slots']:>7.2f} "
              f"{metrics['proofs_completed']:>7} {len(held):>7} "
              f"{subsumed:>6} ({share:.0f}%)")

    if not args.json:
        print()
        print("`subsumed` counts held aggregates whose coverage another held")
        print("aggregate already contains. v1 supersedes those away by reading")
        print("an object's parents; v2 cannot see them, so they stay foldable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
