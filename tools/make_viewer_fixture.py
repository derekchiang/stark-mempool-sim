#!/usr/bin/env python3
"""Write a trace plus the simulator's own answers, for tools/check_viewer.mjs.

The fixture deliberately exercises every event kind the viewer reduces: births
and admissions, all three departure reasons, followed-transaction knowledge
transitions, per-kind byte counters, blocks under a rotating proposer, drops
under a bounded send buffer, and a finalised lifecycle. A viewer check against a
trace missing any of those would pass without covering the reducer that handles
it -- which is why the send buffer is deliberately too small.
"""

import io
import json
import os
import random
import sys

from starksim.arena import KIND_PQ
from starksim.costs import CostTable
from starksim.netsim import NetworkSim
from starksim.network import Overlay, bytes_per_ms


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("usage: python3 -m tools.make_viewer_fixture <directory>")
        return 2
    target = sys.argv[1]
    os.makedirs(target, exist_ok=True)

    stream = io.StringIO()
    overlay = Overlay.random_regular(10, 4, random.Random("viewer"),
                                     base_latency=60.0)
    sim = NetworkSim(overlay, seed=1, capacity=bytes_per_ms(50), trace=stream,
                     costs=CostTable(verify_recv=2.0,
                                     prove_recursive=lambda deps: 20.0),
                     aggregation_interval=250.0, slot_length=500.0,
                     candidate_limit=5, trace_follow_rate=0.4,
                     trace_counter_interval=250.0, proposer_mode="rotating",
                     egress_queue_bytes=400_000)
    for index in range(16):
        sim.schedule_birth(index * 40.0, KIND_PQ, lifetime=1500.0)
    sim.schedule_ticks(10)
    sim.schedule_slots(5)
    sim.run()
    sim.finish_trace()

    with open(os.path.join(target, "trace.jsonl"), "w") as handle:
        handle.write(stream.getvalue())

    residency = {}
    for object_id, obj in sim.arena.objects.items():
        if obj.leaf_index is not None:
            residency[str(object_id)] = sim.residency_fraction(obj)
    kinds = set()
    for table in sim.sent_kind:
        kinds |= set(table)
    sent_total = {}
    for kind in kinds:
        sent_total[kind] = sum(table.get(kind, 0) for table in sim.sent_kind)
    expected = {
        "nodes": len(sim.nodes),
        "included": sim.chain.included_count(),
        "residency": residency,
        "sent_kind_total": sent_total,
        "dropped": sim.dropped_messages,
        "dropped_bytes": sim.dropped_bytes,
        "dropped_by_node": {str(node): count for node, count
                            in enumerate(sim.dropped_by_node) if count},
        "egress_queue_bytes": sim.egress_queue_bytes,
    }
    with open(os.path.join(target, "expected.json"), "w") as handle:
        json.dump(expected, handle)
    print(f"wrote {target}/trace.jsonl and expected.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
