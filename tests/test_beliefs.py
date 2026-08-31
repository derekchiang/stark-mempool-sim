#!/usr/bin/env python3
"""Per-peer body beliefs, and receiving a wrapper that carries a proof plus a
subset of bodies.

Both exist for the EIP's hash substitution: a sender may replace a transaction
body with a hash when the peer already holds it, which needs a belief per peer
per transaction, and a receiver may get the same proof twice with different
bodies attached, which breaks deduplication by identity alone.
"""

import random
import unittest

from starksim.arena import KIND_PQ
from starksim.netsim import RELAY_ANNOUNCE_FETCH, RELAY_FLOOD, NetworkSim
from starksim.network import Overlay
from starksim.node import PeerBodyBeliefs

from tests.support import make_node, two_node_overlay


class TestPeerBodyBeliefs(unittest.TestCase):
    def test_nothing_is_believed_held_to_begin_with(self):
        beliefs = PeerBodyBeliefs((1, 2))
        self.assertEqual(beliefs.believes_held(1, 0b1111), 0)
        self.assertEqual(beliefs.missing_for(1, 0b1111), 0b1111)

    def test_beliefs_are_per_peer(self):
        beliefs = PeerBodyBeliefs((1, 2))
        beliefs.confirm_delivery(1, 0b0011)
        self.assertEqual(beliefs.missing_for(1, 0b1111), 0b1100)
        self.assertEqual(beliefs.missing_for(2, 0b1111), 0b1111)

    def test_confirming_reports_only_what_was_new(self):
        beliefs = PeerBodyBeliefs((1,))
        self.assertEqual(beliefs.confirm_delivery(1, 0b0110), 0b0110)
        self.assertEqual(beliefs.confirm_delivery(1, 0b0111), 0b0001)
        self.assertEqual(beliefs.confirm_delivery(1, 0b0111), 0)

    def test_mask_memory_is_instrumented(self):
        # Python integers are ideal for the bit operations but grow with the
        # highest transaction id, per peer. At the target scale that has to be
        # measurable before it is a surprise.
        beliefs = PeerBodyBeliefs((1, 2))
        self.assertEqual(beliefs.mask_bytes(), 0)
        beliefs.confirm_delivery(1, 1 << 80_000)
        self.assertGreater(beliefs.mask_bytes(), 9_000)


class TestBeliefsUpdateOnDeliveryOnly(unittest.TestCase):
    def test_a_belief_forms_when_the_body_arrives_not_when_it_is_queued(self):
        # If beliefs formed at enqueue, a sender could follow a queued body with
        # a hash-only message that overtook it.
        overlay = two_node_overlay(50.0)
        sim = NetworkSim(overlay, seed=1, capacity=1000.0)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        # Queued and being transmitted, but not yet arrived.
        sim.run(until=1.0)
        sender = sim.nodes[0]
        self.assertEqual(sender.bodies_to_send(1, 0b1), 0b1)   # still owed
        sim.run()
        self.assertEqual(sender.bodies_to_send(1, 0b1), 0)     # now believed

    def test_flooding_teaches_every_sender_what_its_peers_hold(self):
        rng = random.Random("beliefs")
        overlay = Overlay.random_regular(12, 4, rng, base_latency=10.0)
        sim = NetworkSim(overlay, seed=1, relay_policy=RELAY_FLOOD)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        # Every delivery is a body, so each sender ends up believing the peers
        # it sent to hold it.
        beliefs = 0
        for node in sim.nodes:
            for peer in node.peers:
                if node.view.peer_bodies.believes_held(peer, 0b1):
                    beliefs += 1
        self.assertGreater(beliefs, 12)

    def test_announcements_teach_nothing_about_bodies(self):
        # An announcement is 32 bytes and carries no body, so it must not make
        # the sender believe the peer now holds one.
        overlay = two_node_overlay(10.0)
        sim = NetworkSim(overlay, seed=1,
                         relay_policy=RELAY_ANNOUNCE_FETCH)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run(until=10.0)          # the announcement has landed
        self.assertEqual(sim.nodes[0].bodies_to_send(1, 0b1), 0b1)
        sim.run()                    # ...and now the body has too
        self.assertEqual(sim.nodes[0].bodies_to_send(1, 0b1), 0)


