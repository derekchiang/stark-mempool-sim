#!/usr/bin/env python3
"""The wrapper-economy levers, and the relay policy that never ships a witness.

The measured motivation: under the default cadence, wrappers -- not raw
objects -- are 73-98% of all offered bytes, because a recursive proof is a
constant 256 kB and every node sends one to every peer every tick whether or
not anything changed. These tests pin down the three levers that spend less:
wrapper_only (the witness never leaves its entry node), skip_unchanged (stay
silent toward a peer that verifiably holds this exact proof), and proof_by_ref
(send such a peer 32 bytes instead of 256 kB).

Every lever rests on beliefs formed at *delivery*, so the tests also pin the
failure edges: an undelivered wrapper must not suppress the retransmission,
and a reference to bytes the receiver abandoned must not become evidence.
"""

import unittest

from starksim.arena import KIND_PQ
from starksim.costs import CostTable
from starksim.netsim import NetworkSim
from starksim.network import Overlay, bytes_per_ms
from starksim.payload import SizeBreakdown, WireMessage, WrapperCore, \
    WrapperPayload

from tests.support import line_overlay


def line(node_count, **kwargs):
    overlay = line_overlay(node_count)
    defaults = dict(seed=1, aggregation_interval=200.0, slot_length=1000.0,
                    costs=CostTable(verify_recv=1.0,
                                    prove_recursive=lambda deps: 10.0))
    defaults.update(kwargs)
    return NetworkSim(overlay, **defaults)


class TestWrapperOnly(unittest.TestCase):
    def test_no_raw_object_ever_touches_the_wire(self):
        sim = line(4, relay_policy="wrapper_only")
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_ticks(6)
        sim.run()
        for kind in ("object", "announce", "want"):
            self.assertNotIn(kind, sim.offered_messages_by_kind,
                             f"{kind} messages under wrapper_only")
        self.assertEqual(sim.nodes[0].view.relayed, 0)

    def test_claims_and_bodies_still_spread_by_reaggregation(self):
        sim = line(4, relay_policy="wrapper_only")
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_ticks(8)
        sim.run()
        bit = 0b1
        # Two hops from the entry: reachable only if node 1 folded node 0's
        # aggregate and node 2 admitted node 1's wrapper -- re-aggregation
        # carrying the claim one tick per hop.
        two_hops = sim.nodes[2]
        claims = 0
        for object_id in two_hops.view.held:
            obj = sim.arena.objects.get(object_id)
            if obj is not None:
                claims |= sim.arena.claim(obj)
        self.assertTrue(claims & bit, "the claim never crossed two hops")
        self.assertTrue(two_hops.view.bodies & bit,
                        "the body never crossed two hops")

    def test_unknown_policy_still_rejected(self):
        with self.assertRaises(ValueError):
            line(2, relay_policy="sideways")


class TestSkipUnchanged(unittest.TestCase):
    def test_a_peer_that_has_the_proof_is_left_in_peace(self):
        sim = line(2, wrapper_economy="skip_unchanged")
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_ticks(6)
        sim.run()
        self.assertGreater(sim.wrapper_sends_suppressed, 0)

    def test_an_undelivered_wrapper_suppresses_nothing(self):
        # A wrapper still crawling down a starved uplink has formed no belief,
        # so the next tick must send again rather than starve the peer: the
        # suppression is belief-based exactly so that this cannot happen.
        slow = bytes_per_ms(0.001)
        sim = line(2, wrapper_economy="skip_unchanged", capacity=slow)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_ticks(3)
        sim.run(until=800.0)
        self.assertEqual(sim.wrapper_sends_suppressed, 0)

    def test_a_peer_with_the_body_but_not_the_proof_still_gets_the_wrapper(self):
        # Under flood the body arrives as a raw object, so the first wrapper
        # toward that peer carries no bodies at all -- but the peer has never
        # seen the *proof*, and staying silent on "nothing new to send" rather
        # than "peer holds this proof" would starve it of the claim for ever.
        # Aligned ticks, deliberately: they make both nodes fold their own
        # aggregate at the same instant, so each provably holds a proof the
        # other lacks. With spread ticks one node folds first and the other
        # side really does have nothing to add -- suppression is then correct
        # and the premise of this test never arises.
        sim = line(2, wrapper_economy="skip_unchanged", relay_policy="flood",
                   aggregation_trigger="aligned")
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_ticks(6)
        sim.run()
        # `verified_proofs`, not `held`, and any creator: only a wrapper
        # delivered *to this node* can put something there -- its own folds do
        # not, and an admitted peer aggregate leaves `held` again the moment
        # it is folded onward. (Creator is no discriminator either: with
        # spread ticks whichever node folds first makes the proof, and the
        # other side re-emits it unchanged.)
        self.assertGreater(len(sim.nodes[1].view.verified_proofs), 0,
                           "no wrapper ever reached the peer")

    def test_the_full_economy_never_suppresses(self):
        sim = line(2, wrapper_economy="full")
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_ticks(6)
        sim.run()
        self.assertEqual(sim.wrapper_sends_suppressed, 0)

    def test_unknown_economy_rejected_and_variant_carries_it(self):
        with self.assertRaises(ValueError):
            line(2, wrapper_economy="thrifty")
        sim = line(2, wrapper_economy="lean")
        self.assertEqual(sim.variant()["wrapper_economy"], "lean")


