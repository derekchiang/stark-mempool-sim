#!/usr/bin/env python3
"""Supersession by provenance: the design's Part I rule as selectable policies
(`docs/history/networking-plan.md`).

The design says a newer wrapper from the same peer supersedes the older one.
Whether that is safe is not obvious: a prover drops dependencies it knows are
settled, so a newer claim can be *narrower* than the older one it replaces, and
superseding unconditionally can lose evidence. So the rule is a strategy family
-- keep_all, newest_per_peer, newest_if_covered -- and these tests pin down
what each one drops, keeps, and counts.
"""

import io
import json
import unittest

from starksim.arena import Arena, KIND_PQ
from starksim.chain import ChainState
from starksim.costs import CostTable
from starksim.netsim import NetworkSim
from starksim.network import Overlay
from starksim.payload import SizeBreakdown, WireMessage, WrapperCore, \
    WrapperPayload
from starksim.strategies import get, names

from tests.support import make_node, parse_jsonl, two_node_overlay


def node_and_chain(node_id=0, peers=(1, 2), chain=None):
    if chain is None:
        chain = ChainState()
    return make_node(node_id, peers, chain=chain), chain


def an_aggregate(arena, leaves, claimed, born=1.0, creator=1):
    return arena.new_aggregate(leaves, born, creator, claimed=claimed)


class TestTheFamily(unittest.TestCase):
    def test_the_three_policies_are_registered(self):
        self.assertEqual(names("supersede"),
                         ["keep_all", "newest_if_covered", "newest_per_peer"])

    def test_an_unknown_policy_is_rejected_at_construction(self):
        overlay = two_node_overlay(10.0)
        with self.assertRaises(KeyError):
            NetworkSim(overlay, supersede_policy="sideways")


class TestKeepAll(unittest.TestCase):
    def test_nothing_is_ever_superseded(self):
        policy = get("supersede", "keep_all")
        node, _chain = node_and_chain()
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(2)]
        old = an_aggregate(arena, leaves[:1], claimed=0b01)
        new = an_aggregate(arena, leaves, claimed=0b11)
        node.admit(old, 1.0)
        node.supersede_from_peer(1, old, arena, policy)
        node.admit(new, 2.0)
        dropped = node.supersede_from_peer(1, new, arena, policy)
        self.assertEqual(dropped, [])
        self.assertIn(old.id, node.view.held)
        self.assertIn(new.id, node.view.held)
        self.assertEqual(node.superseded, 0)

    def test_a_gone_object_does_not_linger_in_tracking(self):
        # Under a policy that keeps everything, a gone object must still fall
        # out of the tracking list: it left the view by another route, and
        # tracking it for ever would grow without bound and keep putting a
        # dead object to the policy on every wrapper.
        policy = get("supersede", "keep_all")
        node, _chain = node_and_chain()
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(2)]
        old = an_aggregate(arena, leaves[:1], claimed=0b01)
        new = an_aggregate(arena, leaves, claimed=0b11)
        node.admit(old, 1.0)
        node.supersede_from_peer(1, old, arena, policy)
        del node.view.held[old.id]
        node.admit(new, 2.0)
        node.supersede_from_peer(1, new, arena, policy)
        self.assertEqual(node.view.wrapper_provenance[1], [new.id])


class TestNewestPerPeer(unittest.TestCase):
    def test_the_same_peers_newer_wrapper_wins_outright(self):
        policy = get("supersede", "newest_per_peer")
        node, _chain = node_and_chain()
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(2)]
        # The new claim does NOT cover the old one: this policy drops anyway,
        # which is exactly what distinguishes it from newest_if_covered.
        old = an_aggregate(arena, leaves[:1], claimed=0b01)
        new = an_aggregate(arena, leaves[1:], claimed=0b10)
        node.admit(old, 1.0)
        node.supersede_from_peer(1, old, arena, policy)
        node.admit(new, 2.0)
        dropped = node.supersede_from_peer(1, new, arena, policy)
        self.assertEqual(dropped, [old.id])
        self.assertNotIn(old.id, node.view.held)
        self.assertIn(new.id, node.view.held)
        self.assertEqual(node.superseded, 1)

    def test_a_different_peer_supersedes_nothing(self):
        policy = get("supersede", "newest_per_peer")
        node, _chain = node_and_chain()
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(2)]
        old = an_aggregate(arena, leaves[:1], claimed=0b01)
        new = an_aggregate(arena, leaves, claimed=0b11)
        node.admit(old, 1.0)
        node.supersede_from_peer(1, old, arena, policy)
        node.admit(new, 2.0)
        dropped = node.supersede_from_peer(2, new, arena, policy)
        self.assertEqual(dropped, [])
        self.assertIn(old.id, node.view.held)
        self.assertIn(new.id, node.view.held)

    def test_an_object_gone_by_another_route_is_forgotten_not_counted(self):
        policy = get("supersede", "newest_per_peer")
        node, _chain = node_and_chain()
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(2)]
        old = an_aggregate(arena, leaves[:1], claimed=0b01)
        new = an_aggregate(arena, leaves, claimed=0b11)
        node.admit(old, 1.0)
        node.supersede_from_peer(1, old, arena, policy)
        # Evicted, pruned or folded away in the meantime: whatever removed it
        # already counted the loss, and supersession must not count it again.
        del node.view.held[old.id]
        node.admit(new, 2.0)
        dropped = node.supersede_from_peer(1, new, arena, policy)
        self.assertEqual(dropped, [])
        self.assertEqual(node.superseded, 0)
        self.assertEqual(node.view.wrapper_provenance[1], [new.id])