class TestReceivingAWrapper(unittest.TestCase):
    """Reception is two steps now, because verification costs time.

    Between them the proof is *in flight*: known about, not yet relied on. That
    window is where a second copy carrying different bodies has to merge rather
    than be discarded, and where a proof must not be treated as evidence.
    """

    def test_a_new_proof_with_new_bodies_is_worth_verifying(self):
        node = make_node()
        worth, fresh, upgraded = node.begin_wrapper(proof_id=7, body_mask=0b1010)
        self.assertTrue(worth)
        self.assertEqual(fresh, 0b1010)
        # Not yet evidence, and its bodies are not yet installed.
        self.assertNotIn(7, node.view.verified_proofs)
        self.assertEqual(node.view.bodies, 0)
        self.assertEqual(node.finish_wrapper(7), 0b1010)
        self.assertEqual(node.view.bodies, 0b1010)
        self.assertIn(7, node.view.verified_proofs)

    def test_a_second_copy_in_flight_merges_its_bodies(self):
        # Paying to verify the same proof twice is waste; dropping the second
        # copy would lose the bodies it carried.
        node = make_node()
        node.begin_wrapper(7, 0b0010)
        worth, fresh, upgraded = node.begin_wrapper(7, 0b0110)
        self.assertFalse(worth)              # no second verification
        self.assertEqual(fresh, 0b0100)      # but a body is new
        self.assertEqual(node.view.duplicates, 0)
        self.assertEqual(node.finish_wrapper(7), 0b0110)

    def test_the_same_proof_with_more_bodies_after_verifying(self):
        node = make_node()
        node.begin_wrapper(7, 0b0010)
        node.finish_wrapper(7)
        worth, fresh, upgraded = node.begin_wrapper(7, 0b0110)
        self.assertFalse(worth)
        self.assertEqual(fresh, 0b0100)
        # And the body is actually kept. Asserting only the returned mask let a
        # bug through for four phases: the bodies arrived, were reported fresh
        # rather than duplicate, and were then dropped on the floor, because
        # nothing installed them when the proof needed no second verification.
        self.assertEqual(node.view.bodies, 0b0110)
        self.assertEqual(node.view.duplicates, 0)

    def test_a_different_proof_over_known_bodies_is_still_new_evidence(self):
        node = make_node()
        node.begin_wrapper(7, 0b0011)
        node.finish_wrapper(7)
        worth, fresh, upgraded = node.begin_wrapper(8, 0b0011)
        self.assertTrue(worth)
        self.assertEqual(fresh, 0)
        self.assertEqual(node.view.duplicates, 0)

    def test_neither_new_is_a_duplicate(self):
        node = make_node()
        node.begin_wrapper(7, 0b0011)
        node.finish_wrapper(7)
        worth, fresh, upgraded = node.begin_wrapper(7, 0b0001)
        self.assertFalse(worth)
        self.assertEqual(fresh, 0)
        self.assertEqual(node.view.duplicates, 1)

    def test_a_proof_for_witnesses_in_flight_pays_its_own_verification(self):
        # Witnesses are being checked; the recursive proof for the same claim
        # arrives. Checking a proof is different work from checking
        # witnesses -- whatever a flat table charges for each -- so this copy
        # is worth verifying even though it brings no fresh body, and it is
        # not a duplicate: the claim now carries two pending jobs, and each
        # completion installs only its own evidence.
        node = make_node()
        node.begin_wrapper(7, 0b0001, witness_backed=True)
        self.assertFalse(node.can_resolve_proof(7))
        worth, fresh, upgraded = node.begin_wrapper(7, 0b0001)
        self.assertTrue(worth)
        self.assertEqual(fresh, 0)
        self.assertTrue(upgraded)
        self.assertEqual(node.view.duplicates, 0)
        self.assertTrue(node.can_resolve_proof(7))
        node.finish_wrapper(7, witness_backed=True)
        self.assertNotIn(7, node.view.verified_proofs)
        self.assertTrue(node.can_resolve_proof(7))
        node.finish_wrapper(7)
        self.assertIn(7, node.view.verified_proofs)

    def test_the_upgrade_completes_in_either_order(self):
        node = make_node()
        node.begin_wrapper(7, 0b0001, witness_backed=True)
        node.begin_wrapper(7, 0b0001)
        node.finish_wrapper(7)                       # the proof lands first
        self.assertIn(7, node.view.verified_proofs)
        node.finish_wrapper(7, witness_backed=True)  # witnesses trail in
        self.assertIn(7, node.view.verified_proofs)
        self.assertNotIn(7, node.view.witness_backed)

    def test_a_second_proof_during_an_upgrade_is_a_duplicate(self):
        node = make_node()
        node.begin_wrapper(7, 0b0001, witness_backed=True)
        node.begin_wrapper(7, 0b0001)
        worth, fresh, upgraded = node.begin_wrapper(7, 0b0001)
        self.assertFalse(worth)
        self.assertFalse(upgraded)
        self.assertEqual(node.view.duplicates, 1)

    def test_an_abandoned_upgrade_reverts_to_witness_pending(self):
        # The proof job overflowed: the claim falls back to what the still-
        # running witness job can establish, never to a proof belief.
        node = make_node()
        node.begin_wrapper(7, 0b0001, witness_backed=True)
        node.begin_wrapper(7, 0b0001)
        node.abandon_wrapper(7)
        self.assertFalse(node.can_resolve_proof(7))
        node.finish_wrapper(7, witness_backed=True)
        self.assertNotIn(7, node.view.verified_proofs)
        self.assertIn(7, node.view.witness_backed)

    def test_witnesses_for_a_proof_in_flight_are_a_duplicate(self):
        # The reverse of the upgrade: the pending proof is the stronger
        # evidence, so the witnesses teach nothing.
        node = make_node()
        node.begin_wrapper(7, 0b0001)
        worth, fresh, upgraded = node.begin_wrapper(7, 0b0001,
                                                    witness_backed=True)
        self.assertFalse(worth)
        self.assertFalse(upgraded)
        self.assertEqual(node.view.duplicates, 1)

    def test_abandoning_drops_the_evidence_and_keeps_the_contents(self):
        # The bodies physically arrived and the sender's per-peer belief
        # formed at delivery, so they will never be sent here in full again:
        # dropping them with the proof starved this node of content the
        # network believed it had.
        node = make_node()
        node.begin_wrapper(7, 0b1111)
        node.abandon_wrapper(7)
        self.assertNotIn(7, node.view.verified_proofs)
        self.assertNotIn(7, node.view.verifying_proofs)
        self.assertEqual(node.view.bodies, 0b1111)


if __name__ == "__main__":
    unittest.main()
