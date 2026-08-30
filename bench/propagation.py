#!/usr/bin/env python3
"""
Reproducible propagation and bandwidth measurement.

Committed so that the numbers quoted in docs/history/networking-plan.md can be
regenerated rather than believed. Everything that affects the result is a
parameter printed in the header: node count, degree, uplink rate, latency,
object kind, relay policy, the birth stagger, the observation window, and the
seeds.

Three things this harness is careful about, each because getting it wrong here
once produced a number that had to be retracted.

**Uncontended and contended propagation are different questions.** "How long
does one 256 kB proof take to cross the network?" and "what happens when twenty
of them are offered per slot?" have different answers, and the earlier version
reported a single statistic that mixed them. They are now two tables.

**Equal observation windows.** Under sustained load, births are staggered over
`--spread` and every object is then measured for exactly `--window` from *its
own* birth, so the last object born is watched for as long as the first. The
earlier version staggered births and sampled every object once at a single
shared horizon, which gave the oldest object the whole run and the youngest a
twentieth of it; coverage then tracked age almost perfectly, and a mean over
that measures the birth schedule as much as the network.

**Right-censoring declared, not averaged away.** An object that never reaches
90% of nodes within its window has no time-to-90%. It is not counted as a large
delay, and it is not silently dropped either -- the count that *did* reach each
threshold is printed beside every mean, because the objects that fail are the
slow ones and a mean over the rest is a mean over survivors.

Reports **wire** bytes, not offered bytes. Bytes handed to an egress queue have
not crossed anything, and on a saturated uplink counting them produces totals
above the physical capacity of the links.

    python -m bench.propagation
    python -m bench.propagation --nodes 500 --seeds 5
    python -m bench.propagation --objects 5 --spread 4000
"""

import argparse
import statistics
import sys

from bench.common import (emit_json_record, print_table_header,
                          random_regular_overlay, workload_parameters)
from starksim.arena import KIND_PQ, KIND_PRIVACY
from starksim.netsim import RELAY_ANNOUNCE_FETCH, RELAY_FLOOD, NetworkSim
from starksim.network import SizeModel, bytes_per_ms

THRESHOLDS = (0.5, 0.9, 1.0)

KINDS = ((KIND_PRIVACY, "privacy (256 kB)"), (KIND_PQ, "pq sig (3.2 kB)"))
POLICIES = (RELAY_FLOOD, RELAY_ANNOUNCE_FETCH)


def one_run(nodes, degree, uplink_mbps, latency, jitter, kind, policy,
            objects, spread, window, seed, return_sim=False):
    """One measurement. `spread == 0` births everything at once.

    With `objects == 1` and `spread == 0` this is the uncontended case: a single
    object diffusing through an otherwise idle network.
    """
    overlay = random_regular_overlay(nodes, degree, latency, seed,
                                     jitter_frac=jitter)
    sim = NetworkSim(overlay, seed=seed, capacity=bytes_per_ms(uplink_mbps),
                     size_model=SizeModel(), relay_policy=policy)
    spacing = spread / objects
    for index in range(objects):
        sim.schedule_birth(index * spacing, kind)

    # The loaded run has two stages. Utilisation is read at the end of the birth
    # stagger, where the offered load is what the parameters say it is; past
    # that point nothing new is born and the run is only draining, so averaging
    # the drain in would understate the load the network was put under.
    utilisation = None
    if spread > 0:
        sim.run(until=spread)
        utilisations = []
        for node_id in range(nodes):
            utilisations.append(sim.egress_utilisation(node_id, spread))
        utilisation = statistics.fmean(utilisations)

    # Now let the last object born have its full window too.
    sim.run(until=spread + window)

    ordered = sorted(sim.arena.objects.values(), key=lambda obj: obj.born)
    coverages = []
    for obj in ordered:
        coverages.append(sim.coverage_fraction_within(obj, window))

    times = {}
    reached = {}
    for fraction in THRESHOLDS:
        delays = []
        for obj in ordered:
            delay = sim.time_to_coverage(obj, fraction, window)
            if delay is not None:
                delays.append(delay)
        reached[fraction] = len(delays)
        if delays:
            times[fraction] = statistics.fmean(delays)
        else:
            times[fraction] = None

    # Whether the run is in steady state or still building a backlog. Objects
    # born late face the queues everything before them left behind, and equal
    # windows do not remove that -- they only stop the *measurement* from
    # confusing it with age. Near zero means the mean describes a settled
    # network; strongly negative means the network is degrading through the run
    # and the mean is a snapshot of a transient, not a steady state.
    half = len(coverages) // 2
    if half:
        later = statistics.fmean(coverages[half:])
        earlier = statistics.fmean(coverages[:half])
        drift = later - earlier
    else:
        drift = 0.0

    result = {
        "coverage": statistics.fmean(coverages),
        "drift": drift,
        "times": times,
        "reached": reached,
        "objects": len(ordered),
        "wire_mb": sim.total_wire_bytes() / 1e6,
        "offered_mb": sim.total_offered_bytes() / 1e6,
        "utilisation": utilisation,
        "max_queue": sim.max_egress_depth(),
    }
    if return_sim:
        return result, sim
    return result