class TestNewestIfCovered(unittest.TestCase):
    def test_a_still_useful_claim_protects_the_older_aggregate(self):
        policy = get("supersede", "newest_if_covered")
        node, _chain = node_and_chain()
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(3)]
        # The old aggregate claims leaf 0, which the new one dropped and which
        # nothing has settled. Dropping the old one would lose evidence.
        old = an_aggregate(arena, leaves[:2], claimed=0b011)
        new = an_aggregate(arena, leaves[1:], claimed=0b110)
        node.admit(old, 1.0)
        node.supersede_from_peer(1, old, arena, policy)
        node.admit(new, 2.0)
        dropped = node.supersede_from_peer(1, new, arena, policy)
        self.assertEqual(dropped, [])
        self.assertIn(old.id, node.view.held)

    def test_a_settled_surplus_no_longer_protects(self):
        policy = get("supersede", "newest_if_covered")
        node, chain = node_and_chain()
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(3)]
        old = an_aggregate(arena, leaves[:2], claimed=0b011)
        new = an_aggregate(arena, leaves[1:], claimed=0b110)
        node.admit(old, 1.0)
        node.supersede_from_peer(1, old, arena, policy)
        # Leaf 0 settles: the old claim's surplus over the new claim is now
        # dead weight, so the new aggregate subsumes it.
        chain.expire(0b001)
        node.admit(new, 2.0)
        dropped = node.supersede_from_peer(1, new, arena, policy)
        self.assertEqual(dropped, [old.id])
        self.assertNotIn(old.id, node.view.held)
        self.assertEqual(node.superseded, 1)

    def test_a_survivor_is_reconsidered_against_the_next_wrapper(self):
        policy = get("supersede", "newest_if_covered")
        node, chain = node_and_chain()
        arena = Arena()
        leaves = [arena.new_leaf(KIND_PQ, 0.0) for _ in range(3)]
        old = an_aggregate(arena, leaves[:2], claimed=0b011)
        middle = an_aggregate(arena, leaves[1:], claimed=0b110)
        node.admit(old, 1.0)
        node.supersede_from_peer(1, old, arena, policy)
        node.admit(middle, 2.0)
        # Leaf 0 is still live here, so the old aggregate survives this one.
        self.assertEqual(node.supersede_from_peer(1, middle, arena, policy),
                         [])
        chain.expire(0b001)
        third = an_aggregate(arena, leaves[1:], claimed=0b110)
        node.admit(third, 3.0)
        dropped = node.supersede_from_peer(1, third, arena, policy)
        # Both fall at once: the old one because its surplus settled, the
        # middle one because the third claims everything it did.
        self.assertEqual(sorted(dropped), sorted([old.id, middle.id]))
        self.assertEqual(node.view.wrapper_provenance[1], [third.id])


def two_nodes(**kwargs):
    overlay = two_node_overlay(10.0)
    defaults = dict(seed=1, costs=CostTable(verify_recv=0.0))
    defaults.update(kwargs)
    return NetworkSim(overlay, **defaults)


def wrapper_from(sim, sender, receiver, aggregate, wrapper_id, tick):
    claimed = sim.arena.claim(aggregate)
    core = WrapperCore(wrapper_id=wrapper_id,
                       proof_object_id=aggregate.id, creator=sender,
                       tick=tick, mode=1, tx_mask=claimed,
                       dep_count=claimed.bit_count())
    return WireMessage(sender=sender, receiver=receiver,
                       payload=WrapperPayload(core=core, body_mask=0,
                                              size=SizeBreakdown()))


