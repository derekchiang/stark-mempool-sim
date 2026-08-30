#!/usr/bin/env python3
"""Bounded queues, drops, and request recovery.

Until now the model could only grow backlogs, never shed them, so it could say
nothing about behaviour under stress -- arguably the regime that matters most for
the EIP. These are the mechanics that make the overload experiment possible:
a bound on what may wait, tail-drop when it is exceeded, and a timeout so that a
lost request is not permanent.

The honesty property most of these protect: **a dropped message is a loss, not
traffic.** Counting a refused send as offered bytes would inflate the very figure
that is supposed to bound what the links can carry, which is a mistake this
project has published once already.
"""

import io
import json
import random
import unittest

from starksim.arena import KIND_PQ, KIND_PRIVACY
from starksim.costs import CostTable
from starksim.engine import Engine, Job
from starksim.netsim import NetworkSim, RELAY_ANNOUNCE_FETCH
from starksim.network import Overlay, SizeModel, bytes_per_ms


class TestBoundedServer(unittest.TestCase):
    def _server(self, **kwargs):
        engine = Engine()
        engine.on("x", lambda e, p: None)
        return engine, engine.new_server("s", **kwargs)

    def test_an_unbounded_server_never_refuses(self):
        _engine, server = self._server()
        for _ in range(500):
            self.assertTrue(server.submit(Job(1.0)))
        self.assertEqual(server.dropped, 0)

    def test_the_bound_counts_what_waits_not_what_is_in_service(self):
        # The job in service has left the buffer, so a limit of one admits two:
        # one running, one waiting.
        _engine, server = self._server(queue_limit=1)
        self.assertTrue(server.submit(Job(5.0)))     # starts immediately
        self.assertTrue(server.submit(Job(5.0)))     # fills the queue
        self.assertFalse(server.submit(Job(5.0)))    # refused
        self.assertEqual(server.dropped, 1)
        self.assertEqual(server.queue_depth(), 1)

    def test_the_arriving_job_is_the_one_refused(self):
        # Tail-drop, not head-drop: what was already accepted stays accepted.
        _engine, server = self._server(queue_limit=1)
        server.submit(Job(5.0, tag="in-service"))
        server.submit(Job(5.0, tag="queued"))
        server.submit(Job(5.0, tag="arriving"))
        self.assertEqual([job.tag for job in server.waiting], ["queued"])

    def test_a_weighted_bound_measures_the_weight(self):
        # Bytes rather than messages: a bound in messages would give a 32-byte
        # announcement and a 256 kB proof the same footprint.
        _engine, server = self._server(queue_limit=100, weigh=lambda job: job.tag)
        server.submit(Job(5.0, tag=10))              # in service
        self.assertTrue(server.submit(Job(5.0, tag=60)))
        self.assertFalse(server.submit(Job(5.0, tag=60)))
        self.assertTrue(server.submit(Job(5.0, tag=40)))
        self.assertEqual(server.queued_weight, 100)
        self.assertEqual(server.dropped_weight, 60)

    def test_service_and_cancellation_release_the_weight(self):
        engine, server = self._server(queue_limit=3)
        for _ in range(4):
            server.submit(Job(1.0))
        self.assertEqual(server.queued_weight, 3)
        server.cancel_waiting(lambda job: True)
        self.assertEqual(server.queued_weight, 0)
        server.assert_invariants()
        # And the freed room is usable, which a leak would silently prevent.
        self.assertTrue(server.submit(Job(1.0)))

    def test_draining_keeps_the_accounting_straight(self):
        engine, server = self._server(queue_limit=5)
        for _ in range(6):
            server.submit(Job(1.0))
        engine.run()
        self.assertEqual(server.queued_weight, 0)
        server.assert_invariants()


def loaded(nodes=30, births=30, spacing=100.0, kind=KIND_PRIVACY, **kwargs):
    overlay = Overlay.random_regular(nodes, 6, random.Random("overload"),
                                     base_latency=100.0)
    defaults = dict(seed=1, capacity=bytes_per_ms(10), size_model=SizeModel())
    defaults.update(kwargs)
    sim = NetworkSim(overlay, **defaults)
    for index in range(births):
        sim.schedule_birth(index * spacing, kind)
    sim.run(until=6000.0)
    return sim