def sweep(args, objects, spread):
    """Every (kind, policy) pair at this load, over every seed."""
    cases = []
    for kind, label in KINDS:
        for policy in POLICIES:
            runs = []
            for seed in range(args.seeds):
                runs.append(one_run(args.nodes, args.degree, args.uplink_mbps,
                                    args.latency, args.jitter, kind, policy,
                                    objects, spread, args.window, seed))
            cases.append((label, policy, runs))
    return cases


def mean_spread(runs, key):
    values = [run[key] for run in runs]
    return statistics.fmean(values), min(values), max(values)


def mean_time(runs, fraction):
    """Mean time-to-threshold across seeds, over the seeds that reached it."""
    values = []
    for run in runs:
        if run["times"][fraction] is not None:
            values.append(run["times"][fraction])
    if not values:
        return None
    return statistics.fmean(values)


def reached_share(runs, fraction):
    """Objects that reached the threshold, out of objects measured."""
    got = sum(run["reached"][fraction] for run in runs)
    total = sum(run["objects"] for run in runs)
    return got, total


def format_time(value):
    if value is None:
        return "--"
    return f"{value:.0f}ms"


def print_propagation(cases, window, with_drift):
    header = f"{'object':22s} {'policy':16s} {'cov@win':>8s} "
    if with_drift:
        header += f"{'drift':>7s} "
    header += (f"{'t50':>8s} {'t90':>8s} {'t100':>8s} "
               f"{'reached 50/90/100':>21s}")
    print_table_header(header)
    for label, policy, runs in cases:
        coverage = mean_spread(runs, "coverage")
        row = f"{label:22s} {policy:16s} {coverage[0]:7.1%} "
        if with_drift:
            row += f"{mean_spread(runs, 'drift')[0]:+7.1%} "
        counts = []
        for fraction in THRESHOLDS:
            got, total = reached_share(runs, fraction)
            counts.append(f"{got}/{total}")
        row += (f"{format_time(mean_time(runs, 0.5)):>8s} "
                f"{format_time(mean_time(runs, 0.9)):>8s} "
                f"{format_time(mean_time(runs, 1.0)):>8s} "
                f"{' '.join(counts):>21s}")
        print(row)


