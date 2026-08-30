#!/usr/bin/env python3
"""
Overload and recovery: what happens past capacity, and whether it comes back.

The regime the model could not express until queues could be bounded and things
could be dropped. Until then a backlog could only grow, so "what happens under
stress" had no answer at all -- and stress is arguably the most decision-relevant
regime for EIP-8288, because a mempool that degrades gracefully and one that
collapses look identical at low load.

Two experiments.

**The load sweep** raises offered load through the physical ceiling and reports
what breaks first. `offered / ceiling` is the honest x-axis: bytes offered over
what the uplinks could carry in that time. Below 1 nothing should be lost; above
it something must be, and the question is what.

**The recovery run** offers a burst well past capacity, stops, and watches. The
number that matters is how long after the load stops the network is still working
through it -- a queue that drains in one slot and a queue that never drains are
very different failure modes, and only the second one is a design problem.

    python -m bench.overload
    python -m bench.overload --nodes 60 --seeds 3
    python -m bench.overload --policy announce_fetch --request-timeout 1500

Every figure here is a **wire** or a **drop**, never an offered byte presented as
traffic: offered bytes above the ceiling are the definition of overload, not a
measurement of what crossed.
"""

import argparse
import statistics
import sys

from bench.common import (emit_json_record, mean_path, print_table_header,
                          random_regular_overlay, workload_parameters)
from starksim.arena import KIND_PRIVACY
from starksim.costs import CostTable
from starksim.netsim import RELAY_ANNOUNCE_FETCH, RELAY_FLOOD, NetworkSim
from starksim.network import SizeModel, bytes_per_ms


def one_run(nodes, degree, uplink_mbps, latency, policy, spacing, window,
            drain, egress_bytes, cpu_jobs, request_timeout, seed,
            return_sim=False):
    """Offer load for `window`, then watch for `drain` with nothing new born."""
    overlay = random_regular_overlay(nodes, degree, latency, seed)
    sim = NetworkSim(overlay, seed=seed, capacity=bytes_per_ms(uplink_mbps),
                     size_model=SizeModel(), relay_policy=policy,
                     costs=CostTable(verify_recv=1.0),
                     egress_queue_bytes=egress_bytes, cpu_queue_jobs=cpu_jobs,
                     request_timeout=request_timeout)
    births = max(1, int(window / spacing))
    for index in range(births):
        sim.schedule_birth(index * spacing, KIND_PRIVACY)

    # Measured at the end of the load, not the end of the run: this is the state
    # the network was driven into, and the drain that follows is the recovery.
    sim.run(until=window)
    at_load = snapshot(sim, nodes, window)

    sim.run(until=window + drain)
    at_end = snapshot(sim, nodes, window + drain)

    ceiling = nodes * bytes_per_ms(uplink_mbps) * window
    result = {
        "births": births,
        "offered_ratio": at_load["offered"] / ceiling if ceiling else 0.0,
        "load": at_load,
        "end": at_end,
        "drained": at_end["queue_bytes"] == 0,
        "residency": mean_residency(sim),
    }
    if return_sim:
        return result, sim
    return result


def snapshot(sim, nodes, now):
    queue_bytes = 0
    for node_id in range(nodes):
        queue_bytes += sim.egress[node_id].queued_weight
    utilisations = []
    for node_id in range(nodes):
        utilisations.append(sim.egress_utilisation(node_id, now))
    return {
        "t": now,
        "offered": sim.total_offered_bytes(),
        "wire": sim.total_wire_bytes(),
        "dropped": sim.dropped_messages,
        "dropped_bytes": sim.dropped_bytes,
        "queue_bytes": queue_bytes,
        "max_queue": sim.max_egress_depth(),
        "utilisation": statistics.fmean(utilisations),
        "timed_out": sim.requests_timed_out,
        "retried": sim.requests_retried,
    }


def mean_residency(sim):
    values = []
    for obj in sim.arena.objects.values():
        values.append(sim.residency_fraction(obj))
    if not values:
        return 0.0
    return statistics.fmean(values)


def sweep(args, spacings):
    header = (f"{'offered/ceil':>12} {'births':>7} {'wire MB':>9} "
              f"{'drop msg':>9} {'drop MB':>8} {'queue MB':>9} "
              f"{'uplink':>7} {'residency':>10}")
    print_table_header(header)
    for spacing in spacings:
        runs = []
        for seed in range(args.seeds):
            runs.append(one_run(args.nodes, args.degree, args.uplink_mbps,
                                args.latency, args.policy, spacing,
                                args.window, args.drain, args.egress_bytes,
                                args.cpu_jobs, args.request_timeout, seed))
        print(f"{mean_path(runs, 'offered_ratio'):12.2f} "
              f"{mean_path(runs, 'births'):7.0f} "
              f"{mean_path(runs, 'load', 'wire') / 1e6:9.1f} "
              f"{mean_path(runs, 'load', 'dropped'):9.0f} "
              f"{mean_path(runs, 'load', 'dropped_bytes') / 1e6:8.1f} "
              f"{mean_path(runs, 'load', 'queue_bytes') / 1e6:9.1f} "
              f"{mean_path(runs, 'load', 'utilisation'):6.1%} "
              f"{mean_path(runs, 'residency'):10.1%}")


