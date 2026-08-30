#!/usr/bin/env python3
"""
Proposer placement: does inclusion delay attach to a *place*?

The falsifiable claim from `[D18]` (`docs/history/networking-plan.md`): with a
fixed proposer, a transaction entering the overlay far from it is late every
slot rather than occasionally, so inclusion delay should correlate with hop
distance to the proposer. With a
rotating proposer the blind spot moves, so that correlation should collapse --
while average delay stays much the same, because rotation redistributes the
disadvantage rather than removing it.

The condition matters as much as the claim. Position can only matter when the
network is **diffusion-limited**: if a transaction reaches everyone well inside
a slot, it makes no difference where it started. So this sweeps the ratio of
propagation time to slot length.

Three things this harness is careful about, each a confound the first version
had and the results had to be qualified for.

**The ratio is measured, not proxied.** It was `diameter x latency / slot`,
which is an upper bound on a shortest path, not a propagation time -- it ignores
bandwidth, queueing and the fanout that makes real diffusion much faster than
its diameter suggests. It is now the observed time for a transaction to reach
90% of nodes, read from the trace, because by the end of a run every included
transaction has been pruned from every node's view and only the trace still
knows when each node learned of it.

**Only one thing varies.** The first version swept slot length, which dragged
the aggregation interval (`slot / 2`) and the birth rate (`slot / 10`) along
with it, so three quantities moved at once and the ratio was not the reason any
of them changed. Slot length, tick interval, birth rate, block budget and run
duration are now all fixed, and the ratio is swept by varying **link latency**
alone.

**Censored transactions are reported, not dropped.** A transaction never
included has no inclusion delay and so cannot enter a correlation over delays.
That is the strongest form of the very penalty being looked for, and it was
invisible. `corr(incl,hops)` is the companion: hops against a 0/1 inclusion
indicator, over every transaction born. Births stop `--settle-slots` before the
run does, so that a transaction counted as never included had real slots in
which it could have been -- otherwise the censored column mostly measures the
end of the run.

Without deadlines nothing is ever excluded, because one fold carries many
transactions and the block budget seldom binds. `--lifetime` is therefore where
this gets interesting: give transactions a deadline and a fixed proposer's
placement penalty stops being a delay and becomes an exclusion.

    python -m bench.placement
    python -m bench.placement --nodes 100 --seeds 6
"""

import argparse
import io
import json
import math
import statistics
import sys

from bench.common import (emit_json_record, print_table_header,
                          random_regular_overlay, workload_parameters)
from starksim.arena import KIND_PQ
from starksim.costs import CostTable
from starksim.netsim import NetworkSim
from starksim.network import bytes_per_ms

# `snd` is the only sampled event kind and it is the one that scales with
# N x degree. Thinning it hard keeps the traces small while leaving every `adm`
# event -- the ones the diffusion measurement reads -- untouched.
TRACE_SAMPLE = 500


def one_run(mode, nodes, degree, slot, tick_interval, latency, entry_subset,
            slots, birth_interval, settle_slots, block_budget, lifetime,
            seed):
    # This benchmark historically used fixed per-edge latency. Preserve that
    # workload while sharing the topology seed contract with the other benches.
    overlay = random_regular_overlay(nodes, degree, latency, seed,
                                     jitter_frac=0.0)
    trace = io.StringIO()
    sim = NetworkSim(overlay, seed=seed, capacity=bytes_per_ms(50),
                     costs=CostTable(prove_recursive=lambda deps: 30.0),
                     aggregation_interval=tick_interval, slot_length=slot,
                     proposer_mode=mode, proposer_id=0,
                     block_budget=block_budget,
                     entry_subset=entry_subset,
                     trace=trace, trace_sample=TRACE_SAMPLE)
    duration = slots * slot
    # Births stop early and the chain keeps going, so every transaction has at
    # least `settle_slots` blocks in which it could have been included. Without
    # that, the last transactions born are censored by the horizon rather than
    # by anything the model is trying to measure.
    birthing = duration - settle_slots * slot
    for index in range(int(birthing / birth_interval)):
        sim.schedule_birth(index * birth_interval, KIND_PQ,
                           lifetime=lifetime)
    sim.schedule_ticks(int(duration / tick_interval))
    sim.schedule_slots(slots)
    sim.run()
    return sim, trace.getvalue()


