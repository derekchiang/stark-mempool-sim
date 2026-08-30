#!/usr/bin/env python3
"""The CPU's queueing discipline as a policy ([D20] in the plan:
docs/history/networking-plan.md).

One CPU per node stands on the assumption that each job is perfectly
parallelisable by itself, so sequencing beats sharing -- but *which* sequence
is a policy, not a corollary. FIFO gives head-of-line blocking behind a long
fold; priority (verify > build > prove, non-preemptive) is what a real
implementation would do. These tests pin the mechanism, not its consequences:
whether the discipline changes results is the sensitivity sweep's question.
"""

import unittest

from starksim.costs import BUILD_BLOCK, PROVE_RECURSIVE, VERIFY_RECV, \
    CostTable
from starksim.engine import Engine, Job
from starksim.netsim import NetworkSim

from tests.support import make_sim


def served_order(rank):
    """Run one long job, queue three behind it, return completion order."""
    engine = Engine()
    server = engine.new_server("cpu", rank=rank)
    order = []

    def record(job):
        order.append(job.kind)

    server.submit(Job(10.0, on_done=record, kind=PROVE_RECURSIVE))
    # All three arrive while the fold runs, in deliberately worst FIFO order.
    server.submit(Job(1.0, on_done=record, kind=PROVE_RECURSIVE))
    server.submit(Job(1.0, on_done=record, kind=BUILD_BLOCK))
    server.submit(Job(1.0, on_done=record, kind=VERIFY_RECV))
    engine.run()
    return order


RANK = {VERIFY_RECV: 0, BUILD_BLOCK: 1, PROVE_RECURSIVE: 2}


class TestRankedServer(unittest.TestCase):
    def test_fifo_serves_in_arrival_order(self):
        self.assertEqual(served_order(None),
                         [PROVE_RECURSIVE, PROVE_RECURSIVE, BUILD_BLOCK,
                          VERIFY_RECV])

    def test_priority_serves_by_rank_without_preempting(self):
        # The long fold still finishes first -- selection happens only when
        # the server goes idle -- and then rank order takes over.
        self.assertEqual(served_order(lambda job: RANK[job.kind]),
                         [PROVE_RECURSIVE, VERIFY_RECV, BUILD_BLOCK,
                          PROVE_RECURSIVE])

    def test_rank_orders_service_never_admission(self):
        # The documented boundary: with a bounded queue, a full buffer
        # refuses an arriving high-priority job even while a low-priority one
        # waits inside it. Priority is service-order only; changing that is a
        # policy to build deliberately, not an accident to inherit.
        engine = Engine()
        server = engine.new_server("cpu", queue_limit=1,
                                   rank=lambda job: RANK[job.kind])
        order = []
        server.submit(Job(10.0, on_done=lambda j: order.append(j.kind),
                          kind=PROVE_RECURSIVE))
        server.submit(Job(1.0, on_done=lambda j: order.append(j.kind),
                          kind=PROVE_RECURSIVE))
        accepted = server.submit(Job(1.0, kind=VERIFY_RECV))
        self.assertFalse(accepted)
        self.assertEqual(server.dropped, 1)
        engine.run()
        self.assertEqual(order, [PROVE_RECURSIVE, PROVE_RECURSIVE])

    def test_ties_stay_fifo(self):
        engine = Engine()
        server = engine.new_server("cpu", rank=lambda job: 0)
        order = []
        server.submit(Job(1.0, on_done=lambda j: order.append(j.tag),
                          tag="first"))
        server.submit(Job(1.0, on_done=lambda j: order.append(j.tag),
                          tag="second"))
        server.submit(Job(1.0, on_done=lambda j: order.append(j.tag),
                          tag="third"))
        engine.run()
        self.assertEqual(order, ["first", "second", "third"])


class TestTheKnob(unittest.TestCase):
    def two_nodes(self, **kwargs):
        defaults = dict(seed=1, costs=CostTable(verify_recv=1.0))
        defaults.update(kwargs)
        return make_sim(**defaults)

    def test_the_default_is_fifo_and_the_variant_says_so(self):
        sim = self.two_nodes()
        self.assertEqual(sim.variant()["cpu_discipline"], "fifo")
        self.assertIsNone(sim.cpu[0].rank)

    def test_priority_reaches_every_cpu(self):
        sim = self.two_nodes(cpu_discipline="priority")
        self.assertEqual(sim.variant()["cpu_discipline"], "priority")
        self.assertEqual(len(sim.cpu), sim.overlay.node_count)
        self.assertTrue(all(server.rank is not None for server in sim.cpu))
        # The rank map orders verify ahead of build ahead of prove.
        rank = sim.cpu[0].rank
        verify = rank(Job(1.0, kind=VERIFY_RECV))
        build = rank(Job(1.0, kind=BUILD_BLOCK))
        prove = rank(Job(1.0, kind=PROVE_RECURSIVE))
        self.assertLess(verify, build)
        self.assertLess(build, prove)

    def test_an_unknown_discipline_is_rejected(self):
        with self.assertRaises(ValueError):
            self.two_nodes(cpu_discipline="fair")


if __name__ == "__main__":
    unittest.main()
