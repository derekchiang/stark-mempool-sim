#!/usr/bin/env python3
"""The v2 object model: n-ary folds, coverage as a bitmask, node-relative
usefulness, and the absence of per-viewer state."""

import unittest

from starksim.arena import (Arena, KIND_AGGREGATE, KIND_PQ, KIND_PRIVACY, USER,
                            marginal_count, useful_count)
from starksim.chain import (ChainState, InstantNotifier, OverlayNotifier,
                            ProposerSchedule)


class TestLeaves(unittest.TestCase):
    def test_a_leaf_covers_exactly_itself(self):
        arena = Arena()
        first = arena.new_leaf(KIND_PQ, 10.0)
        second = arena.new_leaf(KIND_PRIVACY, 20.0)
        self.assertEqual(arena.coverage(first), 0b01)
        self.assertEqual(arena.coverage(second), 0b10)
        self.assertEqual(first.total, 1)
        self.assertEqual(first.depth, 0)
        self.assertTrue(first.is_leaf())
        self.assertEqual(first.creator, USER)

    def test_leaf_indices_are_dense(self):
        # Coverage masks index by leaf, so leaf numbering must not skip: an
        # aggregate consumes an object id but no coverage bit.
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(3)]
        arena.new_aggregate(leaves, 1.0, 0)
        after = arena.new_leaf(KIND_PQ, 2.0)
        self.assertEqual([leaf.leaf_index for leaf in leaves], [0, 1, 2])
        self.assertEqual(after.leaf_index, 3)
        self.assertEqual(arena.leaf_count(), 4)


class TestFolds(unittest.TestCase):
    def test_arity_is_free(self):
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(5)]
        wide = arena.new_aggregate(leaves, 10.0, 3)
        self.assertEqual(arena.coverage(wide), 0b11111)
        self.assertEqual(wide.total, 5)
        self.assertEqual(wide.depth, 1)
        self.assertEqual(wide.kind, KIND_AGGREGATE)
        self.assertEqual(wide.creator, 3)

    def test_a_unary_fold_is_a_reproof(self):
        arena = Arena()
        leaf = arena.new_leaf(KIND_PQ, 0.0)
        again = arena.new_aggregate([leaf], 5.0, 0)
        self.assertEqual(arena.coverage(again), arena.coverage(leaf))
        self.assertEqual(again.total, 1)
        self.assertEqual(again.depth, 1)

    def test_empty_fold_rejected(self):
        arena = Arena()
        with self.assertRaises(ValueError):
            arena.new_aggregate([], 1.0, 0)

    def test_overlapping_folds_union_coverage_but_add_work(self):
        # Two nodes racing on the same inputs produce aggregates covering the
        # same txs. Coverage is a union -- each tx once -- while `total` stays
        # additive, because the overlap really was proved twice.
        arena = Arena()
        first = arena.new_leaf(KIND_PQ, 0.0)
        second = arena.new_leaf(KIND_PQ, 0.0)
        twin_a = arena.new_aggregate([first, second], 10.0, 0)
        twin_b = arena.new_aggregate([first, second], 10.0, 1)
        merged = arena.new_aggregate([twin_a, twin_b], 20.0, 2)
        self.assertEqual(arena.coverage(merged), 0b11)     # two txs, once each
        self.assertEqual(merged.total, 4)                  # four leaves proved
        self.assertEqual(merged.depth, 2)

    def test_creating_an_aggregate_books_no_work(self):
        # Work is committed when proving starts, not when the aggregate appears:
        # once proving takes time those are different instants, and a cancelled
        # or still-in-flight job spent effort that produced no aggregate.
        arena = Arena()
        leaf = arena.new_leaf(KIND_PQ, 0.0)
        arena.new_aggregate([leaf], 1.0, 0)
        self.assertEqual(leaf.consumed, 0)

    def test_consumption_is_booked_by_the_caller_that_spends_it(self):
        arena = Arena()
        leaf = arena.new_leaf(KIND_PQ, 0.0)
        arena.note_consumption([leaf])
        arena.note_consumption([leaf])
        self.assertEqual(leaf.consumed, 2)

    def test_no_parent_links_exist(self):
        # "Somebody folded this" is not observable from a wrapper, so the object
        # must not offer a way to find out.
        arena = Arena()
        leaf = arena.new_leaf(KIND_PQ, 0.0)
        arena.new_aggregate([leaf], 1.0, 0)
        self.assertFalse(hasattr(leaf, "parents"))

    def test_no_per_viewer_state_on_objects(self):
        arena = Arena()
        leaf = arena.new_leaf(KIND_PQ, 0.0)
        for attribute in ("included", "pending", "covering", "builder_seen",
                          "builder_counted", "traced_size"):
            self.assertFalse(hasattr(leaf, attribute), attribute)