def diffusion_time(trace_text, nodes, fraction=0.9):
    """Observed time for a transaction to reach `fraction` of nodes.

    Reconstructed from the trace, which is the only durable record: `adm` events
    carry the object's birth time, so a delay is a subtraction, and they survive
    the pruning that empties every node's view of anything included.

    Returns the mean over transactions that got there and how many did. An
    object that never reached the fraction has no time and is not counted as a
    large one -- the count is returned so the mean is never read as if it
    covered everything.
    """
    delays = {}
    for line in trace_text.splitlines():
        event = json.loads(line)
        if event["e"] == "arr":
            # Only leaves get an `arr`, which is what keeps wrapper admissions
            # out of a transaction-diffusion measurement.
            delays[event["o"]] = [0.0]           # the entry node, at no delay
        elif event["e"] == "adm" and event["o"] in delays:
            delays[event["o"]].append(event["t"] - event["born"])
    needed = math.ceil(fraction * nodes)
    reached = []
    for values in delays.values():
        if len(values) < needed:
            continue
        values.sort()
        reached.append(values[needed - 1])
    if not reached:
        return None, 0, len(delays)
    return statistics.fmean(reached), len(reached), len(delays)


def format_ratio(value):
    if value is None:
        return "--"
    return f"{value:.2f}"


def format_correlation(value, censored=None):
    """A correlation, or why there is none.

    `none` distinguishes an undefined correlation that *is* the finding -- no
    transaction was excluded, so distance has no exclusion effect to explain --
    from `--`, which means too little data to say. A structural absence read as
    a zero would claim the effect was measured and found absent.
    """
    if value is None:
        if censored == 0:
            return "none"
        return "--"
    return f"{value:+.3f}"


