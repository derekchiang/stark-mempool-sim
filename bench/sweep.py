#!/usr/bin/env python3
"""
Parameter sweeps: one CSV row per configuration.

The simulator exists to compare design options, so the harness that varies them
is part of it rather than something a reader is left to write. Each row carries
its own variant identity, so a sweep's output is interpretable without the
command that produced it.

    python -m bench.sweep --axis fold_plan --values wide,binary
    python -m bench.sweep --axis relay_policy --values flood,announce_fetch --seeds 3
    python -m bench.sweep --axis proposer_mode --values fixed,rotating --csv out.csv

One axis varies; everything else is held. `--set key=value` holds a *different*
value than the default for the run, validated against the same names as the axes
so a typo cannot quietly become a row that describes a configuration nobody ran:

    python -m bench.sweep --axis wrapper_mode --values recursive,cheapest \
        --set relay_policy=flood

`docs/configuration.md` lists every field a run can set, with its default;
the axes below are the subset this harness varies.

`--prove-cost` is a flat number of milliseconds per recursive proving job,
independent of dependency or input count. This differs from economy's
pairwise-fold anchor and triage's cost per dependency.
`--prove-cost-circuit` replaces it with a cost priced by what the circuit
verifies, calibrated on one measured pairwise fold in milliseconds, and selects
`prove_cost_basis="circuit"` with it.
"""

import argparse
import sys

from bench.common import (emit_json_record, random_regular_overlay,
                          workload_parameters)
from starksim.arena import KIND_PQ
from starksim.costs import CircuitCost, CostTable
from starksim.netsim import NetworkSim, csv_header, csv_row
from starksim.network import bytes_per_ms

# Axes a sweep may vary. Named rather than free-form: a typo in a keyword would
# otherwise be silently accepted by **kwargs and produce a row that looks like a
# result for a configuration that was never run.
AXES = {
    "relay_policy": str,
    "chain_notify": str,
    "proposer_mode": str,
    "selection_strategy": str,
    "fold_plan": str,
    "supersede_policy": str,
    "eviction_policy": str,
    "wrapper_economy": str,
    "inclusion_policy": str,
    "cpu_discipline": str,
    "prove_cost_basis": str,
    "on_tick_overrun": str,
    "block_budget": int,
    "entry_subset": int,
    "aggregation_interval": float,
    "slot_length": float,
    "wrapper_mode": str,
    "fold_threshold": str,
    "aggregation_trigger": str,
    "proving_budget": float,
    "fold_amortize": float,
}


def one_run(nodes, degree, latency, seed, transactions, ticks, slots,
            uplink_mbps, prove_cost, circuit_anchor=None, **overrides):
    overlay = random_regular_overlay(nodes, degree, latency, seed)
    if circuit_anchor is None:
        prover = lambda deps: prove_cost
        basis = {}
    else:
        # Calibrated on one measured 2-to-1 fold, which is the only
        # millisecond measurement this project has to attach a scale to.
        prover = CircuitCost.anchored_on_pairwise_fold(circuit_anchor)
        basis = {"prove_cost_basis": "circuit"}
    settings = dict(seed=seed, capacity=bytes_per_ms(uplink_mbps),
                    costs=CostTable(verify_recv=2.0, prove_recursive=prover,
                                    seed=seed),
                    aggregation_interval=500.0, slot_length=2000.0,
                    proposer_id=0, block_budget=5)
    settings.update(basis)
    settings.update(overrides)
    sim = NetworkSim(overlay, **settings)
    spacing = (settings["slot_length"] * slots) / max(1, transactions)
    for index in range(transactions):
        sim.schedule_birth(index * spacing, KIND_PQ, fee=float(index % 5))
    sim.schedule_ticks(ticks)
    sim.schedule_slots(slots)
    sim.run()
    return sim


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axis", required=True, choices=sorted(AXES))
    parser.add_argument("--values", required=True,
                        help="comma-separated values for the axis")
    parser.add_argument("--nodes", type=int, default=40)
    parser.add_argument("--degree", type=int, default=4)
    parser.add_argument("--latency", type=float, default=100.0)
    parser.add_argument("--uplink-mbps", type=float, default=50.0)
    parser.add_argument("--prove-cost", type=float, default=30.0,
                        help="flat milliseconds per recursive proving job")
    parser.add_argument("--prove-cost-circuit", type=float, default=None,
                        metavar="PAIRWISE_MS",
                        help="price proving by what the circuit verifies, "
                             "calibrated on one measured 2-to-1 fold in ms; "
                             "implies prove_cost_basis=circuit")
    parser.add_argument("--set", dest="held", action="append", default=[],
                        metavar="KEY=VALUE",
                        help="hold a non-swept setting at VALUE; repeatable")
    parser.add_argument("--transactions", type=int, default=120)
    parser.add_argument("--ticks", type=int, default=24)
    parser.add_argument("--slots", type=int, default=6)
    parser.add_argument("--seeds", type=int, default=2)
    parser.add_argument("--csv", type=str, default=None,
                        help="write rows here instead of stdout")
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON record per seed and axis value")
    args = parser.parse_args()

    if args.csv and args.json:
        parser.error("--csv and --json are mutually exclusive")
    convert = AXES[args.axis]
    values = [convert(piece) for piece in args.values.split(",")]

    held = {}
    for assignment in args.held:
        name, _, raw = assignment.partition("=")
        if not _:
            parser.error(f"--set wants KEY=VALUE, got {assignment!r}")
        if name not in AXES:
            parser.error(f"--set {name}: not a known setting; one of "
                         f"{', '.join(sorted(AXES))}")
        if name == args.axis:
            parser.error(f"--set {name}: that is the swept axis")
        held[name] = AXES[name](raw)

    if args.json:
        stream = None
    elif args.csv:
        stream = open(args.csv, "w", buffering=1)
    else:
        stream = sys.stdout
    if stream is not None:
        stream.write(csv_header() + "\n")
    for value in values:
        for seed in range(args.seeds):
            sim = one_run(args.nodes, args.degree, args.latency, seed,
                          args.transactions, args.ticks, args.slots,
                          args.uplink_mbps, args.prove_cost,
                          circuit_anchor=args.prove_cost_circuit,
                          **held, **{args.axis: value})
            if args.json:
                workload = workload_parameters(
                    args, axis=args.axis, axis_value=value, held=held)
                emit_json_record("sweep", workload, seed, sim)
            else:
                stream.write(csv_row(sim.metrics()) + "\n")
    if args.csv:
        stream.close()
        print(f"wrote {len(values) * args.seeds} rows to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
