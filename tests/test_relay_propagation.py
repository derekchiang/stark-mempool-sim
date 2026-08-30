#!/usr/bin/env python3
"""Store-and-forward flooding over latency-only links.

The two acceptance criteria from the plan -- complete graph admits at t and
t + l, line graph admits at the summed hop latencies -- are special cases of one
property, which is what most of this file checks:

    with links that carry latency and nothing else, a node's admission time is
    the shortest *latency* path from the object's origin.

This property applies only when egress and computation costs are disabled;
either cost adds delay that a shortest-path calculation cannot see.
"""

import heapq
import random
import unittest

from starksim.arena import KIND_PQ, KIND_PRIVACY
from starksim.netsim import NetworkSim
from starksim.network import Overlay

from tests.support import line_adjacency, two_node_overlay


def complete_graph(node_count):
    adjacency = []
    for node in range(node_count):
        adjacency.append([other for other in range(node_count)
                          if other != node])
    return adjacency


def line_graph(node_count):
    return line_adjacency(node_count)


def shortest_latency_paths(overlay, origin):
    """Dijkstra over link latencies: the earliest an object could possibly
    arrive, given store-and-forward with no other costs."""
    distance = {origin: 0.0}
    frontier = [(0.0, origin)]
    while frontier:
        so_far, node = heapq.heappop(frontier)
        if so_far > distance.get(node, float("inf")):
            continue
        for peer in overlay.peers(node):
            candidate = so_far + overlay.latency(node, peer)
            if candidate < distance.get(peer, float("inf")):
                distance[peer] = candidate
                heapq.heappush(frontier, (candidate, peer))
    return distance