def emit_json_runs(args, latencies):
    for latency in latencies:
        for mode in ("fixed", "rotating"):
            for seed in range(args.seeds):
                sim, trace_text = one_run(
                    mode, args.nodes, args.degree, args.slot,
                    args.tick_interval, latency, args.entry_subset,
                    args.slots, args.birth_interval, args.settle_slots,
                    args.block_budget, args.lifetime, seed)
                workload = workload_parameters(
                    args, proposer_mode=mode, latency=latency)
                observed, reached, total = diffusion_time(trace_text,
                                                          args.nodes)
                metrics = {
                    "delay_distance_correlation":
                        sim.delay_distance_correlation(0),
                    "inclusion_distance_correlation":
                        sim.inclusion_distance_correlation(0),
                    "delay_mean_slots": sim.delay_mean(),
                    "included_txs": sim.chain.included_count(),
                    "censored_txs": (sim.arrived_txs
                                      - sim.chain.included_count()),
                    "time_to_90_ms": observed,
                    "reached_90": reached,
                    "measured_txs": total,
                }
                emit_json_record("placement", workload, seed, sim, metrics)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=60)
    parser.add_argument("--degree", type=int, default=4)
    parser.add_argument("--entry-subset", type=int, default=8)
    parser.add_argument("--slot", type=float, default=1000.0)
    parser.add_argument("--tick-interval", type=float, default=500.0)
    parser.add_argument("--birth-interval", type=float, default=100.0)
    parser.add_argument("--slots", type=int, default=12)
    parser.add_argument("--block-budget", type=int, default=2,
                        help="folds per block; with aggregation this rarely "
                             "binds, so it rarely excludes anything")
    parser.add_argument("--lifetime", type=float, default=None,
                        help="transaction deadline in ms; this is what reaches "
                             "the regime where distance excludes rather than "
                             "merely delays")
    parser.add_argument("--settle-slots", type=int, default=2,
                        help="trailing slots with no births, so a censored "
                             "transaction had a real chance of inclusion")
    parser.add_argument("--latencies", default="25,50,100,200,400",
                        help="the only quantity swept; the ratio follows it")
    parser.add_argument("--seeds", type=int, default=4)
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON record per seed, latency, and mode")
    args = parser.parse_args()

    latencies = [float(value) for value in args.latencies.split(",")]
    if args.json:
        emit_json_runs(args, latencies)
        return 0
    print(f"nodes={args.nodes} degree={args.degree} "
          f"entry_subset={args.entry_subset} seeds={args.seeds} "
          f"block_budget={args.block_budget} lifetime={args.lifetime}")
    print(f"held fixed: slot={args.slot:.0f} ms "
          f"tick_interval={args.tick_interval:.0f} ms "
          f"birth_interval={args.birth_interval:.0f} ms slots={args.slots} "
          f"settle_slots={args.settle_slots}")
    print()
    header = (f"{'latency':>8s} {'t90/slot':>9s} {'mode':>9s} "
              f"{'corr(delay,hops)':>17s} {'corr(incl,hops)':>16s} "
              f"{'mean delay':>11s} {'included':>9s} {'censored':>9s}")
    print_table_header(header)
    for latency in latencies:
        ratio = None
        for mode in ("fixed", "rotating"):
            delay_correlations = []
            inclusion_correlations = []
            delays = []
            included = []
            censored = []
            ratios = []
            for seed in range(args.seeds):
                sim, trace_text = one_run(mode, args.nodes, args.degree,
                                          args.slot, args.tick_interval,
                                          latency, args.entry_subset,
                                          args.slots, args.birth_interval,
                                          args.settle_slots,
                                          args.block_budget, args.lifetime,
                                          seed)
                delay_correlation = sim.delay_distance_correlation(0)
                if delay_correlation is None:
                    continue
                delay_correlations.append(delay_correlation)
                inclusion_correlation = sim.inclusion_distance_correlation(0)
                if inclusion_correlation is not None:
                    inclusion_correlations.append(inclusion_correlation)
                delays.append(sim.delay_mean())
                included.append(sim.chain.included_count())
                censored.append(sim.arrived_txs - sim.chain.included_count())
                observed, _reached, _total = diffusion_time(trace_text,
                                                            args.nodes)
                if observed is not None:
                    ratios.append(observed / args.slot)
            if not delay_correlations:
                continue
            if ratios and ratio is None:
                ratio = statistics.fmean(ratios)
            if inclusion_correlations:
                inclusion = statistics.fmean(inclusion_correlations)
            else:
                inclusion = None
            mean_censored = statistics.fmean(censored)
            print(f"{latency:8.0f} {format_ratio(ratio):>9s} {mode:>9s} "
                  f"{statistics.fmean(delay_correlations):+17.3f} "
                  f"{format_correlation(inclusion, mean_censored):>16s} "
                  f"{statistics.fmean(delays):11.2f} "
                  f"{statistics.fmean(included):9.0f} "
                  f"{mean_censored:9.1f}")
        print()
    print("t90/slot is the *observed* time for a transaction to reach 90% of")
    print("nodes, over the slot length: how much of a slot it takes news to")
    print("cross the overlay. Latency is the only quantity swept, so the ratio")
    print("is the only thing that changes with it.")
    print()
    print("corr(delay,hops) covers included transactions only. corr(incl,hops)")
    print("covers every transaction born, so a placement penalty that shows up")
    print("as never being included at all -- which the delay correlation cannot")
    print("see, since a censored transaction has no delay -- appears there.")
    print("`none` there means nothing was excluded, so distance had no")
    print("exclusion effect to explain. Aggregation makes that the usual case:")
    print("one fold carries many transactions, so the block budget seldom")
    print("binds. Pass --lifetime 800 to give transactions a deadline, which is")
    print("what turns a delay penalty into an exclusion penalty -- and where")
    print("the correlation goes sharply negative for a fixed proposer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