def emit_json_runs(args):
    loads = (("uncontended", 1, 0.0),
             ("sustained", args.objects, args.spread))
    for load, objects, spread in loads:
        for kind, label in KINDS:
            for policy in POLICIES:
                for seed in range(args.seeds):
                    summary, sim = one_run(
                        args.nodes, args.degree, args.uplink_mbps,
                        args.latency, args.jitter, kind, policy, objects,
                        spread, args.window, seed, return_sim=True)
                    workload = workload_parameters(
                        args, load=load, object_kind=kind,
                        object_label=label, relay_policy=policy,
                        objects=objects, spread=spread)
                    emit_json_record("propagation", workload, seed, sim,
                                     summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=200)
    parser.add_argument("--degree", type=int, default=8)
    parser.add_argument("--uplink-mbps", type=float, default=25.0)
    parser.add_argument("--latency", type=float, default=100.0)
    parser.add_argument("--jitter", type=float, default=0.1)
    parser.add_argument("--objects", type=int, default=20,
                        help="objects offered during the sustained-load run")
    parser.add_argument("--spread", type=float, default=4000.0,
                        help="births are staggered evenly over this window")
    parser.add_argument("--window", type=float, default=4000.0,
                        help="each object is observed for this long from its "
                             "own birth")
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON record per seed and case")
    args = parser.parse_args()

    if args.json:
        emit_json_runs(args)
        return 0

    print(f"nodes={args.nodes} degree={args.degree} "
          f"uplink={args.uplink_mbps} Mbit/s latency={args.latency} ms "
          f"jitter={args.jitter} objects={args.objects} "
          f"spread={args.spread} ms window={args.window} ms "
          f"seeds={args.seeds}")
    sizes = SizeModel()
    print(f"sizes: pq={sizes.tx_body + sizes.triple + sizes.sig_witness} B  "
          f"privacy={sizes.tx_body + sizes.triple + sizes.stark_witness} B")

    print()
    print(f"uncontended -- one object into an idle network, "
          f"observed {args.window:.0f} ms")
    solo = sweep(args, objects=1, spread=0.0)
    print_propagation(solo, args.window, with_drift=False)

    print()
    print(f"sustained load -- {args.objects} objects over {args.spread:.0f} ms, "
          f"each observed {args.window:.0f} ms from its own birth")
    loaded = sweep(args, objects=args.objects, spread=args.spread)
    print_propagation(loaded, args.window, with_drift=True)

    header = (f"{'object':22s} {'policy':16s} {'wire MB':>9s} "
              f"{'offered':>9s} {'uplink':>7s} {'queue':>6s}")
    print()
    print(f"bandwidth under sustained load -- uplink over the "
          f"{args.spread:.0f} ms stagger, bytes over the whole run")
    print_table_header(header)
    for label, policy, runs in loaded:
        wire = mean_spread(runs, "wire_mb")
        offered = mean_spread(runs, "offered_mb")
        util = mean_spread(runs, "utilisation")
        queue = mean_spread(runs, "max_queue")
        print(f"{label:22s} {policy:16s} {wire[0]:9.1f} {offered[0]:9.1f} "
              f"{util[0]:6.1%} {queue[0]:6.0f}")
        print(f"{'':22s} {'  wire range':16s} "
              f"{wire[1]:9.1f}-{wire[2]:.1f}")

    # The ceiling has to span the same interval as the byte totals it bounds.
    # Both columns above are cumulative over the whole run, stagger plus
    # window, so a ceiling computed over the stagger alone would be the wrong
    # comparison -- and reporting a byte total against a mismatched ceiling is
    # the same species of error this harness exists to avoid.
    run_ms = args.spread + args.window
    ceiling = args.nodes * bytes_per_ms(args.uplink_mbps) * run_ms / 1e6
    print()
    print(f"physical ceiling over the whole run: {ceiling:.0f} MB "
          f"({args.nodes} uplinks x {run_ms/1000:.0f} s), matching the byte "
          "columns above.")
    print("Wire totals must stay under it; offered totals need not, and an "
          "offered total above it")
    print("is the definition of an unsustainable load.")
    print("t50/t90/t100 are means over the objects that reached the threshold "
          "inside their window;")
    print("the rest are right-censored, which is what the reached counts show. "
          "A threshold no")
    print("object reaches prints as --, never as a large delay. drift is late-"
          "birth coverage minus")
    print("early-birth coverage: near zero means a settled network, strongly "
          "negative means the")
    print("mean above describes a collapse in progress rather than a steady "
          "state.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