class TestOnTheWirePath(unittest.TestCase):
    def test_the_default_keeps_both_and_the_header_says_so(self):
        buffer = io.StringIO()
        sim = two_nodes(trace=buffer)
        leaves = [sim.arena.new_leaf(KIND_PQ, 0.0) for _ in range(2)]
        old = an_aggregate(sim.arena, leaves[:1], claimed=0b01)
        new = an_aggregate(sim.arena, leaves, claimed=0b11)
        sim._deliver_wrapper(wrapper_from(sim, 1, 0, old, 1, 0))
        sim._deliver_wrapper(wrapper_from(sim, 1, 0, new, 2, 1))
        node = sim.nodes[0]
        self.assertIn(old.id, node.view.held)
        self.assertIn(new.id, node.view.held)
        header = json.loads(buffer.getvalue().split("\n")[0])
        self.assertEqual(header["supersede_policy"], "keep_all")

    def test_a_newer_wrapper_supersedes_and_the_trace_records_why(self):
        buffer = io.StringIO()
        sim = two_nodes(trace=buffer, supersede_policy="newest_per_peer")
        leaves = [sim.arena.new_leaf(KIND_PQ, 0.0) for _ in range(2)]
        old = an_aggregate(sim.arena, leaves[:1], claimed=0b01)
        new = an_aggregate(sim.arena, leaves, claimed=0b11)
        sim._deliver_wrapper(wrapper_from(sim, 1, 0, old, 1, 0))
        sim._deliver_wrapper(wrapper_from(sim, 1, 0, new, 2, 1))
        node = sim.nodes[0]
        self.assertNotIn(old.id, node.view.held)
        self.assertIn(new.id, node.view.held)
        self.assertEqual(sim.metrics()["superseded"], 1)
        events = parse_jsonl(buffer.getvalue())
        drops = [event for event in events
                 if event["e"] == "drop" and event["why"] == "superseded"]
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0]["o"], [old.id])
        self.assertEqual(drops[0]["n"], 0)

    def test_a_duplicate_from_a_second_peer_joins_that_peers_stream(self):
        # A proof delivered first by peer 1 and again by peer 2 was tracked
        # only under peer 1, so peer 2's newer wrapper could never supersede
        # it -- violating the per-peer stream semantics the policy family
        # exists to express.
        overlay = Overlay.from_adjacency([[1, 2], [0], [0]], base_latency=10.0)
        sim = NetworkSim(overlay, seed=1, costs=CostTable(verify_recv=0.0),
                         supersede_policy="newest_per_peer")
        leaves = [sim.arena.new_leaf(KIND_PQ, 0.0) for _ in range(2)]
        old = an_aggregate(sim.arena, leaves[:1], claimed=0b01)
        new = an_aggregate(sim.arena, leaves, claimed=0b11)
        sim._deliver_wrapper(wrapper_from(sim, 1, 0, old, 1, 0))
        # The same proof again, now from peer 2: a duplicate, but it still
        # enters peer 2's wrapper stream.
        sim._deliver_wrapper(wrapper_from(sim, 2, 0, old, 2, 0))
        sim._deliver_wrapper(wrapper_from(sim, 2, 0, new, 3, 1))
        node = sim.nodes[0]
        self.assertNotIn(old.id, node.view.held)
        self.assertIn(new.id, node.view.held)

    def test_provenance_is_the_sender_not_the_receiver(self):
        # Two senders delivering to one node: keyed by anything other than the
        # sender, these would land in one bucket and supersede across peers.
        overlay = Overlay.from_adjacency([[1, 2], [0], [0]], base_latency=10.0)
        sim = NetworkSim(overlay, seed=1, costs=CostTable(verify_recv=0.0),
                         supersede_policy="newest_per_peer")
        leaves = [sim.arena.new_leaf(KIND_PQ, 0.0) for _ in range(2)]
        old = an_aggregate(sim.arena, leaves[:1], claimed=0b01)
        new = an_aggregate(sim.arena, leaves, claimed=0b11)
        sim._deliver_wrapper(wrapper_from(sim, 1, 0, old, 1, 0))
        sim._deliver_wrapper(wrapper_from(sim, 2, 0, new, 2, 0))
        node = sim.nodes[0]
        self.assertIn(old.id, node.view.held)
        self.assertIn(new.id, node.view.held)
        self.assertEqual(node.superseded, 0)


if __name__ == "__main__":
    unittest.main()