def recovery(args, spacings):
    header = (f"{'offered/ceil':>12} {'queue at load':>14} "
              f"{'queue after drain':>18} {'drained':>8} "
              f"{'wire during drain':>18} {'residency':>10}")
    print_table_header(header)
    for spacing in spacings:
        runs = []
        for seed in range(args.seeds):
            runs.append(one_run(args.nodes, args.degree, args.uplink_mbps,
                                args.latency, args.policy, spacing,
                                args.window, args.drain, args.egress_bytes,
                                args.cpu_jobs, args.request_timeout, seed))
        drained = sum(1 for run in runs if run["drained"])
        during = (mean_path(runs, 'end', 'wire')
                  - mean_path(runs, 'load', 'wire')) / 1e6
        print(f"{mean_path(runs, 'offered_ratio'):12.2f} "
              f"{mean_path(runs, 'load', 'queue_bytes') / 1e6:13.1f}M "
              f"{mean_path(runs, 'end', 'queue_bytes') / 1e6:17.1f}M "
              f"{drained:>4}/{len(runs)} "
              f"{during:17.1f}M "
              f"{mean_path(runs, 'residency'):10.1%}")


def emit_json_runs(args, spacings):
    for spacing in spacings:
        for seed in range(args.seeds):
            summary, sim = one_run(
                args.nodes, args.degree, args.uplink_mbps, args.latency,
                args.policy, spacing, args.window, args.drain,
                args.egress_bytes, args.cpu_jobs, args.request_timeout, seed,
                return_sim=True)
            workload = workload_parameters(args, tps=1000.0 / spacing)
            emit_json_record("overload", workload, seed, sim, summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, default=40)
    parser.add_argument("--degree", type=int, default=6)
    parser.add_argument("--uplink-mbps", type=float, default=25.0)
    parser.add_argument("--latency", type=float, default=100.0)
    parser.add_argument("--policy", default=RELAY_FLOOD,
                        choices=[RELAY_FLOOD, RELAY_ANNOUNCE_FETCH])
    parser.add_argument("--window", type=float, default=8000.0,
                        help="how long load is offered for")
    parser.add_argument("--drain", type=float, default=16000.0,
                        help="how long to watch after the load stops")
    parser.add_argument("--egress-bytes", type=int, default=4_000_000,
                        help="send-buffer bound per node; 0 for unbounded")
    parser.add_argument("--cpu-jobs", type=int, default=0,
                        help="CPU queue bound per node; 0 for unbounded")
    parser.add_argument("--request-timeout", type=float, default=0.0,
                        help="ms before an unanswered request is retried; "
                             "0 for never")
    parser.add_argument("--tps", default="0.625,1.25,2.5,5,10",
                        help="transactions per second offered, one row each")
    parser.add_argument("--seeds", type=int, default=2)
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON record per seed and rate")
    args = parser.parse_args()
    if args.egress_bytes == 0:
        args.egress_bytes = None
    if args.cpu_jobs == 0:
        args.cpu_jobs = None
    if args.request_timeout == 0.0:
        args.request_timeout = None
    # Births are scheduled in milliseconds apart; a rate is what a reader
    # asks for, so the conversion happens here and nowhere else.
    spacings = [1000.0 / float(value) for value in args.tps.split(",")]

    if args.json:
        emit_json_runs(args, spacings)
        return 0

    sizes = SizeModel()
    body = sizes.tx_body + sizes.triple + sizes.stark_witness
    ceiling = args.nodes * bytes_per_ms(args.uplink_mbps) * args.window
    print(f"nodes={args.nodes} degree={args.degree} "
          f"uplink={args.uplink_mbps} Mbit/s latency={args.latency} ms "
          f"policy={args.policy} object={body} B")
    print(f"load window={args.window:.0f} ms  drain={args.drain:.0f} ms  "
          f"egress bound={args.egress_bytes}  cpu bound={args.cpu_jobs}  "
          f"request timeout={args.request_timeout}  seeds={args.seeds}")
    print(f"physical ceiling over the load window: {ceiling / 1e6:.0f} MB")
    print()
    print("load sweep — what breaks first, measured at the end of the load")
    sweep(args, spacings)
    print()
    print("recovery — does the backlog drain once the load stops")
    recovery(args, spacings)
    print()
    print("offered/ceil is offered bytes over what the uplinks could carry in")
    print("the window. Below 1 nothing need be lost; above it something must")
    print("be, and which thing is the result. Residency is the share of nodes")
    print("still holding each object at the end -- reach would be higher, and")
    print("the two are different claims.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