class TestBeliefFormsOnRetentionOnly(unittest.TestCase):
    def test_a_cpu_refused_wrapper_forms_no_proof_belief(self):
        # Beliefs recorded at delivery, before the receiver's CPU decided
        # whether to keep the wrapper, became stale the moment the queue
        # refused it -- and a stale belief never self-heals, because
        # skip_unchanged then suppresses every retransmission. The belief must
        # form only when the receiver retains the proof; the bodies, which
        # need no verification, are kept regardless.
        from starksim.engine import Job
        sim = line(2, wrapper_economy="lean", cpu_queue_jobs=0,
                   costs=CostTable(verify_recv=5.0))
        leaf = sim.arena.new_leaf(KIND_PQ, 0.0)
        aggregate = sim.arena.new_aggregate([leaf], 1.0, 1, claimed=0b1)
        core = WrapperCore(wrapper_id=1, proof_object_id=aggregate.id,
                           creator=1, tick=0, mode=1, tx_mask=0b1,
                           dep_count=1)
        message = WireMessage(sender=1, receiver=0,
                              payload=WrapperPayload(core=core, body_mask=0b1,
                                                     size=SizeBreakdown()))
        # Wedge the receiver's CPU so the bounded queue must refuse.
        sim.cpu[0].submit(Job(10_000.0))
        sim._on_deliver(sim.engine, message)
        # No proof belief at the sender: the next tick will resend in full.
        self.assertFalse(sim.nodes[1].peer_has_proof(0, aggregate.id))
        # The bodies arrived and are kept, so the sender's body belief --
        # which did form at delivery -- stays true.
        self.assertTrue(sim.nodes[0].view.bodies & 0b1)
        self.assertNotIn(aggregate.id, sim.nodes[0].view.verifying_proofs)
        # And the refusal's bytes reach the *global* dropped total, not only
        # the per-node one.
        self.assertEqual(sim.dropped_bytes, message.size_bytes())

    def test_an_accepted_wrapper_does_form_the_belief(self):
        sim = line(2, wrapper_economy="lean",
                   costs=CostTable(verify_recv=0.0))
        leaf = sim.arena.new_leaf(KIND_PQ, 0.0)
        aggregate = sim.arena.new_aggregate([leaf], 1.0, 1, claimed=0b1)
        core = WrapperCore(wrapper_id=1, proof_object_id=aggregate.id,
                           creator=1, tick=0, mode=1, tx_mask=0b1,
                           dep_count=1)
        sim._on_deliver(sim.engine, WireMessage(
            sender=1, receiver=0,
            payload=WrapperPayload(core=core, body_mask=0b1,
                                   size=SizeBreakdown())))
        self.assertTrue(sim.nodes[1].peer_has_proof(0, aggregate.id))


class TestProofByRef(unittest.TestCase):
    def test_a_known_proof_travels_as_a_hash(self):
        by_ref = line(2, wrapper_economy="proof_by_ref")
        full = line(2, wrapper_economy="full")
        for sim in (by_ref, full):
            sim.schedule_birth(0.0, KIND_PQ, node_id=0)
            sim.schedule_ticks(6)
            sim.run()
        ref_bytes = by_ref.offered_bytes_by_kind.get("wrapper", 0)
        full_bytes = full.offered_bytes_by_kind.get("wrapper", 0)
        # Same ticks, same claims; after the first delivery every further copy
        # of that proof is 32 bytes instead of 256 kB, so the totals must be
        # far apart -- not merely different.
        self.assertLess(ref_bytes, full_bytes / 2)

    def test_one_delivery_teaches_both_ends(self):
        # The sender learns the peer now holds the proof; the receiver learns
        # the sender must hold what it just sent. The receipt direction is the
        # one re-emission needs -- without it a node answers its peer's own
        # proof with a full 256 kB copy of it.
        sim = line(2, wrapper_economy="proof_by_ref")
        leaf = sim.arena.new_leaf(KIND_PQ, 0.0)
        aggregate = sim.arena.new_aggregate([leaf], 1.0, 1, claimed=0b1)
        core = WrapperCore(wrapper_id=1, proof_object_id=aggregate.id,
                           creator=1, tick=0, mode=1, tx_mask=0b1,
                           dep_count=1)
        message = WireMessage(sender=1, receiver=0,
                              payload=WrapperPayload(core=core, body_mask=0b1,
                                                     size=SizeBreakdown()))
        sim._on_deliver(sim.engine, message)
        self.assertTrue(sim.nodes[1].peer_has_proof(0, aggregate.id),
                        "delivery direction")
        self.assertTrue(sim.nodes[0].peer_has_proof(1, aggregate.id),
                        "receipt direction")

    def test_a_reference_to_bytes_never_held_is_not_evidence(self):
        sim = line(2, wrapper_economy="proof_by_ref")
        leaf = sim.arena.new_leaf(KIND_PQ, 0.0)
        aggregate = sim.arena.new_aggregate([leaf], 1.0, 1, claimed=0b1)
        core = WrapperCore(wrapper_id=1, proof_object_id=aggregate.id,
                           creator=1, tick=0, mode=1, tx_mask=0b1,
                           dep_count=1)
        message = WireMessage(sender=1, receiver=0,
                              payload=WrapperPayload(core=core, body_mask=0b1,
                                                     size=SizeBreakdown(),
                                                     proof_by_ref=True))
        sim._deliver_wrapper(message)
        node = sim.nodes[0]
        self.assertEqual(sim.wrapper_refs_unresolved, 1)
        self.assertNotIn(aggregate.id, node.view.held)
        self.assertNotIn(aggregate.id, node.view.verifying_proofs)
        self.assertNotIn(aggregate.id, node.view.verified_proofs)
        # The bodies were physically in the message and the sender's belief
        # formed at delivery, so they are kept even though the proof was not.
        self.assertTrue(node.view.bodies & 0b1)


if __name__ == "__main__":
    unittest.main()
