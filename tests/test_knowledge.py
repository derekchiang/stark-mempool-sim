#!/usr/bin/env python3
"""The knowledge classifier: what one node knows about one transaction.

Knowledge is four independent predicates -- settlement, evidence, body,
retrieval -- over different identity domains, and several objects covering one
transaction can satisfy several at once. Anything that counts nodes by knowledge
needs a partition instead, so `knowledge_class` projects the predicates onto one
label by precedence.

**These tests state the expected class by hand.** The obvious alternative --
drive a simulation, then check the classifier against the classifier -- verifies
plumbing with the same oracle it is testing and proves nothing about the
classification itself. So the truth table below is written out, one row at a
time, with the state built directly on a `NodeView`.
"""

import unittest

from starksim.arena import Arena, KIND_PQ
from starksim.chain import ChainState, InstantNotifier
from starksim.node import (KNOWLEDGE_BODY_ONLY, KNOWLEDGE_CLASSES,
                           KNOWLEDGE_HELD_AND_BODY, KNOWLEDGE_HELD_NO_BODY,
                           KNOWLEDGE_NONE, KNOWLEDGE_REQUESTED,
                           KNOWLEDGE_SETTLED, KNOWLEDGE_VERIFYING, Node,
                           knowledge_class, knowledge_evidence)


def fixture():
    """A node, an arena with three leaves, and an aggregate over two of them."""
    arena = Arena()
    chain = ChainState()
    leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(3)]
    aggregate = arena.new_aggregate(tuple(leaves[:2]), 0.0, creator=0)
    node = Node(0, (1, 2), InstantNotifier(chain))
    return arena, chain, node, leaves, aggregate