class TestAcceptanceCriteria(unittest.TestCase):
    def test_complete_graph_admits_at_t_then_exactly_t_plus_l(self):
        overlay = Overlay.from_adjacency(complete_graph(6), base_latency=100.0)
        sim = NetworkSim(overlay, seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = sim.arena.objects[0]
        times = sim.admission_times(obj)
        self.assertEqual(times[0], 0.0)                  # the creator, at once
        self.assertEqual(times[1:], [100.0] * 5)         # everyone else, one hop
        sim.assert_invariants()

    def test_complete_graph_with_a_later_birth(self):
        overlay = Overlay.from_adjacency(complete_graph(4), base_latency=25.0)
        sim = NetworkSim(overlay, seed=1)
        sim.schedule_birth(500.0, KIND_PRIVACY, node_id=2)
        sim.run()
        obj = sim.arena.objects[0]
        self.assertEqual(obj.born, 500.0)
        times = sim.admission_times(obj)
        self.assertEqual(times[2], 500.0)
        self.assertEqual([times[i] for i in (0, 1, 3)], [525.0] * 3)

    def test_line_graph_admits_at_summed_hop_latencies(self):
        overlay = Overlay.from_adjacency(line_graph(5), base_latency=100.0)
        sim = NetworkSim(overlay, seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = sim.arena.objects[0]
        self.assertEqual(sim.admission_times(obj),
                         [0.0, 100.0, 200.0, 300.0, 400.0])

    def test_line_graph_with_unequal_edges_sums_the_actual_path(self):
        # Jitter makes every edge a different length, so this checks summation
        # rather than multiplication by a constant.
        rng = random.Random("line-jitter")
        overlay = Overlay.from_adjacency(line_graph(6), base_latency=100.0,
                                         jitter_frac=0.5)
        sim = NetworkSim(overlay, seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = sim.arena.objects[0]
        expected = [0.0]
        running = 0.0
        for hop in range(5):
            running += overlay.latency(hop, hop + 1)
            expected.append(running)
        for node, (observed, wanted) in enumerate(
                zip(sim.admission_times(obj), expected)):
            self.assertAlmostEqual(observed, wanted, places=9,
                                   msg=f"node {node}")

    def test_admission_equals_shortest_latency_path(self):
        # The general form of both criteria above, on a random regular graph
        # with unequal edges and many competing paths.
        rng = random.Random("dijkstra")
        overlay = Overlay.random_regular(60, 4, rng, base_latency=100.0,
                                         jitter_frac=0.3)
        for origin in (0, 17, 59):
            with self.subTest(origin=origin):
                sim = NetworkSim(overlay, seed=1)
                sim.schedule_birth(0.0, KIND_PQ, node_id=origin)
                sim.run()
                obj = sim.arena.objects[0]
                expected = shortest_latency_paths(overlay, origin)
                observed = sim.admission_times(obj)
                for node in range(overlay.node_count):
                    self.assertAlmostEqual(observed[node], expected[node],
                                           places=9, msg=f"node {node}")

    def test_no_node_holds_an_object_before_it_is_born(self):
        rng = random.Random("unborn")
        overlay = Overlay.random_regular(40, 4, rng, base_latency=80.0,
                                         jitter_frac=0.2)
        sim = NetworkSim(overlay, seed=7)
        for index in range(20):
            kind = KIND_PQ if index % 2 else KIND_PRIVACY
            sim.schedule_birth(index * 37.0, kind)
        # Check between every event, not only at the end.
        while sim.engine.run(max_events=1):
            sim.assert_invariants()
        self.assertEqual(len(sim.arena.objects), 20)


class TestFlooding(unittest.TestCase):
    def test_every_node_admits_exactly_once(self):
        rng = random.Random("once")
        overlay = Overlay.random_regular(50, 6, rng, base_latency=100.0)
        sim = NetworkSim(overlay, seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = sim.arena.objects[0]
        self.assertEqual(sim.residency_fraction(obj), 1.0)
        for node in sim.nodes:
            self.assertEqual(len(node.view.held), 1)
            self.assertEqual(len(node.view.seen), 1)

    def test_duplicates_are_counted_not_silently_dropped(self):
        # On a clique of 5, the creator sends 4 and each receiver relays to 3
        # others: 16 messages for 5 admissions, so 12 are duplicates. Gossip
        # waste is a measurement, not an accident.
        overlay = Overlay.from_adjacency(complete_graph(5), base_latency=100.0)
        sim = NetworkSim(overlay, seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        self.assertEqual(sim.messages_offered, 16)
        self.assertEqual(sim.messages_delivered, 16)
        self.assertEqual(sim.duplicates, 12)
        total_duplicates = sum(node.view.duplicates for node in sim.nodes)
        self.assertEqual(total_duplicates, 12)

    def test_relay_excludes_the_sender(self):
        # A relays to B; B must not send it back to A.
        overlay = Overlay.from_adjacency(line_graph(3), base_latency=10.0)
        sim = NetworkSim(overlay, seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        # 0 -> 1, then 1 -> 2 only. Three admissions, two messages.
        self.assertEqual(sim.messages_offered, 2)
        self.assertEqual(sim.duplicates, 0)

    def test_flooding_terminates_on_a_dense_graph(self):
        # Without duplicate suppression this would not halt at all.
        rng = random.Random("terminate")
        overlay = Overlay.random_regular(30, 8, rng, base_latency=10.0)
        sim = NetworkSim(overlay, seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        processed = sim.run()
        self.assertEqual(sim.residency_fraction(sim.arena.objects[0]), 1.0)
        # One birth, then two events per message: the sender's egress finishing
        # with it, and its delivery one latency later.
        self.assertEqual(processed, 1 + 2 * sim.messages_offered)

    def test_disconnected_component_never_learns(self):
        # Two separate triangles: an object born in one cannot reach the other.
        adjacency = [[1, 2], [0, 2], [0, 1], [4, 5], [3, 5], [3, 4]]
        overlay = Overlay.from_adjacency(adjacency, base_latency=10.0)
        self.assertFalse(overlay.is_connected())
        sim = NetworkSim(overlay, seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = sim.arena.objects[0]
        times = sim.admission_times(obj)
        self.assertEqual(times[:3], [0.0, 10.0, 10.0])
        self.assertEqual(times[3:], [None, None, None])
        self.assertAlmostEqual(sim.residency_fraction(obj), 0.5)


class TestFloodingCost(unittest.TestCase):
    """What naive flooding costs, stated as a formula rather than a vibe.

    For one object on a connected d-regular graph of N nodes: the origin sends
    to all `d` of its peers, and each of the other `N-1` nodes admits once and
    relays to its `d-1` peers other than the one it heard from. So

        messages = d + (N-1)*(d-1)
        useful   = N-1                 (one admission per non-origin node)

    and the duplicate share is `1 - (N-1)/messages`, which tends to `1 - 1/(d-1)`
    as N grows: 50% at degree 3, 86% at degree 8. This is the baseline for
    comparing relay policies and isolates duplicate suppression from wrapper
    sizing.
    """

    def test_duplicate_share_matches_the_formula(self):
        rng = random.Random("flood-cost")
        for degree in (3, 4, 8):
            with self.subTest(degree=degree):
                node_count = 300
                overlay = Overlay.random_regular(node_count, degree, rng,
                                                 base_latency=10.0)
                sim = NetworkSim(overlay, seed=1)
                sim.schedule_birth(0.0, KIND_PQ, node_id=0)
                sim.run()
                sent = degree + (node_count - 1) * (degree - 1)
                self.assertEqual(sim.messages_offered, sent)
                observed = sim.duplicates / sim.messages_offered
                expected = 1.0 - (node_count - 1) / sent
                self.assertAlmostEqual(observed, expected, places=9)
                # Useful deliveries are exactly one per node bar the origin.
                self.assertEqual(sim.messages_delivered - sim.duplicates,
                                 node_count - 1)


class TestChainNotifyDivergence(unittest.TestCase):
    """The overlay notifier gets exercised every phase, not just when used.

    Under `instant` every node's discard mask is identical always, so the
    per-node machinery never diverges and a bug in it cannot surface. Running at
    least one configuration where knowledge *does* differ is what keeps that
    honest while `instant` remains the default for measurements.
    """

    def test_overlay_notifier_diverges_and_stays_truthful(self):
        rng = random.Random("divergence")
        overlay = Overlay.random_regular(20, 4, rng, base_latency=50.0)
        sim = NetworkSim(overlay, seed=1, chain_notify="overlay")
        for index in range(10):
            sim.schedule_birth(index * 60.0, KIND_PQ)
        sim.run()
        self.assertTrue(sim.divergence_measurable())
        # Discard half the transactions on chain, then tell only some nodes.
        arena = sim.arena
        mask = 0
        for object_id in list(arena.objects)[:5]:
            mask |= arena.coverage(arena.objects[object_id])
        sim.chain.expire(mask)
        for node_id in range(0, 20, 2):
            sim.notifier.notify(node_id, sim.chain.discarded_mask())
        informed = sim.nodes[0].known_discarded()
        uninformed = sim.nodes[1].known_discarded()
        self.assertNotEqual(informed, uninformed)
        self.assertEqual(informed, mask)
        self.assertEqual(uninformed, 0)
        sim.assert_invariants()

    def test_instant_notifier_cannot_diverge(self):
        rng = random.Random("instant")
        overlay = Overlay.random_regular(12, 4, rng, base_latency=50.0)
        sim = NetworkSim(overlay, seed=1, chain_notify="instant")
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        sim.chain.expire(sim.arena.coverage(sim.arena.objects[0]))
        masks = {node.known_discarded() for node in sim.nodes}
        self.assertEqual(len(masks), 1)
        self.assertFalse(sim.divergence_measurable())

    def test_unknown_chain_notify_rejected(self):
        overlay = two_node_overlay(1.0)
        with self.assertRaises(ValueError):
            NetworkSim(overlay, chain_notify="telepathy")


class TestEntryPolicy(unittest.TestCase):
    def test_uniform_entry_is_the_default_and_is_reproducible(self):
        rng = random.Random("entry")
        overlay = Overlay.random_regular(20, 4, rng, base_latency=50.0)
        entries = []
        for seed in (5, 5, 6):
            sim = NetworkSim(overlay, seed=seed)
            for index in range(30):
                sim.schedule_birth(index * 10.0, KIND_PQ)
            sim.run()
            # The entry node is the only node admitting at the birth instant.
            chosen = []
            for object_id, obj in sim.arena.objects.items():
                for node in sim.nodes:
                    if node.view.admitted_at(obj) == obj.born:
                        chosen.append(node.id)
                        break
            entries.append(chosen)
        self.assertEqual(entries[0], entries[1])       # same seed, same entries
        self.assertNotEqual(entries[0], entries[2])    # different seed differs
        self.assertGreater(len(set(entries[0])), 5)    # actually spread out


if __name__ == "__main__":
    unittest.main()
