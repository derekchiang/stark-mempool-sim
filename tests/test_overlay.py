#!/usr/bin/env python3
"""Overlay construction: degree, simplicity, connectivity, and the supported
(N, d) domain."""

import random
import unittest

from starksim.network import (Overlay, build_random_regular, can_be_connected,
                              is_connected, validate_degree)

from tests.support import two_node_overlay


class TestDomain(unittest.TestCase):
    def test_degree_beyond_node_count_rejected(self):
        with self.assertRaises(ValueError):
            validate_degree(3, 4)

    def test_odd_degree_times_odd_nodes_rejected(self):
        # N*d must be even: every edge adds one to two nodes' degrees.
        with self.assertRaises(ValueError):
            validate_degree(5, 3)
        validate_degree(6, 3)          # even N, odd d is fine

    def test_negative_and_empty_rejected(self):
        with self.assertRaises(ValueError):
            validate_degree(10, -1)
        with self.assertRaises(ValueError):
            validate_degree(0, 0)

    def test_connectability_of_low_degrees(self):
        # d=0 is isolated nodes, d=1 is a matching, d>=2 admits a cycle.
        self.assertTrue(can_be_connected(1, 0))
        self.assertFalse(can_be_connected(4, 0))
        self.assertTrue(can_be_connected(2, 1))
        self.assertFalse(can_be_connected(4, 1))
        self.assertTrue(can_be_connected(3, 2))
        self.assertTrue(can_be_connected(1000, 16))

    def test_unconnectable_request_is_refused_with_a_reason(self):
        rng = random.Random(1)
        with self.assertRaises(ValueError):
            build_random_regular(4, 1, rng)
        # ...unless the caller says it means it.
        adjacency = build_random_regular(4, 1, rng, require_connected=False)
        self.assertEqual([len(peers) for peers in adjacency], [1, 1, 1, 1])


class TestConstruction(unittest.TestCase):
    DOMAIN = ((1, 0), (2, 1), (3, 2), (4, 3), (10, 4), (11, 4),
              (100, 8), (256, 3), (1000, 16))

    def test_degree_simplicity_and_connectivity_across_the_domain(self):
        rng = random.Random("overlay-domain")
        for node_count, degree in self.DOMAIN:
            with self.subTest(node_count=node_count, degree=degree):
                adjacency = build_random_regular(node_count, degree, rng)
                self.assertEqual(len(adjacency), node_count)
                # every node has exactly the requested degree
                for peers in adjacency:
                    self.assertEqual(len(peers), degree)
                # simple: no self-loops, no repeated neighbour
                for node, peers in enumerate(adjacency):
                    self.assertNotIn(node, peers)
                    self.assertEqual(len(set(peers)), len(peers))
                # symmetric
                for node, peers in enumerate(adjacency):
                    for other in peers:
                        self.assertIn(node, adjacency[other])
                self.assertTrue(is_connected([set(p) for p in adjacency]))

    def test_same_seed_same_graph(self):
        first = build_random_regular(50, 6, random.Random("same"))
        second = build_random_regular(50, 6, random.Random("same"))
        self.assertEqual(first, second)

    def test_different_seed_different_graph(self):
        first = build_random_regular(50, 6, random.Random("one"))
        second = build_random_regular(50, 6, random.Random("two"))
        self.assertNotEqual(first, second)

    def test_swaps_actually_randomise(self):
        # The circulant we start from has every neighbour within d/2 hops of a
        # node's index; after swapping, long-range edges must exist, or the
        # "random" in random-regular is doing nothing.
        adjacency = build_random_regular(200, 4, random.Random("mixing"))
        long_range = 0
        for node, peers in enumerate(adjacency):
            for other in peers:
                separation = abs(node - other)
                circular = min(separation, 200 - separation)
                if circular > 10:
                    long_range += 1
        self.assertGreater(long_range, 100)