class TestTheTruthTable(unittest.TestCase):
    """One row per state, with the answer written out rather than computed."""

    def test_nothing_at_all_is_none(self):
        arena, _chain, node, leaves, _agg = fixture()
        self.assertEqual(knowledge_class(node, leaves[0].leaf_index, arena),
                         KNOWLEDGE_NONE)

    def test_requested_only(self):
        arena, _chain, node, leaves, _agg = fixture()
        node.view.requested.add(leaves[0].id)
        self.assertEqual(knowledge_class(node, leaves[0].leaf_index, arena),
                         KNOWLEDGE_REQUESTED)

    def test_a_full_object_being_verified(self):
        arena, _chain, node, leaves, _agg = fixture()
        node.view.verifying.add(leaves[0].id)
        self.assertEqual(knowledge_class(node, leaves[0].leaf_index, arena),
                         KNOWLEDGE_VERIFYING)

    def test_a_proof_being_verified_that_claims_the_leaf(self):
        arena, _chain, node, leaves, aggregate = fixture()
        node.view.verifying_proofs[aggregate.id] = 0
        self.assertEqual(knowledge_class(node, leaves[0].leaf_index, arena),
                         KNOWLEDGE_VERIFYING)

    def test_a_proof_bringing_only_a_body_for_a_leaf_it_does_not_claim(self):
        # The proof claims leaves 0 and 1; the pending body is for leaf 2. Leaf
        # 2 is still `verifying`, because its body is on the way.
        arena, _chain, node, leaves, aggregate = fixture()
        node.view.verifying_proofs[aggregate.id] = 1 << leaves[2].leaf_index
        self.assertEqual(knowledge_class(node, leaves[2].leaf_index, arena),
                         KNOWLEDGE_VERIFYING)

    def test_a_full_transfer_in_flight_brings_bodies_it_does_not_claim(self):
        """Coverage, not claim, for what a full object transfer will deliver.

        A proof's claim is frozen when proving starts, so an aggregate can
        structurally cover more than it asserts -- a dependency settled after the
        claim was taken. A *full object* transfer still delivers the bodies its
        structure covers, so asking `claim` here under-reports bodies about to
        arrive.

        Written because a mutation that dropped `coverage` from this check
        survived the rest of this file: every other object in flight here is a
        leaf, and for a leaf claim and coverage are the same mask, so nothing
        could tell the two apart.
        """
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(2)]
        # Covers both leaves, claims only the first.
        narrow = arena.new_aggregate(tuple(leaves), 0.0, creator=0,
                                     claimed=1 << leaves[0].leaf_index)
        self.assertNotEqual(arena.claim(narrow), arena.coverage(narrow))
        node = Node(0, (1,), InstantNotifier(ChainState()))
        node.view.verifying.add(narrow.id)
        # Leaf 1 is covered but not claimed: its body is on the way regardless.
        self.assertEqual(knowledge_class(node, leaves[1].leaf_index, arena),
                         KNOWLEDGE_VERIFYING)

    def test_a_request_for_an_aggregate_asks_for_what_it_covers(self):
        """Claim *or* coverage in the `requested` branch too.

        Fetching an aggregate returns the bodies its structure covers, so a
        request for it is also a request for a leaf it covers but does not claim.
        This branch was written with `claim` alone and reported `none` for a
        transaction already on its way -- the same mistake the in-flight branch
        had already been corrected for, repeated a few lines further down.
        """
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(2)]
        narrow = arena.new_aggregate(tuple(leaves), 0.0, creator=0,
                                     claimed=1 << leaves[0].leaf_index)
        node = Node(0, (1,), InstantNotifier(ChainState()))
        node.view.requested.add(narrow.id)
        self.assertEqual(knowledge_class(node, leaves[1].leaf_index, arena),
                         KNOWLEDGE_REQUESTED)

    def test_body_present_with_no_evidence(self):
        arena, _chain, node, leaves, _agg = fixture()
        node.view.bodies |= 1 << leaves[0].leaf_index
        self.assertEqual(knowledge_class(node, leaves[0].leaf_index, arena),
                         KNOWLEDGE_BODY_ONLY)

    def test_held_leaf_brings_its_own_body(self):
        arena, _chain, node, leaves, _agg = fixture()
        node.view.held[leaves[0].id] = 0.0
        node.view.bodies |= 1 << leaves[0].leaf_index
        self.assertEqual(knowledge_class(node, leaves[0].leaf_index, arena),
                         KNOWLEDGE_HELD_AND_BODY)

    def test_held_proof_with_the_body_substituted_by_a_hash(self):
        # The EIP's own mechanism, and the state binary coverage erases.
        arena, _chain, node, leaves, aggregate = fixture()
        node.view.held[aggregate.id] = 0.0
        self.assertEqual(knowledge_class(node, leaves[0].leaf_index, arena),
                         KNOWLEDGE_HELD_NO_BODY)

    def test_settled(self):
        arena, chain, node, leaves, _agg = fixture()
        chain.include(1 << leaves[0].leaf_index, slot=1)
        self.assertEqual(knowledge_class(node, leaves[0].leaf_index, arena),
                         KNOWLEDGE_SETTLED)


class TestPrecedenceBoundaries(unittest.TestCase):
    """Each case satisfies two predicates; the higher one has to win."""

    def test_settled_beats_a_held_proof_and_a_body(self):
        # A node knows a transaction is settled the moment it is notified, but
        # drops the objects covering it only at the next tick. In between it
        # holds evidence and a body for something it may no longer act on, and
        # the class describes capability rather than storage.
        arena, chain, node, leaves, aggregate = fixture()
        node.view.held[aggregate.id] = 0.0
        node.view.bodies |= 1 << leaves[0].leaf_index
        chain.include(1 << leaves[0].leaf_index, slot=1)
        self.assertEqual(knowledge_class(node, leaves[0].leaf_index, arena),
                         KNOWLEDGE_SETTLED)

    def test_held_no_body_beats_a_body_in_flight(self):
        arena, _chain, node, leaves, aggregate = fixture()
        node.view.held[aggregate.id] = 0.0
        node.view.verifying_proofs[leaves[0].id] = 1 << leaves[0].leaf_index
        self.assertEqual(knowledge_class(node, leaves[0].leaf_index, arena),
                         KNOWLEDGE_HELD_NO_BODY)

    def test_body_only_beats_verifying(self):
        arena, _chain, node, leaves, aggregate = fixture()
        node.view.bodies |= 1 << leaves[0].leaf_index
        node.view.verifying_proofs[aggregate.id] = 0
        self.assertEqual(knowledge_class(node, leaves[0].leaf_index, arena),
                         KNOWLEDGE_BODY_ONLY)

    def test_verifying_beats_requested(self):
        arena, _chain, node, leaves, _agg = fixture()
        node.view.verifying.add(leaves[0].id)
        node.view.requested.add(leaves[0].id)
        self.assertEqual(knowledge_class(node, leaves[0].leaf_index, arena),
                         KNOWLEDGE_VERIFYING)