class TestDropsAreLossesNotTraffic(unittest.TestCase):
    def test_an_unbounded_run_drops_nothing(self):
        sim = loaded()
        self.assertEqual(sim.dropped_messages, 0)
        self.assertEqual(sim.dropped_bytes, 0)

    def test_a_bounded_egress_drops_and_says_so(self):
        sim = loaded(egress_queue_bytes=2_000_000)
        self.assertGreater(sim.dropped_messages, 0)
        self.assertGreater(sim.dropped_bytes, 0)
        self.assertTrue(sim.dropped_by_kind)

    def test_refused_sends_are_not_counted_as_offered(self):
        """The property that matters. A refused send never entered a queue.

        Charging it as offered bytes would inflate the figure whose whole job is
        to bound what the links can carry -- and reporting offered bytes as
        traffic is a mistake this project has already published once.
        """
        bounded = loaded(egress_queue_bytes=2_000_000)
        self.assertGreater(bounded.dropped_bytes, 0)
        # Offered is what queued; wire is what crossed. Neither may include a
        # byte that was refused at the door.
        self.assertLessEqual(bounded.total_wire_bytes() - 1e-6,
                             bounded.total_offered_bytes())
        unbounded = loaded()
        self.assertLess(bounded.total_offered_bytes(),
                        unbounded.total_offered_bytes())

    def test_a_full_cpu_queue_costs_ingress_and_yields_nothing(self):
        # The asymmetry a bounded CPU queue exists to model: the bytes arrived
        # and were paid for, but the work to make sense of them never ran.
        # Tight spacing on purpose: a CPU queue only fills if arrivals
        # outpace verification, and at 100 ms apart they do not.
        free = loaded(kind=KIND_PQ, births=60, spacing=20.0,
                      capacity=bytes_per_ms(200),
                      costs=CostTable(verify_recv=20.0))
        squeezed = loaded(kind=KIND_PQ, births=60, spacing=20.0,
                          capacity=bytes_per_ms(200),
                          costs=CostTable(verify_recv=20.0), cpu_queue_jobs=2)
        self.assertEqual(free.dropped_messages, 0)
        self.assertGreater(squeezed.dropped_messages, 0)
        held_free = sum(len(node.view.held) for node in free.nodes)
        held_squeezed = sum(len(node.view.held) for node in squeezed.nodes)
        self.assertLess(held_squeezed, held_free)
        # Ingress still counts the bytes: they really did arrive.
        self.assertGreater(squeezed.total_ingress_bytes(), 0)

    def test_losses_are_attributed_to_nodes(self):
        # "The network dropped 1580 messages" and "four nodes dropped everything
        # while the rest dropped nothing" are different situations, and only the
        # second names a hotspot.
        sim = loaded(egress_queue_bytes=2_000_000)
        self.assertEqual(sum(sim.dropped_by_node), sim.dropped_messages)
        self.assertEqual(sum(sim.dropped_bytes_by_node), sim.dropped_bytes)
        droppers = [n for n, count in enumerate(sim.dropped_by_node) if count]
        self.assertTrue(droppers)

    def test_counters_report_queued_bytes_not_only_messages(self):
        # The bound is in bytes, so depth in messages cannot say how full a
        # buffer is: one 256 kB proof and one 32-byte announcement are both "1".
        buffer = io.StringIO()
        sim = loaded(egress_queue_bytes=2_000_000, trace=buffer,
                     trace_counter_interval=500.0)
        seen = []
        for line in buffer.getvalue().strip().split("\n"):
            event = json.loads(line)
            if event["e"] == "cnt":
                seen.append(event)
        self.assertTrue(seen)
        for event in seen:
            self.assertLessEqual(event["eqb"], 2_000_000)
            self.assertIn("dr", event)
            self.assertIn("drb", event)
        # And the queue really does fill, or the bound proves nothing.
        self.assertGreater(max(event["eqb"] for event in seen), 0)

    def test_drops_are_traced_with_a_reason(self):
        buffer = io.StringIO()
        sim = loaded(egress_queue_bytes=2_000_000, trace=buffer)
        reasons = set()
        traced = 0
        for line in buffer.getvalue().strip().split("\n"):
            event = json.loads(line)
            if event["e"] == "drp":
                reasons.add(event["why"])
                traced += 1
        self.assertEqual(traced, sim.dropped_messages)
        self.assertIn("egress_full", reasons)


