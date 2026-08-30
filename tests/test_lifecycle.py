#!/usr/bin/env python3
"""Step 4: fee and expiry, the candidate lifecycle, and the bounded structures
that keep per-node state from growing without limit.

The theme is that every way an object leaves a view is either meaningful or
measured. Included and expired are chain facts; superseded is the model working
as intended; evicted and forgotten are the model admitting it dropped something,
which is why both are counted rather than silent.
"""

import random
import unittest

from starksim.arena import Arena, KIND_PQ, KIND_PRIVACY, useful_count
from starksim.chain import ChainState, InstantNotifier
from starksim.netsim import NetworkSim
from starksim.network import Overlay
from starksim.node import Node, SeenWindow

from tests.support import make_node, two_node_overlay


class TestFeeAndExpiry(unittest.TestCase):
    def test_both_are_inert_by_default(self):
        arena = Arena()
        leaf = arena.new_leaf(KIND_PQ, 0.0)
        self.assertEqual(leaf.fee, 0.0)
        self.assertIsNone(leaf.expiry)

    def test_they_are_carried_when_given(self):
        arena = Arena()
        leaf = arena.new_leaf(KIND_PRIVACY, 10.0, fee=2.5, expiry=500.0)
        self.assertEqual(leaf.fee, 2.5)
        self.assertEqual(leaf.expiry, 500.0)

    def test_an_expired_transaction_is_neither_included_nor_pending(self):
        # Without expiry the only exit is inclusion, so nothing can ever be
        # pruned and the EIP's removals tier has nothing to act on.
        overlay = two_node_overlay(1.0)
        sim = NetworkSim(overlay, seed=1, slot_length=100.0, proposer_id=0,
                         block_budget=0)
        # Two transactions, one object per block: the second cannot be included
        # before it dies.
        sim.schedule_birth(0.0, KIND_PQ, node_id=0, lifetime=150.0)
        sim.schedule_birth(1.0, KIND_PQ, node_id=0, lifetime=150.0)
        sim.schedule_slots(3)
        sim.run()
        metrics = sim.metrics()
        self.assertEqual(metrics["included_txs"], 1)
        self.assertEqual(metrics["expired_txs"], 1)
        self.assertEqual(metrics["pending_txs"], 0)

    def test_expiry_happens_before_the_proposer_looks(self):
        # A transaction that dies at this very boundary must not be offered.
        overlay = two_node_overlay(1.0)
        sim = NetworkSim(overlay, seed=1, slot_length=100.0, proposer_id=0)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0, lifetime=100.0)
        sim.schedule_slots(1)
        sim.run()
        self.assertEqual(sim.chain.included_count(), 0)
        self.assertEqual(sim.chain.expired_count(), 1)

    def test_expiry_advances_the_revision_without_a_block(self):
        # A usefulness cache keyed on block count would go stale here.
        overlay = two_node_overlay(1.0)
        sim = NetworkSim(overlay, seed=1, slot_length=100.0)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0, lifetime=50.0)
        sim.run(until=60.0)
        sim._expire_due(sim.engine.now)
        self.assertEqual(sim.chain.revision, 1)
        self.assertEqual(len(sim.chain.blocks), 0)


class TestSeenWindow(unittest.TestCase):
    def test_unbounded_remembers_everything(self):
        window = SeenWindow()
        for identity in range(1000):
            window.add(identity)
        self.assertIn(0, window)
        self.assertEqual(window.forgotten, 0)

    def test_bounded_keeps_recent_history_exactly(self):
        window = SeenWindow(limit=10)
        for identity in range(15):
            window.add(identity)
        # Everything since the last rotation is still known exactly.
        for identity in range(11, 15):
            self.assertIn(identity, window)

    def test_bounded_memory_does_not_grow_without_limit(self):
        window = SeenWindow(limit=100)
        for identity in range(10_000):
            window.add(identity)
        self.assertLessEqual(len(window), 210)

    def test_forgetting_is_counted_not_hidden(self):
        # A forgotten identity can be accepted twice, doing work again. That is
        # a cost of the bound and has to be visible.
        window = SeenWindow(limit=5)
        for identity in range(40):
            window.add(identity)
        self.assertGreater(window.forgotten, 0)
        self.assertNotIn(0, window)