class TestTheProjectionIsAPartition(unittest.TestCase):
    def test_seen_alone_is_none_and_not_a_class_of_its_own(self):
        # `seen` is bounded duplicate-suppression memory, not knowledge: it can
        # hold an identity that was received, verified and then abandoned. So it
        # gets no class, and `none` is displayed as "no active evidence, body or
        # request" rather than as ignorance.
        arena, _chain, node, leaves, _agg = fixture()
        node.view.seen.add(leaves[0].id)
        self.assertEqual(knowledge_class(node, leaves[0].leaf_index, arena),
                         KNOWLEDGE_NONE)

    def test_every_answer_is_one_of_the_declared_classes(self):
        arena, chain, node, leaves, aggregate = fixture()
        node.view.seen.add(leaves[0].id)
        node.view.requested.add(leaves[1].id)
        node.view.verifying.add(leaves[2].id)
        node.view.held[aggregate.id] = 0.0
        node.view.bodies |= 1 << leaves[1].leaf_index
        chain.include(1 << leaves[2].leaf_index, slot=1)
        for leaf in leaves:
            self.assertIn(knowledge_class(node, leaf.leaf_index, arena),
                          KNOWLEDGE_CLASSES)

    def test_classes_are_declared_in_precedence_order(self):
        # The order is load-bearing twice over: it is the precedence the
        # classifier applies and the order a stacked area draws.
        self.assertEqual(KNOWLEDGE_CLASSES[0], KNOWLEDGE_SETTLED)
        self.assertEqual(KNOWLEDGE_CLASSES[-1], KNOWLEDGE_NONE)
        self.assertEqual(len(set(KNOWLEDGE_CLASSES)), len(KNOWLEDGE_CLASSES))


if __name__ == "__main__":
    unittest.main()