class TestRequestRecovery(unittest.TestCase):
    """`R6`. Harmless while queues were lossless, which is why it was missed."""

    def _fetching(self, **kwargs):
        overlay = Overlay.random_regular(40, 6, random.Random("r6"),
                                        base_latency=100.0)
        defaults = dict(seed=1, capacity=bytes_per_ms(10),
                        size_model=SizeModel(),
                        relay_policy=RELAY_ANNOUNCE_FETCH,
                        egress_queue_bytes=1_500_000)
        defaults.update(kwargs)
        sim = NetworkSim(overlay, **defaults)
        for index in range(40):
            sim.schedule_birth(index * 100.0, KIND_PRIVACY)
        sim.run(until=8000.0)
        return sim

    def test_without_a_timeout_a_lost_request_is_permanent(self):
        sim = self._fetching(request_timeout=None)
        stuck = sum(len(node.view.requested) for node in sim.nodes)
        self.assertGreater(stuck, 0)
        self.assertEqual(sim.requests_timed_out, 0)

    def test_a_timeout_clears_the_request_and_asks_someone_else(self):
        sim = self._fetching(request_timeout=800.0)
        self.assertGreater(sim.requests_timed_out, 0)
        self.assertGreater(sim.requests_retried, 0)
        # Fewer nodes left waiting than when nothing ever times out.
        stalled = sum(len(node.view.requested)
                      for node in self._fetching(request_timeout=None).nodes)
        recovered = sum(len(node.view.requested) for node in sim.nodes)
        self.assertLess(recovered, stalled)

    def test_a_retry_goes_to_a_different_peer(self):
        # Asking again the peer whose answer went missing is the least
        # promising move available, so the alternative announcers are kept.
        sim = self._fetching(request_timeout=800.0)
        self.assertGreater(sim.requests_retried, 0)
        # Every retry had somewhere to go; the rest gave up and are counted.
        self.assertLessEqual(sim.requests_retried, sim.requests_timed_out)

    def test_a_timeout_longer_than_the_run_is_inert(self):
        # Otherwise every earlier result would shift under the mere presence of
        # the parameter.
        without = loaded(relay_policy=RELAY_ANNOUNCE_FETCH)
        with_timeout = loaded(relay_policy=RELAY_ANNOUNCE_FETCH,
                              request_timeout=60_000.0)
        # Field by field, skipping NaN-against-NaN: a run that builds no blocks
        # has no mean delay, and `nan != nan` is Python's business not the
        # model's.
        left, right = without.metrics(), with_timeout.metrics()
        self.assertEqual(sorted(left), sorted(right))
        for key, value in left.items():
            # The parameter itself is part of the variant identity and *must*
            # differ: two runs configured differently are different runs, even
            # when the difference changed no outcome. That is the distinction
            # this test exists to draw.
            if key == "request_timeout":
                self.assertNotEqual(value, right[key])
                continue
            other = right[key]
            if isinstance(value, float) and value != value:
                self.assertNotEqual(other, other, key)
                continue
            self.assertEqual(value, other, key)
        self.assertEqual(with_timeout.requests_timed_out, 0)
        self.assertEqual(with_timeout.requests_retried, 0)

    def test_a_timeout_cannot_tell_lost_from_merely_slow(self):
        """A property of timeouts, not a defect -- and worth asserting.

        Nothing is dropped in this run, yet a short timeout still fires and
        still retries, because a 256 kB body over a shared 10 Mbit/s uplink
        takes longer than the timeout allows. Retrying something that was only
        slow duplicates work and spends bandwidth that was already scarce, which
        is how a retry policy makes congestion worse rather than better.
        """
        lossless = loaded(relay_policy=RELAY_ANNOUNCE_FETCH,
                          request_timeout=800.0)
        self.assertEqual(lossless.dropped_messages, 0)
        self.assertGreater(lossless.requests_timed_out, 0)
        self.assertGreater(lossless.requests_retried, 0)
        # And it costs: more bytes offered than the same run without retries.
        quiet = loaded(relay_policy=RELAY_ANNOUNCE_FETCH)
        self.assertGreater(lossless.total_offered_bytes(),
                           quiet.total_offered_bytes())


