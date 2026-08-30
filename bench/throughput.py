#!/usr/bin/env python3
"""
Sustainable transaction rate at a fixed uplink, per named configuration.

`bench.levers` showed that every bandwidth lever switched on together offers
78% fewer bytes than the EIP as written -- on a workload the uplink never
saturates. Fewer bytes is not more throughput until the uplink is the thing
that binds, so this raises the transaction rate through the point where it
does and asks, for each preset, what the network still includes.

The x-axis is `offered / ceiling` for the EIP as written: bytes the baseline
configuration offers over what the uplinks could carry in the birth window.
Every configuration is run at the same transaction rates, so a row compares
what two configurations make of the same load. Inclusion is counted after a
drain; a configuration that has not included everything by then is late
until a longer `--drain` shows it lost, and delay says how late.

    python -m bench.throughput
    python -m bench.throughput --tps 16,32,64 --seeds 3
    python -m bench.throughput --presets eip_faithful,high_throughput
    python -m bench.throughput --presets high_throughput,high_throughput+fold_plan=capped8

A preset name may carry overrides, `name+field=value+field=value`, so one
lever can be switched on or off against a named configuration and the row
is still reproducible from its command.

Post-quantum signatures, as in `bench.levers`: two of the levers engage only
for them.
"""

import argparse
import statistics
import sys

from bench.common import (emit_json_record, print_table_header,
                          random_regular_overlay, schedule_horizon,
                          workload_parameters)
from starksim.arena import KIND_PQ
from starksim.costs import CostTable
from starksim.netsim import NetworkSim
from starksim.network import SizeModel, bytes_per_ms
from starksim.presets import names as preset_names, preset


def configuration(spec):
    """`name` or `name+field=value...`: a preset with overrides applied."""
    name, *overrides = spec.split("+")
    settings = preset(name)
    for override in overrides:
        field, value = override.split("=", 1)
        settings[field] = value
    return settings


def one_run(args, name, transactions, seed, return_sim=False):
    overlay = random_regular_overlay(args.nodes, args.degree, args.latency,
                                     seed)
    settings = dict(seed=seed, capacity=bytes_per_ms(args.uplink_mbps),
                    size_model=SizeModel(),
                    costs=CostTable(verify_recv=1.0,
                                    prove_recursive=args.prove_ms, seed=seed),
                    egress_queue_bytes=args.egress_bytes or None)
    settings.update(configuration(name))
    sim = NetworkSim(overlay, **settings)
    spacing = args.window / max(1, transactions)
    for index in range(transactions):
        sim.schedule_birth(index * spacing, KIND_PQ, fee=float(index % 5))
    schedule_horizon(sim, args.window + args.drain)
    sim.run(until=args.window + args.drain)
    metrics = sim.metrics()
    queued = sum(sim.egress[node].queued_weight for node in range(args.nodes))
    blocks = sim.chain.blocks
    per_block = (statistics.fmean(block.included.bit_count() for block in blocks)
                 if blocks else 0.0)
    result = {
        "offered": metrics["offered_bytes"],
        "wire": metrics["wire_bytes"],
        "included": metrics["included_txs"],
        "arrived": metrics["arrived_txs"],
        "delay": metrics["delay_mean_slots"],
        "dropped": metrics["dropped_messages"],
        "queued": queued,
        "proofs": metrics["proofs_completed"],
        "txs_per_block": per_block,
    }
    if return_sim:
        return result, sim
    return result


def mean_of(runs, field):
    return statistics.fmean(run[field] for run in runs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=40)
    parser.add_argument("--degree", type=int, default=6)
    parser.add_argument("--uplink-mbps", type=float, default=25.0)
    parser.add_argument("--latency", type=float, default=100.0)
    parser.add_argument("--tps", default="8,16,32,64,128",
                        help="transactions per second born over the window, "
                             "one row each")
    parser.add_argument("--window", type=float, default=16000.0,
                        help="ms over which transactions are born")
    parser.add_argument("--drain", type=float, default=8000.0,
                        help="quiet time after the load, for inclusion")
    parser.add_argument("--prove-ms", type=float, default=200.0,
                        help="flat milliseconds per recursive proving job")
    parser.add_argument("--egress-bytes", type=int, default=4_000_000,
                        help="send-buffer bound per node; 0 for unbounded")
    parser.add_argument("--presets", default="eip_faithful,high_throughput",
                        help="named configurations to compare, in order; "
                             "one of " + ", ".join(preset_names()))
    parser.add_argument("--seeds", type=int, default=2)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    # Each rate keeps the number it was asked for beside the count it became:
    # the record must say this run's rate, not the sweep string it came from.
    rates = [(float(value), int(round(float(value) * args.window / 1000.0)))
             for value in args.tps.split(",")]
    presets = args.presets.split(",")
    for name in presets:
        configuration(name)  # refuse a typo before spending minutes on it

    results = {}
    for tps, transactions in rates:
        for name in presets:
            runs = []
            for seed in range(args.seeds):
                outcome, sim = one_run(args, name, transactions, seed,
                                       return_sim=True)
                runs.append(outcome)
                if args.json:
                    emit_json_record(
                        "throughput",
                        workload_parameters(args, preset=name, tps=tps,
                                            transactions=transactions),
                        seed, sim, outcome)
            results[(transactions, name)] = runs
    if args.json:
        return 0

    ceiling = args.nodes * bytes_per_ms(args.uplink_mbps) * args.window
    print(f"nodes={args.nodes} degree={args.degree} "
          f"uplink={args.uplink_mbps:.0f} Mbit/s latency={args.latency:.0f} ms "
          f"PQ txs over {args.window / 1000:.0f} s, {args.drain / 1000:.0f} s "
          f"drain, prove={args.prove_ms:.0f} ms/job seeds={args.seeds}")
    print(f"physical ceiling over the birth window: {ceiling / 1e6:.0f} MB; "
          f"offered/ceil is the first preset's")
    print()
    width = max(len(name) for name in presets)
    print_table_header(
        f"{'txs':>5} {'tx/s':>6} {'offered/ceil':>12} {'preset':>{width}} "
        f"{'offered MB':>11} {'included':>10} {'delay':>6} {'drops':>6} "
        f"{'queued MB':>10} {'proofs':>7} {'txs/blk':>8}")
    for tps, transactions in rates:
        ratio = mean_of(results[(transactions, presets[0])], "offered") / ceiling
        for index, name in enumerate(presets):
            runs = results[(transactions, name)]
            lead = (f"{transactions:>5} {transactions / (args.window / 1000):>6.1f} "
                    f"{ratio:>12.2f}") if index == 0 else " " * 25
            print(f"{lead} {name:>{width}} "
                  f"{mean_of(runs, 'offered') / 1e6:>11.1f} "
                  f"{mean_of(runs, 'included'):>4.0f}/{mean_of(runs, 'arrived'):<5.0f} "
                  f"{mean_of(runs, 'delay'):>6.2f} {mean_of(runs, 'dropped'):>6.0f} "
                  f"{mean_of(runs, 'queued') / 1e6:>10.1f} "
                  f"{mean_of(runs, 'proofs'):>7.0f} "
                  f"{mean_of(runs, 'txs_per_block'):>8.1f}")
    print()
    print("included is counted after the drain; what the drain did not reach")
    print("is late until a longer drain says otherwise, and delay says how")
    print("late. Drops are messages refused at a full send buffer, never")
    print("traffic. txs/blk is the mean over the run's blocks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