class TestCoverageComputation(unittest.TestCase):
    def test_deep_chain_does_not_recurse(self):
        # A long fold chain must not depend on interpreter stack depth.
        arena = Arena()
        current = arena.new_leaf(KIND_PQ, 0.0)
        for step in range(5000):
            fresh = arena.new_leaf(KIND_PQ, float(step))
            current = arena.new_aggregate([current, fresh], float(step), 0)
        self.assertEqual(arena.coverage(current).bit_count(), 5001)
        self.assertEqual(current.depth, 5000)

    def test_coverage_is_memoised_and_countable(self):
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(4)]
        left = arena.new_aggregate(leaves[:2], 1.0, 0)
        right = arena.new_aggregate(leaves[2:], 1.0, 1)
        top = arena.new_aggregate([left, right], 2.0, 2)
        before = arena.coverage_memos
        first_read = arena.coverage(top)
        after = arena.coverage_memos
        self.assertEqual(first_read, 0b1111)
        self.assertEqual(after - before, 3)          # top plus its two children
        # Reading again computes nothing further: coverage is immutable.
        arena.coverage(top)
        self.assertEqual(arena.coverage_memos, after)

    def test_memo_can_be_released_and_recomputed(self):
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(3)]
        top = arena.new_aggregate(leaves, 1.0, 0)
        expected = arena.coverage(top)
        arena.drop_coverage_memo(top)
        self.assertEqual(arena.coverage(top), expected)

    def test_a_leaf_mask_is_rebuilt_rather_than_stored(self):
        # A Python integer grows with its highest bit, so one mask per leaf makes
        # the collection quadratic in transaction count -- about 625 MB of raw
        # bits at a hundred thousand leaves. A single shift rebuilds it.
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(2000)]
        for leaf in leaves:
            self.assertIsNone(leaf._coverage)
        self.assertEqual(arena.coverage(leaves[1999]), 1 << 1999)
        self.assertEqual(arena.coverage_memos, 0)

    def test_a_zero_cache_limit_still_answers_correctly(self):
        # A bound that corrupts the answer rather than only the memory would be
        # worse than no bound: the mask being returned must not be evicted.
        arena = Arena(coverage_cache_limit=0)
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(4)]
        aggregate = arena.new_aggregate(leaves, 1.0, 0)
        self.assertEqual(arena.coverage(aggregate).bit_count(), 4)

    def test_leaf_memo_is_not_droppable(self):
        # A leaf's mask is a single shift, not worth caching or evicting.
        arena = Arena()
        leaf = arena.new_leaf(KIND_PQ, 0.0)
        before = arena.coverage_memos
        arena.drop_coverage_memo(leaf)
        self.assertEqual(arena.coverage_memos, before)
        self.assertEqual(arena.coverage(leaf), 1)


class TestNodeRelativeUsefulness(unittest.TestCase):
    def test_usefulness_depends_on_the_asking_node(self):
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(4)]
        aggregate = arena.new_aggregate(leaves, 1.0, 0)
        coverage = arena.coverage(aggregate)
        well_informed = arena.coverage(leaves[0]) | arena.coverage(leaves[1])
        self.assertEqual(useful_count(coverage, 0), 4)
        self.assertEqual(useful_count(coverage, well_informed), 2)

    def test_marginal_count_excludes_what_is_already_chosen(self):
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(4)]
        left = arena.new_aggregate(leaves[:3], 1.0, 0)
        right = arena.new_aggregate(leaves[1:], 1.0, 1)
        chosen = arena.coverage(left)
        # `right` covers leaves 1,2,3; leaves 1,2 are already chosen via `left`.
        self.assertEqual(marginal_count(arena.coverage(right), 0, chosen), 1)

    def test_fully_discarded_coverage_is_worth_nothing(self):
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(3)]
        aggregate = arena.new_aggregate(leaves, 1.0, 0)
        coverage = arena.coverage(aggregate)
        self.assertEqual(useful_count(coverage, coverage), 0)


class TestChainState(unittest.TestCase):
    def test_inclusion_counts_each_tx_once(self):
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(4)]
        left = arena.new_aggregate(leaves[:3], 1.0, 0)
        right = arena.new_aggregate(leaves[1:], 1.0, 1)
        chain = ChainState()
        first = chain.include(arena.coverage(left), slot=1)
        second = chain.include(arena.coverage(right), slot=2)
        self.assertEqual(first.count(), 3)
        self.assertEqual(second.count(), 1)          # only leaf 3 was new
        self.assertEqual(chain.included_count(), 4)
        # The block keeps what it needs to be reconstructed, not just a count.
        self.assertEqual(first.slot, 1)
        self.assertEqual(first.included.bit_count(), 3)

    def test_discard_is_separate_from_inclusion(self):
        arena = Arena()
        leaf = arena.new_leaf(KIND_PQ, 0.0)
        chain = ChainState()
        expired = chain.expire(arena.coverage(leaf))
        self.assertEqual(expired.bit_count(), 1)
        self.assertEqual(chain.expired_count(), 1)
        # Expiry advances the revision even though it produced no block: a cache
        # keyed on block count would have missed it.
        self.assertEqual(chain.revision, 1)
        self.assertEqual(len(chain.blocks), 0)
        self.assertEqual(chain.included_count(), 0)    # expiry is not inclusion

    def test_instant_notifier_gives_every_node_the_same_view(self):
        arena = Arena()
        leaf = arena.new_leaf(KIND_PQ, 0.0)
        chain = ChainState()
        notifier = InstantNotifier(chain)
        chain.include(arena.coverage(leaf), slot=1)
        self.assertEqual(notifier.known_discarded(0),
                         notifier.known_discarded(99))
        self.assertFalse(notifier.diverges)