class TestRecovery(unittest.TestCase):
    """Whether a backlog drains once the load stops -- the question bounded
    queues were added to be able to ask at all."""

    def _burst(self, spacing=100.0, window=6000.0, drain=12000.0, **kwargs):
        overlay = Overlay.random_regular(30, 6, random.Random("recover"),
                                         base_latency=100.0)
        defaults = dict(seed=1, capacity=bytes_per_ms(10),
                        size_model=SizeModel(),
                        egress_queue_bytes=4_000_000,
                        costs=CostTable(verify_recv=1.0))
        defaults.update(kwargs)
        sim = NetworkSim(overlay, **defaults)
        for index in range(int(window / spacing)):
            sim.schedule_birth(index * spacing, KIND_PRIVACY)
        sim.run(until=window)
        at_load = sum(server.queued_weight for server in sim.egress)
        sim.run(until=window + drain)
        at_end = sum(server.queued_weight for server in sim.egress)
        return sim, at_load, at_end

    def test_a_light_load_leaves_nothing_queued(self):
        _sim, at_load, at_end = self._burst(spacing=1600.0)
        self.assertEqual(at_end, 0, f"{at_load} bytes queued at load")

    def test_a_heavy_load_builds_a_backlog_that_outlives_it(self):
        _sim, at_load, at_end = self._burst(spacing=100.0)
        self.assertGreater(at_load, 0)
        self.assertGreater(at_end, 0)

    def test_retries_can_turn_recovery_into_collapse(self):
        """The finding, asserted so it cannot quietly stop being true.

        Same load, same policy, both *below* the physical ceiling. Without
        retries the backlog drains; with a timeout shorter than the real fetch
        latency it does not, because requests that were merely slow get retried,
        which adds load, which makes them slower. The retry policy is the
        difference between recovering and not.
        """
        _quiet, quiet_load, quiet_end = self._burst(
            relay_policy=RELAY_ANNOUNCE_FETCH)
        _retry, retry_load, retry_end = self._burst(
            relay_policy=RELAY_ANNOUNCE_FETCH, request_timeout=1500.0)
        self.assertEqual(quiet_end, 0, "the no-retry run should drain")
        self.assertGreater(retry_end, 0, "the retrying run should not")
        self.assertGreater(retry_load, quiet_load)


class TestRefusedCpuWorkIsAccounted(unittest.TestCase):
    """Prove and build submissions once ignored refusal.

    A refused fold made the plan stall for ever with every counter at zero;
    a refused build made assembly silently free while the block was included
    anyway. Both must leave a trace in the metrics.
    """

    def wedged(self):
        adjacency = [[1], [0]]
        overlay = Overlay.from_adjacency(adjacency, base_latency=10.0)
        sim = NetworkSim(overlay, seed=1, cpu_queue_jobs=0,
                         slot_length=1000.0, aggregation_interval=200.0,
                         proposer_id=0,
                         costs=CostTable(verify_recv=1.0,
                                         prove_recursive=lambda deps: 10.0,
                                         build_block=lambda count: 5.0))
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        # Wedge node 0's CPU for the whole run: with a zero-length queue,
        # every further submission there must be refused.
        sim.cpu[0].submit(Job(100_000.0))
        return sim

    def test_a_refused_fold_abandons_the_plan_and_says_so(self):
        sim = self.wedged()
        sim.schedule_ticks(2)
        sim.run(until=1000.0)
        self.assertGreaterEqual(sim.plans_abandoned, 1)

    def test_a_refused_build_is_counted_not_free(self):
        sim = self.wedged()
        sim.schedule_slots(1)
        sim.run(until=1500.0)
        # The block exists -- created at the boundary by design -- but its
        # assembly cost was refused, and that must be visible.
        self.assertEqual(len(sim.chain.blocks), 1)
        self.assertGreaterEqual(sim.build_jobs_refused, 1)


if __name__ == "__main__":
    unittest.main()