class TestCandidateLifecycle(unittest.TestCase):
    def _held(self, node, arena, count):
        for _ in range(count):
            leaf = arena.new_leaf(KIND_PQ, 0.0)
            node.admit(leaf, 0.0)
        return node

    def test_superseding_is_per_node_and_reports_what_it_dropped(self):
        arena = Arena()
        node = self._held(make_node(), arena, 3)
        # The identities, not a count: a count cannot be turned into a residency
        # series, and residency is what departures exist to make recoverable.
        self.assertEqual(node.supersede([0, 1]), [0, 1])
        self.assertEqual(len(node.view.held), 1)
        self.assertEqual(node.superseded, 2)

    def test_superseding_reports_only_what_it_actually_held(self):
        arena = Arena()
        node = self._held(make_node(), arena, 2)
        # Asking to drop something absent is not a departure, and reporting it
        # would put residency below the truth.
        self.assertEqual(node.supersede([1, 99]), [1])
        self.assertEqual(node.superseded, 1)

    def test_pruning_drops_what_covers_nothing_wanted(self):
        arena = Arena()
        chain = ChainState()
        node = Node(0, (1,), InstantNotifier(chain))
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(3)]
        for leaf in leaves:
            node.admit(leaf, 0.0)
        chain.include(arena.coverage(leaves[0]) | arena.coverage(leaves[1]), 1)
        self.assertEqual(sorted(node.prune_discarded(arena)),
                         [leaves[0].id, leaves[1].id])
        self.assertEqual(len(node.view.held), 1)

    def test_no_limit_means_no_eviction(self):
        arena = Arena()
        node = self._held(make_node(), arena, 50)
        self.assertEqual(node.enforce_candidate_limit(arena), [])
        self.assertEqual(len(node.view.held), 50)

    def test_a_limit_evicts_the_least_useful_first(self):
        arena = Arena()
        chain = ChainState()
        node = Node(0, (1,), InstantNotifier(chain), candidate_limit=2)
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(3)]
        wide = arena.new_aggregate(leaves, 1.0, 0)
        for obj in leaves + [wide]:
            node.admit(obj, 0.0)
        # Two of the leaves are already included, so they cover nothing useful.
        chain.include(arena.coverage(leaves[0]) | arena.coverage(leaves[1]), 1)
        node.enforce_candidate_limit(arena)
        self.assertEqual(len(node.view.held), 2)
        self.assertIn(wide.id, node.view.held)      # the most useful survives
        self.assertEqual(node.evicted, 2)

    def test_eviction_is_counted_because_it_is_information_lost(self):
        # Enforcement is an explicit step by the wiring layer, not a side effect
        # of admission: the node stays a holder of state, not a scheduler.
        arena = Arena()
        node = self._held(make_node(candidate_limit=5), arena, 20)
        self.assertEqual(len(node.view.held), 20)
        node.enforce_candidate_limit(arena)
        self.assertEqual(len(node.view.held), 5)
        self.assertEqual(node.evicted, 15)

    def test_a_forgotten_identity_is_still_recognised_while_held(self):
        # A bounded `seen` can forget something the node is still holding. If
        # `seen` were the only guard, that object would be accepted and processed
        # a second time while already in the view.
        arena = Arena()
        node = make_node(seen_limit=2)
        first = arena.new_leaf(KIND_PQ, 0.0)
        node.admit(first, 0.0)
        for _ in range(10):
            node.admit(arena.new_leaf(KIND_PQ, 0.0), 0.0)
        self.assertNotIn(first.id, node.view.seen)      # forgotten
        self.assertFalse(node.begin_receive(first))     # still recognised


class TestCoverageCacheBound(unittest.TestCase):
    def _chain_of(self, arena, length):
        current = arena.new_leaf(KIND_PQ, 0.0)
        for step in range(length):
            fresh = arena.new_leaf(KIND_PQ, float(step))
            current = arena.new_aggregate([current, fresh], float(step), 0)
        return current

    def test_unbounded_keeps_every_traversed_mask(self):
        arena = Arena()
        top = self._chain_of(arena, 200)
        arena.coverage(top)
        self.assertEqual(arena.coverage_memos, 200)
        self.assertEqual(arena.cache_evictions, 0)

    def test_a_bound_evicts_and_the_answer_is_unchanged(self):
        # The cache must be invisible: a bounded run and an unbounded one differ
        # in memory, never in results.
        loose = Arena()
        bounded = Arena(coverage_cache_limit=10)
        for arena in (loose, bounded):
            top = self._chain_of(arena, 200)
            self.assertEqual(arena.coverage(top).bit_count(), 201)
        self.assertLessEqual(bounded.coverage_memos, 10)
        self.assertGreater(bounded.cache_evictions, 100)

    def test_recomputation_after_eviction_is_correct(self):
        arena = Arena(coverage_cache_limit=3)
        top = self._chain_of(arena, 50)
        first = arena.coverage(top)
        # Force eviction of everything by walking other aggregates.
        for _ in range(20):
            arena.coverage(self._chain_of(arena, 5))
        self.assertEqual(arena.coverage(top), first)

    def test_cache_cost_is_measured_in_bytes(self):
        # Entry count says little: a mask's width grows with the highest
        # transaction id it covers, so late aggregates are much larger.
        arena = Arena()
        top = self._chain_of(arena, 500)
        arena.coverage(top)
        self.assertGreater(arena.coverage_cache_bytes(), 1000)

    def test_a_leaf_mask_is_never_evicted(self):
        arena = Arena(coverage_cache_limit=1)
        leaf = arena.new_leaf(KIND_PQ, 0.0)
        self._chain_of(arena, 20)
        self.assertEqual(arena.coverage(leaf), 1 << leaf.leaf_index)


class TestBoundedRunStillWorks(unittest.TestCase):
    def test_a_fully_bounded_run_delivers_and_reports_its_losses(self):
        rng = random.Random("bounded")
        overlay = Overlay.random_regular(20, 4, rng, base_latency=20.0)
        sim = NetworkSim(overlay, seed=1, slot_length=500.0,
                         seen_limit=5, candidate_limit=3,
                         coverage_cache_limit=8)
        for index in range(40):
            sim.schedule_birth(index * 20.0, KIND_PQ, lifetime=2000.0)
        sim.schedule_slots(6)
        sim.run()
        metrics = sim.metrics()
        # It still runs, still includes, and says what it dropped.
        self.assertGreater(metrics["included_txs"], 0)
        self.assertGreater(metrics["evicted"], 0)
        self.assertGreater(metrics["seen_forgotten"], 0)
        sim.assert_invariants()


if __name__ == "__main__":
    unittest.main()
