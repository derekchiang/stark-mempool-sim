#!/usr/bin/env python3
"""What a proposer needs before it may include: a proof, or proof plus body.

The EIP's builder puts every included transaction into the execution payload,
which takes the body -- a claim whose body the proposer lacks cannot become an
inclusion however wide the proof. The model long said otherwise: selection
read claims alone, so a proposer holding one wrapper-delivered aggregate with
every body hash-substituted away "included" everything it claimed. That is now
the selectable `claim_only`; `body_required` is the faithful reading and the
default.
"""

import random
import unittest

from starksim.arena import Arena, KIND_PQ
from starksim.chain import ChainState, InstantNotifier
from starksim.costs import CostTable
from starksim.netsim import NetworkSim
from starksim.network import Overlay, bytes_per_ms
from starksim.node import Node
from starksim.payload import SizeBreakdown, WireMessage, WrapperCore, \
    WrapperPayload

from tests.support import two_node_overlay


class TestSelectionUnderBodies(unittest.TestCase):
    def a_node_holding(self, arena, objects):
        node = Node(0, (1,), InstantNotifier(ChainState()))
        for obj in objects:
            node.admit(obj, 1.0)
        return node

    def test_a_bodiless_claim_is_worth_nothing(self):
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(3)]
        wide = arena.new_aggregate(leaves, 1.0, 1, claimed=0b111)
        node = self.a_node_holding(arena, [wide])
        # Bodies for one of the three: only that one may be attempted, and
        # the returned mask is the effective claim, never raw coverage --
        # coverage let a block include what no selected proof asserted.
        picked, claimed = node.select_by_coverage(arena, 2, 0,
                                                  body_mask=0b010)
        self.assertEqual(picked, [wide])
        self.assertEqual(claimed, 0b010)

    def test_bodies_change_which_object_wins(self):
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(4)]
        # Three claims without bodies against one claim with a body: unmasked
        # the wide aggregate wins, masked it is worthless.
        wide = arena.new_aggregate(leaves[:3], 1.0, 1, claimed=0b0111)
        narrow = arena.new_aggregate(leaves[3:], 1.0, 2, claimed=0b1000)
        node = self.a_node_holding(arena, [wide, narrow])
        unmasked, _ = node.select_by_coverage(arena, 1, 0)
        self.assertEqual(unmasked, [wide])
        masked, _ = node.select_by_coverage(arena, 1, 0, body_mask=0b1000)
        self.assertEqual(masked, [narrow])


def proposer_sim(**kwargs):
    overlay = two_node_overlay(10.0)
    defaults = dict(seed=1, slot_length=1000.0, proposer_id=0,
                    costs=CostTable(verify_recv=0.0))
    defaults.update(kwargs)
    return NetworkSim(overlay, **defaults)


def deliver_claim_with_partial_bodies(sim):
    """Node 0 receives a wrapper claiming two txs but carrying one body."""
    leaves = [sim.arena.new_leaf(KIND_PQ, 0.0) for _ in range(2)]
    aggregate = sim.arena.new_aggregate(leaves, 1.0, 1, claimed=0b11)
    core = WrapperCore(wrapper_id=1, proof_object_id=aggregate.id,
                       creator=1, tick=0, mode=1, tx_mask=0b11, dep_count=2)
    sim._deliver_wrapper(WireMessage(
        sender=1, receiver=0,
        payload=WrapperPayload(core=core, body_mask=0b01,
                               size=SizeBreakdown())))


