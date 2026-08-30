#!/usr/bin/env python3
"""
Triage: which policy loses the least when the proving budget forces a choice?

Every earlier bench ran with proving comfortably inside the tick, so selection
never had to leave anything out and "which picks are better" was unobservable.
This one starves it on purpose: proving is priced per dependency so a tick's
budget covers fewer claims than arrive per tick, the candidate limit forces
eviction, and transactions carry heterogeneous fees and deadlines. What a
policy declines to fold now *costs* something -- an expiry, a lost fee -- and
the cost is the measurement.

Two fee models, because each answers a different question. `cycle` runs fees
through {1, 2, 5, 10, 50} against lifetimes cycling at a coprime period:
value and urgency exactly uncorrelated, every combination equally
represented -- a balanced design that squeezes the most policy signal out of
few seeds, at the price of a bounded, flat spectrum nothing like a real fee
market. `lognormal` draws fees from a dedicated, labelled stream
(`{seed}:fees`), so the workload is a function of the seed alone -- never of
draw order, and identical across every policy row at a given seed -- and the
tail weight `--fee-sigma` becomes a measured variable: the fee-aware
policies' whole advantage lives in the tail, so their lead should widen
with sigma, and a balanced-cycle result that did not survive a heavy tail
would be an artefact of the cycle.

    python -m bench.triage
    python -m bench.triage --fee-model lognormal --fee-sigma 2 --seeds 6
    python -m bench.triage --tps 10 --prove-ms 30

Included fee is the fee market's verdict; included count is the EIP's
throughput verdict; the two can disagree, and which to optimise is a design
question this bench informs rather than answers.

`--prove-ms` is milliseconds per selected dependency. This differs from
economy's pairwise-fold anchor and sweep's flat cost per proving job.
"""

import argparse
import random
import sys

from bench.common import (emit_json_record, mean_field, print_table_header,
                          random_regular_overlay, schedule_horizon,
                          workload_parameters)
from starksim.aggregation import live_fee
from starksim.arena import KIND_PQ
from starksim.costs import CostTable
from starksim.netsim import NetworkSim
from starksim.network import bytes_per_ms

FEES = (1.0, 2.0, 5.0, 10.0, 50.0)
LIFETIMES = (3000.0, 6000.0, 12000.0, 24000.0)

SCENARIOS = [
    ("eip_priority", "usefulness"),
    ("eip_priority", "fee_value"),
    ("fee_density", "usefulness"),
    ("fee_density", "fee_value"),
    ("deadline", "usefulness"),
    ("deadline", "fee_value"),
]


def one_run(args, selection, eviction, seed, return_sim=False):
    overlay = random_regular_overlay(args.nodes, args.degree, args.latency,
                                     seed)
    prove_ms = args.prove_ms
    sim = NetworkSim(overlay, seed=seed,
                     capacity=bytes_per_ms(args.uplink_mbps),
                     selection_strategy=selection, eviction_policy=eviction,
                     candidate_limit=args.candidate_limit,
                     costs=CostTable(verify_recv=1.0,
                                     prove_recursive=lambda deps:
                                     prove_ms * deps),
                     egress_queue_bytes=4_000_000)
    spacing = 1000.0 / args.tps
    births = int(args.window / spacing)
    # A stream of its own, consumed only in this loop: the fee sequence is a
    # function of the seed alone, so every policy row at a given seed faces
    # the identical workload, and no unrelated draw can ever reshuffle it.
    fee_stream = random.Random(f"{seed}:fees")
    offered_fee = 0.0
    for index in range(births):
        if args.fee_model == "lognormal":
            # Median 1 by construction; the share statistics are scale-free,
            # so only the tail weight (sigma) is a real parameter.
            fee = fee_stream.lognormvariate(0.0, args.fee_sigma)
        else:
            fee = FEES[index % len(FEES)]
        lifetime = LIFETIMES[index % len(LIFETIMES)]
        offered_fee += fee
        sim.schedule_birth(index * spacing, KIND_PQ, fee=fee,
                           lifetime=lifetime)
    horizon = args.window + args.drain
    schedule_horizon(sim, horizon)
    sim.run(until=horizon)
    metrics = sim.metrics()
    included_fee = live_fee(sim.arena, sim.chain.included)
    result = {
        "births": births,
        "offered_fee": offered_fee,
        "included": metrics["included_txs"],
        "included_fee": included_fee,
        # The share is computed per run and averaged, not as a ratio of
        # averages: under a drawn fee model the offered fee varies by seed,
        # and the per-run ratio is what stays paired across policy rows.
        "fee_share": included_fee / offered_fee if offered_fee else 0.0,
        "expired": metrics["expired_txs"],
        "expired_fee": live_fee(sim.arena, sim.chain.expired),
        "evicted": metrics["evicted"],
        "delay_mean": metrics["delay_mean_slots"],
    }
    if return_sim:
        return result, sim
    return result


