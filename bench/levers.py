#!/usr/bin/env python3
"""
Do the bandwidth levers compose, or do they eat each other?

Every saving this project has measured was measured alone: `announce_fetch`
against `flood`, `lean` against `full`, `crossover` against `always`, `capped8`
against `wide`, `cheapest` against `recursive`. The proposal in
`docs/proposals/aggregation-strategy.md` argues from those numbers as though
they add up. They have reason not to, and the reasons are mechanical rather
than statistical:

- `crossover` defers folds, so there are fewer emissions for `lean` to find
  redundant -- the second lever is applied to what the first left behind.
- `announce_fetch` changes which raw objects a node ever holds, and holding a
  claim's witnesses is exactly the precondition `cheapest` needs, so the relay
  policy gates the wrapper mode.
- `capped8` changes the shape of a fold but not what it asserts, so it should
  be independent of all three -- which is a prediction worth failing.

So this runs the EIP-faithful configuration, then each lever alone, then all of
them together, on one workload. The number that matters is the last column:
what the levers *would* save if their individual savings were independent,
against what they actually save together.

    python -m bench.levers
    python -m bench.levers --transactions 512 --seeds 3

Post-quantum signatures, because two of the levers only engage for them: a
leanSTARK witness already costs a recursive proof, so privacy transactions
never choose mode 0 and never defer below the crossover.
"""

import argparse
import statistics
import sys

from bench.common import (emit_json_record, random_regular_overlay,
                          schedule_horizon, workload_parameters)
from starksim.arena import KIND_PQ
from starksim.costs import CostTable
from starksim.netsim import NetworkSim
from starksim.network import SizeModel, bytes_per_ms

# Each lever, as the settings that turn it on. The baseline is the EIP as
# written (docs/configuration.md), so every row is a deviation from the
# specification rather than from an arbitrary starting point.
LEVERS = {
    "announce_fetch": {"relay_policy": "announce_fetch"},
    "lean": {"wrapper_economy": "lean"},
    "crossover": {"fold_threshold": "crossover"},
    "capped8": {"fold_plan": "capped8"},
    "cheapest": {"wrapper_mode": "cheapest"},
}


def one_run(args, seed, overrides):
    overlay = random_regular_overlay(args.nodes, args.degree, args.latency,
                                     seed)
    settings = dict(seed=seed, capacity=bytes_per_ms(args.uplink_mbps),
                    size_model=SizeModel(),
                    costs=CostTable(verify_recv=1.0,
                                    prove_recursive=args.prove_ms, seed=seed))
    settings.update(overrides)
    sim = NetworkSim(overlay, **settings)
    spacing = (args.window / max(1, args.transactions))
    for index in range(args.transactions):
        sim.schedule_birth(index * spacing, KIND_PQ, fee=float(index % 5))
    schedule_horizon(sim, args.window + args.drain)
    sim.run(until=args.window + args.drain)
    metrics = sim.metrics()
    return {
        "offered": metrics["offered_bytes"],
        "included": metrics["included_txs"],
        "arrived": metrics["arrived_txs"],
        "delay": metrics["delay_mean_slots"],
        "proofs": metrics["proofs_completed"],
    }, sim


def mean_of(runs, field):
    return statistics.fmean(run[field] for run in runs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=40)
    parser.add_argument("--degree", type=int, default=6)
    parser.add_argument("--uplink-mbps", type=float, default=25.0)
    parser.add_argument("--latency", type=float, default=100.0)
    parser.add_argument("--transactions", type=int, default=256)
    parser.add_argument("--window", type=float, default=16000.0,
                        help="ms over which transactions are born")
    parser.add_argument("--drain", type=float, default=8000.0,
                        help="quiet time after the load, for inclusion")
    parser.add_argument("--prove-ms", type=float, default=200.0,
                        help="flat milliseconds per recursive proving job")
    parser.add_argument("--seeds", type=int, default=2)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cases = [("(the EIP as written)", {})]
    cases.extend((name, dict(settings)) for name, settings in LEVERS.items())
    everything = {}
    for settings in LEVERS.values():
        everything.update(settings)
    cases.append(("all five together", everything))

    results = {}
    for label, overrides in cases:
        runs = []
        for seed in range(args.seeds):
            outcome, sim = one_run(args, seed, overrides)
            runs.append(outcome)
            if args.json:
                emit_json_record("levers",
                                 workload_parameters(args, lever=label),
                                 seed, sim)
        results[label] = runs

    if args.json:
        return 0

    print(f"nodes={args.nodes} degree={args.degree} "
          f"uplink={args.uplink_mbps:.0f} Mbit/s latency={args.latency:.0f} ms "
          f"{args.transactions} PQ txs over {args.window / 1000:.0f} s "
          f"prove={args.prove_ms:.0f} ms/job seeds={args.seeds}")
    print()
    baseline = mean_of(results["(the EIP as written)"], "offered")
    print(f"{'configuration':>22} {'offered MB':>11} {'saved':>7} "
          f"{'included':>10} {'delay':>7} {'proofs':>7}")
    print("-" * 70)
    saved_alone = {}
    for label, runs in results.items():
        offered = mean_of(runs, "offered")
        saved = 100.0 * (baseline - offered) / baseline
        if label in LEVERS:
            saved_alone[label] = saved
        included = mean_of(runs, "included")
        arrived = mean_of(runs, "arrived")
        print(f"{label:>22} {offered / 1e6:>11.1f} {saved:>6.1f}% "
              f"{included:>5.0f}/{arrived:<4.0f} "
              f"{mean_of(runs, 'delay'):>7.2f} {mean_of(runs, 'proofs'):>7.0f}")

    # If the levers were independent, each would keep its own fraction of what
    # the one before it left. Compounding rather than adding, because a lever
    # saves a share of the traffic it sees, not a fixed number of bytes.
    remaining = 1.0
    for saved in saved_alone.values():
        remaining *= (1.0 - saved / 100.0)
    predicted = 100.0 * (1.0 - remaining)
    actual = 100.0 * (baseline - mean_of(results["all five together"],
                                         "offered")) / baseline
    print()
    print(f"if the levers were independent: {predicted:5.1f}% saved")
    print(f"measured together:              {actual:5.1f}% saved")
    print(f"interference:                   {actual - predicted:+5.1f} points")
    print()
    print("A lever saves a share of the traffic it sees, so independence")
    print("compounds rather than adds. A negative gap means the levers")
    print("overlap -- each is partly saving what another already saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