class TestDegreeTwo(unittest.TestCase):
    """Degree 2 is a single cycle, and cannot be reached by swapping.

    A 2-regular graph is a disjoint union of cycles, and a double-edge swap on
    one cycle splits it in two, so the swap loop drifts away from connectivity.
    Before the fix, 13 of 30 seeds failed outright at N=1000.
    """

    def test_every_seed_gives_one_connected_cycle(self):
        for seed in range(30):
            adjacency = build_random_regular(1000, 2, random.Random(seed))
            self.assertTrue(all(len(peers) == 2 for peers in adjacency))
            self.assertTrue(is_connected([set(peers) for peers in adjacency]))

    def test_it_really_is_a_cycle_not_just_connected(self):
        # A connected 2-regular graph on N nodes has exactly N edges and one
        # cycle through all of them.
        adjacency = build_random_regular(50, 2, random.Random(3))
        edges = sum(len(peers) for peers in adjacency) // 2
        self.assertEqual(edges, 50)
        walk = [0, adjacency[0][0]]
        while len(walk) < 50:
            previous, current = walk[-2], walk[-1]
            following = [p for p in adjacency[current] if p != previous]
            walk.append(following[0])
        self.assertEqual(sorted(walk), list(range(50)))

    def test_the_cycle_is_randomised(self):
        # Not the circulant: consecutive ids must not be neighbours everywhere.
        adjacency = build_random_regular(200, 2, random.Random("cycle"))
        consecutive = 0
        for node, peers in enumerate(adjacency):
            for peer in peers:
                if abs(node - peer) == 1:
                    consecutive += 1
        self.assertLess(consecutive, 40)


class TestLatencyDerivation(unittest.TestCase):
    """Per-edge latency must not depend on the topology it sits in."""

    def test_same_edge_same_latency_across_topologies(self):
        # Adding nodes or changing the swap count must not reshuffle latencies,
        # or parameter sweeps stop being comparable.
        sparse = Overlay.random_regular(60, 4, random.Random(1),
                                        base_latency=100.0, jitter_frac=0.3,
                                        latency_seed=11)
        dense = Overlay.random_regular(60, 10, random.Random(77),
                                       base_latency=100.0, jitter_frac=0.3,
                                       latency_seed=11)
        bigger = Overlay.random_regular(400, 4, random.Random(5),
                                        base_latency=100.0, jitter_frac=0.3,
                                        latency_seed=11)
        for pair in ((0, 5), (7, 41), (12, 13)):
            expected = sparse.latency(*pair)
            self.assertEqual(dense.latency(*pair), expected)
            self.assertEqual(bigger.latency(*pair), expected)

    def test_latency_seed_changes_all_edges(self):
        first = Overlay.random_regular(40, 4, random.Random(1),
                                       base_latency=100.0, jitter_frac=0.3,
                                       latency_seed=1)
        second = Overlay.random_regular(40, 4, random.Random(1),
                                        base_latency=100.0, jitter_frac=0.3,
                                        latency_seed=2)
        differences = 0
        for node in range(40):
            for peer in first.peers(node):
                if first.latency(node, peer) != second.latency(node, peer):
                    differences += 1
        self.assertGreater(differences, 100)

    def test_derivation_is_stable_across_processes(self):
        # Hashed, not hash()ed: the built-in hash of a string is randomised per
        # process, so a value derived from it would not reproduce across runs.
        overlay = two_node_overlay(100.0,
                                         jitter_frac=0.5, latency_seed=42)
        self.assertAlmostEqual(overlay.latency(0, 1), 140.05490979101702,
                               places=9)


class TestOverlayLatency(unittest.TestCase):
    def test_latency_is_symmetric_and_fixed_per_edge(self):
        rng = random.Random("latency")
        overlay = Overlay.random_regular(30, 4, rng, base_latency=100.0,
                                         jitter_frac=0.2)
        for node in range(30):
            for peer in overlay.peers(node):
                forward = overlay.latency(node, peer)
                backward = overlay.latency(peer, node)
                self.assertEqual(forward, backward)
                # jitter is additive on top of the base, never subtractive
                self.assertGreaterEqual(forward, 100.0)
                self.assertLessEqual(forward, 120.0)

    def test_repeated_queries_return_the_same_value(self):
        # Per-edge, not per-message: asking twice must not resample, or two
        # messages over one link could arrive out of order.
        rng = random.Random("stability")
        overlay = Overlay.random_regular(20, 4, rng, base_latency=50.0,
                                         jitter_frac=0.5)
        peer = overlay.peers(0)[0]
        values = {overlay.latency(0, peer) for _ in range(10)}
        self.assertEqual(len(values), 1)

    def test_zero_jitter_gives_the_base_everywhere(self):
        rng = random.Random("flat")
        overlay = Overlay.random_regular(20, 4, rng, base_latency=100.0)
        for node in range(20):
            for peer in overlay.peers(node):
                self.assertEqual(overlay.latency(node, peer), 100.0)


if __name__ == "__main__":
    unittest.main()