def emit_json_runs(args):
    for selection, eviction in SCENARIOS:
        for seed in range(args.seeds):
            summary, sim = one_run(args, selection, eviction, seed,
                                   return_sim=True)
            workload = workload_parameters(
                args, selection_strategy=selection, eviction_policy=eviction)
            emit_json_record("triage", workload, seed, sim, summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=40)
    parser.add_argument("--degree", type=int, default=6)
    parser.add_argument("--uplink-mbps", type=float, default=25.0)
    parser.add_argument("--latency", type=float, default=100.0)
    parser.add_argument("--tps", type=float, default=20.0,
                        help="transactions per second offered")
    parser.add_argument("--prove-ms", type=float, default=60.0,
                        help="milliseconds per selected dependency; the tick budget "
                             "is 1000 ms, so 60 means ~16 deps per fold "
                             "against ~20 arrivals per tick -- triage binds")
    parser.add_argument("--candidate-limit", type=int, default=16)
    parser.add_argument("--window", type=float, default=24000.0)
    parser.add_argument("--drain", type=float, default=12000.0)
    parser.add_argument("--fee-model", default="cycle",
                        choices=["cycle", "lognormal"])
    parser.add_argument("--fee-sigma", type=float, default=1.0,
                        help="tail weight of the lognormal fee model; "
                             "median is 1 either way")
    parser.add_argument("--seeds", type=int, default=2)
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON record per seed and scenario")
    args = parser.parse_args()

    if args.json:
        emit_json_runs(args)
        return 0

    print(f"nodes={args.nodes} degree={args.degree} "
          f"uplink={args.uplink_mbps} Mbit/s tps={args.tps:g} "
          f"prove={args.prove_ms:.0f} ms/dep candidate_limit="
          f"{args.candidate_limit} window={args.window:.0f} ms "
          f"drain={args.drain:.0f} ms seeds={args.seeds}")
    if args.fee_model == "lognormal":
        print(f"fees lognormal(sigma={args.fee_sigma}) from the {{seed}}:fees "
              f"stream; lifetimes cycle "
              f"{tuple(int(v) for v in LIFETIMES)} ms")
    else:
        print(f"fees cycle {FEES}; lifetimes cycle "
              f"{tuple(int(v) for v in LIFETIMES)} ms, coprime periods")
    print()
    header = (f"{'selection':>13} {'eviction':>10} {'incl':>10} "
              f"{'incl fee':>9} {'fee %':>6} {'expired':>8} "
              f"{'lost fee':>9} {'evicted':>8} {'delay':>6}")
    print_table_header(header)
    for selection, eviction in SCENARIOS:
        runs = []
        for seed in range(args.seeds):
            runs.append(one_run(args, selection, eviction, seed))
        fee_share = mean_field(runs, "fee_share")
        included = (f"{mean_field(runs, 'included'):.0f}/"
                    f"{mean_field(runs, 'births'):.0f}")
        print(f"{selection:>13} {eviction:>10} {included:>10} "
              f"{mean_field(runs, 'included_fee'):9.0f} "
              f"{fee_share:6.1%} "
              f"{mean_field(runs, 'expired'):8.0f} "
              f"{mean_field(runs, 'expired_fee'):9.0f} "
              f"{mean_field(runs, 'evicted'):8.0f} "
              f"{mean_field(runs, 'delay_mean'):6.2f}")
    print()
    print("incl fee is the fee market's verdict, incl the EIP's throughput")
    print("verdict; lost fee is what expired unincluded. A policy can win one")
    print("and lose the other -- that trade-off, not a winner, is the result.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