class TestTheEvidence(unittest.TestCase):
    """Which object the class rests on, written out row by row like the classes.

    The rule: the transaction's own object when the node has it, else the
    lowest id among the aggregates that match; nothing for the three classes
    no single object evidences. The trace reports this id, and a reader takes
    the form -- own object or wrapper -- from it.
    """

    def test_settled_body_only_and_none_rest_on_no_object(self):
        arena, chain, node, leaves, aggregate = fixture()
        leaf = leaves[0].leaf_index
        self.assertEqual(knowledge_evidence(node, leaf, arena),
                         (KNOWLEDGE_NONE, None))
        node.view.bodies |= 1 << leaf
        self.assertEqual(knowledge_evidence(node, leaf, arena),
                         (KNOWLEDGE_BODY_ONLY, None))
        node.view.held[aggregate.id] = 0.0
        chain.include(1 << leaf, slot=1)
        self.assertEqual(knowledge_evidence(node, leaf, arena),
                         (KNOWLEDGE_SETTLED, None))

    def test_a_held_leaf_is_its_own_evidence(self):
        arena, _chain, node, leaves, _agg = fixture()
        node.view.held[leaves[0].id] = 0.0
        node.view.bodies |= 1 << leaves[0].leaf_index
        self.assertEqual(knowledge_evidence(node, leaves[0].leaf_index, arena),
                         (KNOWLEDGE_HELD_AND_BODY, leaves[0].id))

    def test_a_held_aggregate_is_the_evidence_when_the_leaf_is_absent(self):
        arena, _chain, node, leaves, aggregate = fixture()
        node.view.held[aggregate.id] = 0.0
        self.assertEqual(knowledge_evidence(node, leaves[0].leaf_index, arena),
                         (KNOWLEDGE_HELD_NO_BODY, aggregate.id))

    def test_the_own_object_beats_an_aggregate_however_they_were_admitted(self):
        arena, _chain, node, leaves, aggregate = fixture()
        # Aggregate first, so insertion order would pick it.
        node.view.held[aggregate.id] = 0.0
        node.view.held[leaves[0].id] = 1.0
        self.assertEqual(knowledge_evidence(node, leaves[0].leaf_index, arena)[1],
                         leaves[0].id)

    def test_two_aggregates_resolve_to_the_lowest_id(self):
        arena, _chain, node, leaves, aggregate = fixture()
        later = arena.new_aggregate((leaves[0], leaves[2]), 0.0, creator=0)
        self.assertGreater(later.id, aggregate.id)
        node.view.held[later.id] = 0.0
        node.view.held[aggregate.id] = 1.0
        self.assertEqual(knowledge_evidence(node, leaves[0].leaf_index, arena)[1],
                         aggregate.id)

    def test_evidence_in_flight_and_requested_name_their_object(self):
        arena, _chain, node, leaves, aggregate = fixture()
        leaf = leaves[0].leaf_index
        node.view.requested.add(aggregate.id)
        self.assertEqual(knowledge_evidence(node, leaf, arena),
                         (KNOWLEDGE_REQUESTED, aggregate.id))
        node.view.verifying_proofs[aggregate.id] = 0
        self.assertEqual(knowledge_evidence(node, leaf, arena),
                         (KNOWLEDGE_VERIFYING, aggregate.id))
        node.view.verifying.add(leaves[0].id)
        self.assertEqual(knowledge_evidence(node, leaf, arena),
                         (KNOWLEDGE_VERIFYING, leaves[0].id))

    def test_verifying_chooses_once_across_full_objects_and_wrappers(self):
        # Aggregate 1 is a proof being verified, aggregate 2 a full object in
        # flight; both match. One choice over both stores, so the lower id wins
        # whichever store it sits in -- and the own object beats either.
        arena, _chain, node, leaves, aggregate = fixture()
        later = arena.new_aggregate((leaves[0], leaves[2]), 0.0, creator=0)
        node.view.verifying_proofs[aggregate.id] = 0
        node.view.verifying.add(later.id)
        self.assertEqual(knowledge_evidence(node, leaves[0].leaf_index, arena),
                         (KNOWLEDGE_VERIFYING, aggregate.id))
        node.view.verifying_proofs.clear()
        node.view.verifying_proofs[later.id] = 0
        node.view.verifying.discard(later.id)
        node.view.verifying.add(aggregate.id)
        self.assertEqual(knowledge_evidence(node, leaves[0].leaf_index, arena),
                         (KNOWLEDGE_VERIFYING, aggregate.id))
        node.view.verifying.add(leaves[0].id)
        self.assertEqual(knowledge_evidence(node, leaves[0].leaf_index, arena),
                         (KNOWLEDGE_VERIFYING, leaves[0].id))

    def test_a_pending_body_keeps_verifying_when_its_proof_is_unknown(self):
        # The body is on its way whether or not the arena knows the proof it
        # rides on; the class must not fall to none because the lookup failed.
        arena, _chain, node, leaves, _agg = fixture()
        node.view.verifying_proofs[999] = 1 << leaves[0].leaf_index
        self.assertEqual(knowledge_evidence(node, leaves[0].leaf_index, arena),
                         (KNOWLEDGE_VERIFYING, 999))

    def test_a_pending_body_names_the_proof_carrying_it(self):
        # The proof claims leaves 0 and 1; leaf 2's body is riding along.
        arena, _chain, node, leaves, aggregate = fixture()
        node.view.verifying_proofs[aggregate.id] = 1 << leaves[2].leaf_index
        self.assertEqual(knowledge_evidence(node, leaves[2].leaf_index, arena),
                         (KNOWLEDGE_VERIFYING, aggregate.id))

    def test_the_class_is_the_first_element_in_every_state(self):
        arena, _chain, node, leaves, aggregate = fixture()
        leaf = leaves[0].leaf_index
        states = [
            lambda: None,
            lambda: node.view.requested.add(aggregate.id),
            lambda: node.view.verifying.add(leaves[0].id),
            lambda: node.view.__setattr__("bodies", 1 << leaf),
            lambda: node.view.held.__setitem__(aggregate.id, 0.0),
            lambda: node.view.held.__setitem__(leaves[0].id, 0.0),
        ]
        for mutate in states:
            mutate()
            self.assertEqual(knowledge_class(node, leaf, arena),
                             knowledge_evidence(node, leaf, arena)[0])