class TestInclusionPolicy(unittest.TestCase):
    def test_body_required_includes_only_what_it_can_carry(self):
        sim = proposer_sim()
        deliver_claim_with_partial_bodies(sim)
        sim.schedule_slots(1)
        sim.run()
        self.assertEqual(sim.chain.blocks[-1].included, 0b01)

    def test_claim_only_includes_the_whole_claim(self):
        sim = proposer_sim(inclusion_policy="claim_only")
        deliver_claim_with_partial_bodies(sim)
        sim.schedule_slots(1)
        sim.run()
        self.assertEqual(sim.chain.blocks[-1].included, 0b11)

    def test_coverage_wider_than_claim_is_not_included(self):
        # An aggregate physically covering two transactions but claiming one:
        # the unclaimed one is unsupported evidence, and its inclusion was
        # also an omniscience leak -- a wrapper's recipient cannot see the
        # fold's internal structure. Both policies must refuse it, bodies or
        # not.
        for policy in ("body_required", "claim_only"):
            sim = proposer_sim(inclusion_policy=policy)
            leaves = [sim.arena.new_leaf(KIND_PQ, 0.0) for _ in range(2)]
            aggregate = sim.arena.new_aggregate(leaves, 1.0, 1, claimed=0b01)
            core = WrapperCore(wrapper_id=1, proof_object_id=aggregate.id,
                               creator=1, tick=0, mode=1, tx_mask=0b01,
                               dep_count=1)
            sim._deliver_wrapper(WireMessage(
                sender=1, receiver=0,
                payload=WrapperPayload(core=core, body_mask=0b01,
                                       size=SizeBreakdown())))
            # Both bodies present, so body_required cannot be the reason.
            sim.nodes[0].receive_bodies(0b11)
            sim.schedule_slots(1)
            sim.run()
            self.assertEqual(sim.chain.blocks[-1].included, 0b01, policy)

    def test_the_default_is_body_required(self):
        sim = proposer_sim()
        self.assertEqual(sim.variant()["inclusion_policy"], "body_required")

    def test_an_unknown_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            proposer_sim(inclusion_policy="hopeful")


if __name__ == "__main__":
    unittest.main()


class TestADedicatedBuilder(unittest.TestCase):
    """v1's shape: N aggregators and, separately, one node that only builds.

    Not the EIP's shape, where a proposer is a mempool node like any other, so
    aggregating stays the default. This exists to isolate the difference rather
    than to recommend it.
    """

    def a_run(self, aggregates, **kwargs):
        rng = random.Random("builder:topology")
        overlay = Overlay.random_regular(12, 4, rng, base_latency=50.0)
        settings = dict(seed=5, capacity=bytes_per_ms(50),
                        costs=CostTable(verify_recv=1.0,
                                        prove_recursive=lambda deps: 200.0),
                        proposer_mode=("dedicated" if not aggregates else "fixed"),
                        proposer_id=0)
        settings.update(kwargs)
        sim = NetworkSim(overlay, **settings)
        for index in range(40):
            sim.schedule_birth(index * 150.0, KIND_PQ)
        sim.schedule_ticks(12)
        sim.schedule_slots(3)
        sim.run(until=12000.0)
        return sim

    def test_a_dedicated_builder_proves_nothing(self):
        sim = self.a_run(False)
        built_by_proposer = [obj for obj in sim.arena.objects.values()
                             if not obj.is_leaf() and obj.creator == 0]
        self.assertEqual(built_by_proposer, [])

    def test_the_others_still_do(self):
        sim = self.a_run(False)
        self.assertGreater(sim.proofs_completed, 0)
        self.assertGreater(sim.wrappers_emitted, 0)

    def test_the_exempt_node_runs_no_rounds(self):
        # Not counted as ticks either: a round that decides nothing is not a
        # round, and counting it would flatter the tick statistics.
        exempt = self.a_run(False)
        ordinary = self.a_run(True)
        self.assertLess(exempt.ticks_run, ordinary.ticks_run)

    def test_it_still_builds_blocks(self):
        sim = self.a_run(False)
        self.assertGreater(sim.chain.included_count(), 0)
        self.assertTrue(all(block.proposer == 0 for block in sim.chain.blocks))

    def test_the_default_is_an_ordinary_node(self):
        sim = self.a_run(True)
        self.assertEqual(sim.variant()["proposer_mode"], "fixed")
        self.assertTrue(any(obj.creator == 0 for obj in sim.arena.objects.values()
                            if not obj.is_leaf()))

    def test_a_moving_proposer_cannot_be_dedicated(self):
        # There is no such combination to refuse: the role is one of the
        # four modes, so a rotating proposer simply aggregates.
        sim = self.a_run(True, proposer_mode="rotating")
        self.assertIsNone(sim._dedicated_builder)
