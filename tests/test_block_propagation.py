#!/usr/bin/env python3
"""Chain news that takes time to arrive, and what it costs.

Under instant notification every node's knowledge advances with the chain, so
the per-node discard machinery is exercised without ever diverging. This is the
configuration it exists for: a block is a message, nodes learn at different
times, and a node that has not yet heard keeps proving what the block already
settled.
"""

import unittest

from starksim.arena import KIND_PQ
from starksim.costs import CostTable
from starksim.netsim import NetworkSim
from starksim.network import bytes_per_ms

from tests.support import line_overlay


def line(node_count, **kwargs):
    overlay = line_overlay(node_count, base_latency=100.0)
    defaults = dict(seed=1, capacity=bytes_per_ms(1000), proposer_id=0,
                    aggregation_interval=250.0, slot_length=500.0,
                    costs=CostTable(prove_recursive=lambda deps: 20.0))
    defaults.update(kwargs)
    return NetworkSim(overlay, **defaults)


def loaded(sim, count=30, ticks=16, slots=8):
    for index in range(count):
        sim.schedule_birth(index * 30.0, KIND_PQ)
    sim.schedule_ticks(ticks)
    sim.schedule_slots(slots)
    sim.run()
    return sim


class TestInstantSendsNothing(unittest.TestCase):
    def test_no_block_messages_under_instant_notification(self):
        sim = loaded(line(6, chain_notify="instant"))
        self.assertEqual(sim.block_notices, 0)
        self.assertNotIn("block", sim.offered_messages_by_kind)

    def test_stale_work_is_unavailable_not_zero(self):
        # A structural zero reported as a measurement would say the effect does
        # not happen, when the model simply cannot express it.
        sim = loaded(line(6, chain_notify="instant"))
        self.assertFalse(sim.divergence_measurable())
        self.assertEqual(sim.metrics()["stale_deps_proved"], -1)


class TestBlockNotices(unittest.TestCase):
    def test_a_block_is_a_message_that_floods(self):
        sim = loaded(line(6, chain_notify="overlay"))
        self.assertGreater(sim.block_notices, 0)
        self.assertIn("block", sim.offered_messages_by_kind)
        # Every node learns of every block, eventually.
        for node in sim.nodes:
            self.assertEqual(len(node.view.seen_blocks), len(sim.chain.blocks))

    def test_a_notice_is_not_relayed_twice(self):
        sim = loaded(line(4, chain_notify="overlay"))
        # On a line, each notice crosses each edge once in each useful
        # direction; duplicates are counted, not silently dropped.
        self.assertEqual(sim.block_notices,
                         len(sim.chain.blocks) * (len(sim.nodes) - 1))

    def test_notices_share_the_proposer_uplink(self):
        # B4: it is the same NIC, and hiding that would make block propagation
        # free exactly where it competes with wrappers.
        sim = loaded(line(5, chain_notify="overlay"))
        self.assertGreater(sim.offered_bytes_by_kind.get("block", 0), 0)
        self.assertGreater(sim.egress[0].served, 0)

    def test_a_notice_costs_a_proof_plus_a_hash_per_transaction(self):
        sim = line(3, chain_notify="overlay")
        sizes = sim.size_model
        payload = sizes.block(0, 1, 0b111)
        self.assertEqual(payload.size.proof, sizes.recursive_proof)
        self.assertEqual(payload.size.hashes, 3 * sizes.hash_ref)
        self.assertEqual(payload.size.total(),
                         sizes.recursive_proof + 3 * sizes.hash_ref)


class TestLearningLate(unittest.TestCase):
    def test_the_proposer_knows_at_once_and_wastes_nothing(self):
        sim = loaded(line(9, chain_notify="overlay"))
        self.assertEqual(sim.stale_by_node[0], 0)
        self.assertGreater(sum(sim.stale_by_node), 0)

    def test_a_node_that_has_not_heard_still_believes_a_tx_is_pending(self):
        # The whole point: pruning happens on learning, not on the block being
        # built. Until the notice arrives the far node's view is out of date.
        sim = line(3, chain_notify="overlay", slot_length=200.0)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_slots(1)
        sim.run(until=210.0)                # block built, notice in flight
        self.assertEqual(sim.chain.included_count(), 1)
        self.assertEqual(sim.nodes[0].known_discarded(), 0b1)   # proposer knows
        self.assertEqual(sim.nodes[2].known_discarded(), 0)     # two hops away
        sim.run()
        self.assertEqual(sim.nodes[2].known_discarded(), 0b1)   # now it does

    def test_stale_work_grows_with_distance_from_the_proposer(self):
        """The falsifiable form of the criterion.

        A latency sweep will not do: raising latency changes both how late the
        news arrives *and* how much a node has ever received to prove, and the
        total is not monotonic in latency even though the mechanism is. Distance
        from the proposer isolates lateness on a fixed topology.
        """
        sim = loaded(line(9, chain_notify="overlay"))
        distance = sim.hop_distance_from(0)
        near = []
        far = []
        for node_id, hops in enumerate(distance):
            if hops == 0:
                continue
            if hops <= 4:
                near.append(sim.stale_by_node[node_id])
            else:
                far.append(sim.stale_by_node[node_id])
        self.assertGreater(sum(far) / len(far), sum(near) / len(near))

    def test_a_wrapper_over_settled_work_is_abandoned_rather_than_sent(self):
        # Once the news catches up, a proof whose claim is entirely settled is
        # not emitted: the work is already spent, but the bytes need not be.
        sim = loaded(line(6, chain_notify="overlay"))
        # This fixture creates stale proving work but catches up before emit.
        self.assertGreater(sim.stale_deps_proved, 0)
        self.assertEqual(sim.stale_proofs, 0)
        self.assertEqual(sim.metrics()["stale_deps_proved"],
                         sim.stale_deps_proved)


if __name__ == "__main__":
    unittest.main()