class TestOverlayNotifier(unittest.TestCase):
    """The counterpart that makes per-node knowledge able to differ at all."""

    def _setup(self, node_count=4):
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(4)]
        chain = ChainState()
        notifier = OverlayNotifier(chain, node_count)
        return arena, leaves, chain, notifier

    def test_nodes_can_disagree_about_what_is_discarded(self):
        arena, leaves, chain, notifier = self._setup()
        aggregate = arena.new_aggregate(leaves, 1.0, 0)
        coverage = arena.coverage(aggregate)
        chain.include(arena.coverage(arena.new_aggregate(leaves[:2], 1.0, 0)), 1)
        notifier.notify(0, chain.discarded_mask())
        # Node 0 has been told; node 1 has not.
        self.assertEqual(useful_count(coverage, notifier.known_discarded(0)), 2)
        self.assertEqual(useful_count(coverage, notifier.known_discarded(1)), 4)
        self.assertTrue(notifier.diverges)

    def test_generation_advances_only_on_new_knowledge(self):
        # The generation is what a per-node usefulness cache is keyed on, so it
        # must not tick when nothing changed.
        arena, leaves, chain, notifier = self._setup()
        chain.include(arena.coverage(leaves[0]), 1)
        self.assertEqual(notifier.generation(0), 0)
        notifier.notify(0, chain.discarded_mask())
        self.assertEqual(notifier.generation(0), 1)
        notifier.notify(0, chain.discarded_mask())          # same news again
        self.assertEqual(notifier.generation(0), 1)
        self.assertEqual(notifier.generation(1), 0)

    def test_a_node_never_knows_more_than_is_true(self):
        arena, leaves, chain, notifier = self._setup()
        chain.include(arena.coverage(leaves[0]), 1)
        notifier.notify_all(chain.discarded_mask())
        for node_id in range(4):
            self.assertEqual(
                notifier.known_discarded(node_id) & ~chain.discarded_mask(), 0)

    def test_a_notifier_cannot_invent_facts(self):
        # A notifier is a channel for chain facts, not a way to create them:
        # telling a node a transaction is dead when it is not would make the
        # node's belief unfalsifiable.
        arena, leaves, chain, notifier = self._setup()
        untrue = arena.coverage(leaves[2])
        self.assertEqual(notifier.notify(0, untrue), 0)
        self.assertEqual(notifier.known_discarded(0), 0)
        self.assertEqual(notifier.generation(0), 0)

    def test_notify_reports_what_was_actually_new(self):
        arena, leaves, chain, notifier = self._setup()
        chain.include(arena.coverage(leaves[0]), 1)
        first = notifier.notify(0, chain.discarded_mask())
        self.assertEqual(first.bit_count(), 1)
        self.assertEqual(notifier.notify(0, chain.discarded_mask()), 0)


class TestProposerSchedule(unittest.TestCase):
    def test_fixed_proposer_never_moves(self):
        schedule = ProposerSchedule(10, "fixed", fixed_id=3)
        self.assertEqual([schedule.proposer_for(slot) for slot in range(4)],
                         [3, 3, 3, 3])

    def test_rotating_proposer_is_a_deterministic_round_robin(self):
        schedule = ProposerSchedule(4, "rotating", fixed_id=0)
        self.assertEqual([schedule.proposer_for(slot) for slot in range(6)],
                         [0, 1, 2, 3, 0, 1])

    def test_random_proposer_stays_in_range(self):
        schedule = ProposerSchedule(10, "random", seed=1)
        for slot in range(50):
            self.assertIn(schedule.proposer_for(slot), range(10))

    def test_random_proposer_is_idempotent(self):
        # A slot has one proposer. The builder is asked once to build and again
        # by metrics and the trace, so drawing per call would attribute one
        # block to several proposers.
        schedule = ProposerSchedule(10, "random", seed=1)
        first_pass = [schedule.proposer_for(slot) for slot in range(20)]
        second_pass = [schedule.proposer_for(slot) for slot in range(20)]
        self.assertEqual(first_pass, second_pass)
        # Order of enquiry must not matter either.
        self.assertEqual(schedule.proposer_for(7), first_pass[7])

    def test_random_proposer_spreads_across_nodes(self):
        schedule = ProposerSchedule(8, "random", seed="spread")
        chosen = {schedule.proposer_for(slot) for slot in range(200)}
        self.assertEqual(chosen, set(range(8)))

    def test_bad_configuration_rejected(self):
        with self.assertRaises(ValueError):
            ProposerSchedule(10, "mystery")
        with self.assertRaises(ValueError):
            ProposerSchedule(10, "fixed", fixed_id=10)


if __name__ == "__main__":
    unittest.main()
