#!/usr/bin/env python3
"""Message sizes, egress bandwidth, per-link caps, and fan-out fairness.

Bandwidth is enforced per node, not per link, so the tests here mostly check one
thing from different angles: sending to `d` peers costs `d` times the bytes
through one pipe, and that shows up as delay, queueing and utilisation.
"""

import random
import unittest

from starksim.arena import KIND_PQ, KIND_PRIVACY
from starksim.costs import CircuitCost, CostTable
from starksim.netsim import (RELAY_ANNOUNCE_FETCH, RELAY_FLOOD, NetworkSim)
from starksim.network import MSG_ANNOUNCE, MSG_WANT, Overlay, SizeModel, bytes_per_ms

from tests.support import make_sim, two_node_overlay


def two_nodes(base_latency, capacity, size_model=None, **kwargs):
    return make_sim(base_latency=base_latency, capacity=capacity,
                    size_model=size_model, **kwargs)


def star(node_count):
    """One hub joined to every leaf: the cleanest way to watch fan-out cost."""
    adjacency = [[peer for peer in range(1, node_count)]]
    for _ in range(1, node_count):
        adjacency.append([0])
    return adjacency


class TestSizeModel(unittest.TestCase):
    def test_sizes_come_from_the_eip_constants(self):
        sizes = SizeModel()
        self.assertEqual(sizes.triple, 96)          # eip-8288.md:57
        self.assertEqual(sizes.hash_ref, 32)        # :223
        self.assertEqual(sizes.message_size(MSG_ANNOUNCE, None), 32)
        self.assertEqual(sizes.message_size(MSG_WANT, None), 32)

    def test_a_privacy_tx_dwarfs_a_pq_signature(self):
        # The whole motivation for aggregation: a STARK is two orders of
        # magnitude larger than a signature.
        sizes = SizeModel()
        sim = two_nodes(10.0, None)
        pq = sim.arena.new_leaf(KIND_PQ, 0.0)
        privacy = sim.arena.new_leaf(KIND_PRIVACY, 0.0)
        self.assertGreater(sizes.object_size(privacy),
                           50 * sizes.object_size(pq))

    def test_aggregate_is_sized_by_distinct_dependencies(self):
        # `total` double-counts overlap on purpose, so sizing from it would
        # invent bytes no wrapper contains. Two nodes racing on the same pair
        # produce a merge with total 4 and a dependency list of 2.
        sizes = SizeModel()
        sim = two_nodes(10.0, None)
        first = sim.arena.new_leaf(KIND_PQ, 0.0)
        second = sim.arena.new_leaf(KIND_PQ, 0.0)
        twin_a = sim.arena.new_aggregate([first, second], 1.0, 0)
        twin_b = sim.arena.new_aggregate([first, second], 1.0, 1)
        merged = sim.arena.new_aggregate([twin_a, twin_b], 2.0, 2)
        self.assertEqual(merged.total, 4)
        distinct = sim.arena.coverage(merged).bit_count()
        self.assertEqual(distinct, 2)
        self.assertEqual(sizes.object_size(merged, distinct),
                         sizes.recursive_proof + 2 * sizes.triple)
        # Refusing to guess is the point: `total` must not be used by default.
        with self.assertRaises(ValueError):
            sizes.object_size(merged)

    def test_bytes_per_ms_conversion(self):
        # 1 Mbit/s is 125 bytes per millisecond.
        self.assertEqual(bytes_per_ms(1), 125.0)
        self.assertEqual(bytes_per_ms(10), 1250.0)


class TestAcceptanceCriteria(unittest.TestCase):
    def test_two_node_delivery_is_size_over_capacity_plus_latency(self):
        # The exact fixture the plan asks for.
        sizes = SizeModel()
        capacity = 1000.0            # bytes per unit time
        latency = 40.0
        sim = two_nodes(latency, capacity, sizes)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = sim.arena.objects[0]
        expected = sizes.object_size(obj) / capacity + latency
        self.assertAlmostEqual(sim.admission_times(obj)[1], expected, places=9)
        self.assertEqual(sim.admission_times(obj)[0], 0.0)

    def test_fanout_serialises_on_one_egress(self):
        # A hub sending to 4 leaves pays four transmission times, not one: the
        # k-th leaf is served after k transmissions plus one latency.
        sizes = SizeModel()
        capacity = 1000.0
        latency = 10.0
        overlay = Overlay.from_adjacency(star(5), base_latency=latency)
        sim = NetworkSim(overlay, seed=1, capacity=capacity, size_model=sizes)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = sim.arena.objects[0]
        transmission = sizes.object_size(obj) / capacity
        arrivals = sorted(t for t in sim.admission_times(obj)[1:])
        for index, arrival in enumerate(arrivals):
            self.assertAlmostEqual(arrival, (index + 1) * transmission + latency,
                                   places=9)

    def test_egress_utilisation_matches_offered_load(self):
        # Offer a known byte rate at one node and check the uplink is busy
        # exactly that share of the time.
        sizes = SizeModel(tx_body=0, triple=0, sig_witness=1000)
        capacity = 1000.0                       # one object per unit time
        overlay = two_node_overlay(1.0)
        sim = NetworkSim(overlay, seed=1, capacity=capacity, size_model=sizes)
        # One object every 4 units, each taking 1 unit to transmit -> 25% busy.
        for index in range(200):
            sim.schedule_birth(index * 4.0, KIND_PQ, node_id=0)
        horizon = 800.0
        sim.run(until=horizon)
        utilisation = sim.egress_utilisation(0, horizon)
        self.assertLess(abs(utilisation - 0.25), 0.02,
                        f"utilisation {utilisation:.4f} vs offered 0.25")

    def test_saturated_queue_grows_at_the_predicted_rate(self):
        # Not just "nondecreasing": the backlog has to grow at the rate work
        # arrives faster than it is served. Arrivals offer 1000 bytes per unit
        # against 500 of capacity, so the queue gains one job every two units.
        sizes = SizeModel(tx_body=0, triple=0, sig_witness=1000)
        overlay = two_node_overlay(1.0)
        sim = NetworkSim(overlay, seed=1, capacity=500.0, size_model=sizes)
        arrivals = 300
        for index in range(arrivals):
            sim.schedule_birth(float(index), KIND_PQ, node_id=0)
        samples = []
        for checkpoint in (100.0, 150.0, 200.0):
            sim.run(until=checkpoint)
            samples.append((checkpoint, sim.egress[0].queue_depth()))
        for elapsed, depth in samples:
            # offered jobs minus served jobs, one served every 2.0 units
            expected = elapsed - (elapsed / 2.0)
            self.assertAlmostEqual(depth, expected, delta=2.0,
                                   msg=f"at t={elapsed}")

    def test_saturated_egress_queue_grows_without_bound(self):
        # Offer more bytes than the uplink can carry and the queue must grow
        # monotonically.
        sizes = SizeModel(tx_body=0, triple=0, sig_witness=1000)
        overlay = two_node_overlay(1.0)
        sim = NetworkSim(overlay, seed=1, capacity=500.0, size_model=sizes)
        # Each object needs 2 units of transmission but arrives every 1 unit.
        for index in range(150):
            sim.schedule_birth(float(index), KIND_PQ, node_id=0)
        depths = []
        for checkpoint in (30.0, 60.0, 90.0, 120.0, 150.0):
            sim.run(until=checkpoint)
            depths.append(sim.egress[0].queue_depth())
        self.assertEqual(depths, sorted(depths))
        self.assertGreater(depths[-1], depths[0])
        self.assertGreater(sim.max_egress_depth(), 10)

    def test_peer_rotation_gives_every_peer_an_equal_first_slot(self):
        # One FIFO uplink plus a fixed peer order would put the same peer first
        # every time. Over a run each peer must lead equally often.
        node_count = 5
        overlay = Overlay.from_adjacency(star(node_count), base_latency=10.0)
        sim = NetworkSim(overlay, seed=1, capacity=1000.0)
        rounds = 40
        for index in range(rounds):
            sim.schedule_birth(index * 10_000.0, KIND_PQ, node_id=0)
        sim.run()
        # The hub's first-served peer per object is the one admitting earliest.
        first_counts = {}
        for obj in sim.arena.objects.values():
            times = sim.admission_times(obj)
            leaves = [(times[node], node) for node in range(1, node_count)
                      if times[node] is not None]
            first = min(leaves)[1]
            first_counts[first] = first_counts.get(first, 0) + 1
        self.assertEqual(sorted(first_counts), list(range(1, node_count)))
        self.assertEqual(set(first_counts.values()),
                         {rounds // (node_count - 1)})


class TestPerLinkCap(unittest.TestCase):
    def test_link_cap_is_a_second_serialising_stage(self):
        # With both caps on, a message pays egress then link then latency: the
        # tandem resource the plan warned is not free.
        sizes = SizeModel(tx_body=0, triple=0, sig_witness=1000)
        sim = two_nodes(5.0, capacity=1000.0, size_model=sizes,
                        link_capacity=500.0)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = sim.arena.objects[0]
        # 1000 bytes: 1.0 through a 1000/unit uplink, 2.0 through a 500/unit
        # link, then 5.0 of latency.
        self.assertAlmostEqual(sim.admission_times(obj)[1], 1.0 + 2.0 + 5.0,
                               places=9)

    def test_link_cap_off_by_default(self):
        sizes = SizeModel(tx_body=0, triple=0, sig_witness=1000)
        sim = two_nodes(5.0, capacity=1000.0, size_model=sizes)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = sim.arena.objects[0]
        self.assertAlmostEqual(sim.admission_times(obj)[1], 1.0 + 5.0, places=9)
        self.assertEqual(len(sim.link_servers), 0)


class TestUnmeteredMatchesLatencyOnly(unittest.TestCase):
    def test_no_capacity_means_latency_only(self):
        # The latency-only model remains reachable as a baseline whose
        # shortest-path property is independently checkable.
        overlay = Overlay.from_adjacency(star(4), base_latency=100.0)
        sim = NetworkSim(overlay, seed=1, capacity=None)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = sim.arena.objects[0]
        self.assertEqual(sim.admission_times(obj), [0.0, 100.0, 100.0, 100.0])

    def test_zero_capacity_rejected(self):
        with self.assertRaises(ValueError):
            two_nodes(1.0, capacity=0.0)


class TestRelayPolicies(unittest.TestCase):
    def test_announce_fetch_sends_one_body_per_node(self):
        # The point of the policy: a body crosses each link once, so the byte
        # count stops scaling with degree.
        rng = random.Random("policy")
        overlay = Overlay.random_regular(40, 6, rng, base_latency=10.0)
        sizes = SizeModel()
        results = {}
        for policy in (RELAY_FLOOD, RELAY_ANNOUNCE_FETCH):
            sim = NetworkSim(overlay, seed=1, capacity=None,
                             size_model=sizes, relay_policy=policy)
            sim.schedule_birth(0.0, KIND_PRIVACY, node_id=0)
            sim.run()
            self.assertEqual(sim.residency_fraction(sim.arena.objects[0]), 1.0)
            results[policy] = sim
        flood = results[RELAY_FLOOD]
        fetch = results[RELAY_ANNOUNCE_FETCH]
        # 39 non-origin nodes each receive exactly one body.
        self.assertEqual(fetch.bodies_requested, 39)
        self.assertEqual(fetch.offered_messages_by_kind["want"], 39)
        # Flooding pushes a 256 kB body down every edge instead.
        self.assertGreater(flood.bodies_pushed, 200)
        self.assertLess(fetch.total_sent_bytes(), flood.total_sent_bytes() / 4)

    def test_announce_fetch_costs_round_trips(self):
        # The trade the policy makes: fewer bytes, more hops of latency. A body
        # only starts moving after an announcement and a request.
        overlay = two_node_overlay(10.0)
        sim = NetworkSim(overlay, seed=1, capacity=None,
                         relay_policy=RELAY_ANNOUNCE_FETCH)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = sim.arena.objects[0]
        # announce (10) + want (10) + body (10) = three one-way trips.
        self.assertAlmostEqual(sim.admission_times(obj)[1], 30.0, places=9)

    def test_announce_fetch_requests_a_body_only_once(self):
        # Several peers announcing the same object must not produce several
        # bodies -- that is the waste the policy exists to remove.
        overlay = Overlay.from_adjacency(
            [[1, 2, 3], [0, 2, 3], [0, 1, 3], [0, 1, 2]], base_latency=10.0)
        sim = NetworkSim(overlay, seed=1, capacity=None,
                         relay_policy=RELAY_ANNOUNCE_FETCH)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        self.assertEqual(sim.bodies_requested, 3)
        for node in sim.nodes:
            self.assertEqual(len(node.view.held), 1)
            self.assertEqual(node.view.requested, set())

    def test_flood_wins_when_bodies_are_tiny(self):
        # Announce-then-fetch is not free: when a body is no bigger than its id,
        # the extra round trip is pure overhead. Neither policy dominates, which
        # is why both are kept.
        rng = random.Random("tiny")
        overlay = Overlay.random_regular(30, 4, rng, base_latency=10.0)
        tiny = SizeModel(tx_body=0, triple=0, sig_witness=32, hash_ref=32)
        totals = {}
        for policy in (RELAY_FLOOD, RELAY_ANNOUNCE_FETCH):
            sim = NetworkSim(overlay, seed=1, capacity=None, size_model=tiny,
                             relay_policy=policy)
            sim.schedule_birth(0.0, KIND_PQ, node_id=0)
            sim.run()
            totals[policy] = sim.total_sent_bytes()
        self.assertLess(totals[RELAY_FLOOD], totals[RELAY_ANNOUNCE_FETCH])

    def test_a_node_cannot_serve_a_body_it_does_not_have(self):
        # Answering a request straight from the object reference would let a node
        # serve a body it never had or has since dropped, which would make any
        # retention or eviction policy invisible: bodies would keep arriving
        # however aggressively they were discarded.
        overlay = Overlay.from_adjacency([[1], [0, 2], [1]], base_latency=10.0)
        sim = NetworkSim(overlay, seed=1, capacity=None,
                         relay_policy=RELAY_ANNOUNCE_FETCH)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run(until=5.0)                          # announcement in flight
        obj = sim.arena.objects[0]
        del sim.nodes[0].view.held[obj.id]          # the holder loses it
        sim.run()
        self.assertEqual(sim.unserved_wants, 1)
        self.assertFalse(sim.nodes[1].view.holds(obj))

    def test_link_capacity_must_be_positive(self):
        with self.assertRaises(ValueError):
            two_nodes(1.0, capacity=100.0, link_capacity=0.0)

    def test_unknown_policy_rejected(self):
        with self.assertRaises(ValueError):
            two_nodes(1.0, None, relay_policy="telepathy")


class TestByteAccounting(unittest.TestCase):
    def test_wire_bytes_cannot_exceed_physical_capacity(self):
        """The check that caught a published impossible number.

        Whatever the workload, a node cannot put more bytes on the wire than
        capacity x elapsed time. Counting bytes when they are queued rather than
        when they are sent breaks this, and on a saturated uplink it breaks it by
        enough to be obvious: the first reported figure for a 200-node run
        exceeded the aggregate uplink capacity by 1.55x.
        """
        sizes = SizeModel(tx_body=0, triple=0, sig_witness=1000)
        capacity = 500.0
        overlay = Overlay.from_adjacency(star(4), base_latency=1.0)
        sim = NetworkSim(overlay, seed=1, capacity=capacity, size_model=sizes)
        for index in range(300):
            sim.schedule_birth(float(index), KIND_PQ, node_id=0)
        horizon = 200.0
        sim.run(until=horizon)
        ceiling = capacity * horizon
        self.assertLessEqual(sim.wire_bytes(0), ceiling + 1e-9)
        # Saturated, so it should be right at the ceiling, and offered must be
        # well above it -- that gap is the queue.
        self.assertAlmostEqual(sim.wire_bytes(0), ceiling, places=6)
        self.assertGreater(sim.offered_bytes[0], ceiling * 2)

    def test_sent_is_whole_messages_and_wire_includes_the_partial_one(self):
        sizes = SizeModel(tx_body=0, triple=0, sig_witness=1000)
        sim = two_nodes(1.0, capacity=500.0, size_model=sizes)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        # One 1000-byte message takes 2.0 to transmit. Stop halfway.
        sim.run(until=1.0)
        self.assertEqual(sim.total_sent_bytes(), 0)      # nothing finished yet
        self.assertAlmostEqual(sim.wire_bytes(0), 500.0) # half of it is out
        sim.run(until=2.0)
        self.assertEqual(sim.total_sent_bytes(), 1000)
        self.assertAlmostEqual(sim.wire_bytes(0), 1000.0)

    def test_unmetered_egress_puts_everything_on_the_wire(self):
        sim = two_nodes(1.0, capacity=None)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        self.assertEqual(sim.wire_bytes(0), sim.offered_bytes[0])
        self.assertEqual(sim.total_sent_bytes(), sim.total_offered_bytes())

    def test_ingress_is_measured_per_node(self):
        rng = random.Random("ingress")
        overlay = Overlay.random_regular(20, 4, rng, base_latency=10.0)
        sim = NetworkSim(overlay, seed=1, capacity=None)
        for index in range(10):
            sim.schedule_birth(index * 100.0, KIND_PQ)
        sim.run()
        # Everything sent is received by exactly one node, so the two sides of
        # the network balance.
        self.assertEqual(sim.total_ingress_bytes(), sim.total_sent_bytes())
        self.assertEqual(sum(sim.ingress_messages), sim.messages_delivered)
        # Every node has an uplink and a downlink figure of its own.
        self.assertTrue(all(count > 0 for count in sim.ingress_messages))
        self.assertEqual(sum(sim.ingress_duplicate_bytes), sim.duplicate_bytes)

    def test_offered_bytes_by_kind_sums_to_offered_total(self):
        rng = random.Random("accounting")
        overlay = Overlay.random_regular(20, 4, rng, base_latency=10.0)
        for policy in (RELAY_FLOOD, RELAY_ANNOUNCE_FETCH):
            with self.subTest(policy=policy):
                sim = NetworkSim(overlay, seed=1, capacity=None,
                                 relay_policy=policy)
                for index in range(10):
                    sim.schedule_birth(index * 100.0, KIND_PQ)
                sim.run()
                self.assertEqual(sum(sim.offered_bytes_by_kind.values()),
                                 sim.total_sent_bytes())
                self.assertEqual(sum(sim.offered_messages_by_kind.values()),
                                 sim.messages_offered)

    def test_duplicate_bytes_are_attributed(self):
        rng = random.Random("dupbytes")
        overlay = Overlay.random_regular(30, 6, rng, base_latency=10.0)
        sim = NetworkSim(overlay, seed=1, capacity=None)
        sim.schedule_birth(0.0, KIND_PRIVACY, node_id=0)
        sim.run()
        # Most of what flooding sends is wasted, and the model says how much.
        self.assertGreater(sim.duplicate_bytes, sim.total_sent_bytes() * 0.7)


if __name__ == "__main__":
    unittest.main()


class TestTypedPayloads(unittest.TestCase):
    """Payloads carry identities and a decomposed size, not object references."""

    def test_a_payload_never_carries_the_object(self):
        # Being handed a live reference is how a node ends up able to act on
        # something it was never told about.
        from starksim.payload import AnnouncePayload, ObjectPayload
        sizes = SizeModel()
        sim = two_nodes(1.0, None)
        obj = sim.arena.new_leaf(KIND_PQ, 0.0)
        payloads = (sizes.announce(obj.id), sizes.want(obj.id),
                    sizes.object_payload(obj))
        self.assertEqual([payload.content_id for payload in payloads],
                         [obj.id, obj.id, obj.id])
        for payload in payloads:
            self.assertFalse(hasattr(payload, "obj"))

    def test_size_decomposes_into_its_parts(self):
        # A total that cannot be checked against its components is a number to
        # be believed rather than audited.
        sizes = SizeModel()
        sim = two_nodes(1.0, None)
        privacy = sim.arena.new_leaf(KIND_PRIVACY, 0.0)
        payload = sizes.object_payload(privacy)
        breakdown = payload.size
        self.assertEqual(breakdown.bodies, sizes.tx_body)
        self.assertEqual(breakdown.dependencies, sizes.triple)
        self.assertEqual(breakdown.proof, sizes.stark_witness)
        self.assertEqual(breakdown.total(),
                         sizes.tx_body + sizes.triple + sizes.stark_witness)

    def test_a_wrapper_is_sized_per_recipient(self):
        # The EIP's substitution: a peer believed to hold a body gets a hash.
        # Two peers therefore receive different-sized copies of one wrapper.
        from starksim.payload import WrapperCore
        sizes = SizeModel()
        core = WrapperCore(wrapper_id=1, proof_object_id=9, creator=0, tick=1,
                           mode=1, tx_mask=0b1111, dep_count=4)
        knows_nothing = sizes.wrapper(core, body_mask=0b1111)
        knows_everything = sizes.wrapper(core, body_mask=0b0000)
        self.assertEqual(knows_nothing.size.bodies, 4 * sizes.tx_body)
        self.assertEqual(knows_nothing.size.hashes, 0)
        self.assertEqual(knows_everything.size.bodies, 0)
        self.assertEqual(knows_everything.size.hashes, 4 * sizes.hash_ref)
        # One recursive proof either way: that is the bandwidth argument.
        self.assertEqual(knows_nothing.size.proof, sizes.recursive_proof)
        self.assertEqual(knows_everything.size.proof, sizes.recursive_proof)
        self.assertLess(knows_everything.size.total(),
                        knows_nothing.size.total())

    def test_mode_zero_pays_per_witness(self):
        # Mode 0 carries raw witnesses, so its size grows with dependencies
        # where mode 1 does not. That crossover is the thing to measure.
        from starksim.payload import WrapperCore
        sizes = SizeModel()
        small = WrapperCore(1, 9, 0, 1, 0, 0b11, 2)
        large = WrapperCore(2, 9, 0, 1, 0, (1 << 200) - 1, 200)
        # Mode 0's size is the sum of its dependencies' own witnesses, so the
        # caller states them; a count cannot say whether they are 2.5 kB
        # signatures or 256 kB leanSTARKs.
        self.assertEqual(
            sizes.wrapper(small, 0,
                          witness_bytes=2 * sizes.sig_witness).size.proof,
            2 * sizes.sig_witness)
        self.assertEqual(
            sizes.wrapper(large, 0,
                          witness_bytes=200 * sizes.sig_witness).size.proof,
            200 * sizes.sig_witness)
        mode_one = WrapperCore(3, 9, 0, 1, 1, (1 << 200) - 1, 200)
        self.assertLess(
            sizes.wrapper(mode_one, 0).size.total(),
            sizes.wrapper(large, 0,
                          witness_bytes=200 * sizes.sig_witness).size.total())
        # A mode 0 wrapper without its witness sizes is not sizeable at all.
        with self.assertRaises(ValueError):
            sizes.wrapper(small, 0)

    def test_a_wrapper_cannot_claim_bodies_it_does_not_cover(self):
        from starksim.payload import WrapperCore
        sizes = SizeModel()
        core = WrapperCore(1, 9, 0, 1, 1, 0b0011, 2)
        with self.assertRaises(ValueError):
            sizes.wrapper(core, body_mask=0b1000)

    def test_pushed_and_requested_bodies_are_counted_apart(self):
        rng = random.Random("solicited")
        overlay = Overlay.random_regular(20, 4, rng, base_latency=10.0)
        pushed = NetworkSim(overlay, seed=1, relay_policy=RELAY_FLOOD)
        pushed.schedule_birth(0.0, KIND_PQ, node_id=0)
        pushed.run()
        fetched = NetworkSim(overlay, seed=1,
                             relay_policy=RELAY_ANNOUNCE_FETCH)
        fetched.schedule_birth(0.0, KIND_PQ, node_id=0)
        fetched.run()
        self.assertEqual(fetched.bodies_pushed, 0)
        self.assertEqual(fetched.bodies_requested, 19)
        self.assertEqual(pushed.bodies_requested, 0)
        self.assertGreater(pushed.bodies_pushed, 19)


class TestWrapperModeSelection(unittest.TestCase):
    """Mode 0 concatenates raw witnesses, so it is available only to a node that
    holds them. Holding a body is not holding a witness: a wrapper carries
    bodies and hashes, and the witness travels only inside a raw object."""

    def one_run(self, mode, relay, kind):
        overlay = Overlay.random_regular(8, 4, random.Random("mode"),
                                         base_latency=10.0)
        sim = NetworkSim(overlay, seed=3, relay_policy=relay,
                         wrapper_mode=mode, size_model=SizeModel(),
                         costs=CostTable(verify_recv=1.0))
        for index in range(6):
            sim.schedule_birth(index * 200.0, kind)
        sim.schedule_ticks(8)
        sim.schedule_slots(2)
        sim.run(until=8000.0)
        return sim

    def test_a_lone_signature_is_cheaper_as_raw_witness(self):
        # One PQ dependency: 2 656 B of witness against a 262 144 B proof.
        sizes = SizeModel()
        self.assertLess(sizes.sig_witness + sizes.triple, sizes.recursive_proof)
        chosen = self.one_run("cheapest", "flood", KIND_PQ)
        self.assertGreater(chosen.mode_zero_wrappers, 0)

    def test_privacy_claims_never_choose_mode_zero(self):
        # A leanSTARK witness is itself the size of a recursive proof, so
        # concatenating even one never wins.
        chosen = self.one_run("cheapest", "flood", KIND_PRIVACY)
        self.assertEqual(chosen.mode_zero_wrappers, 0)

    def test_the_default_never_leaves_mode_one(self):
        default = self.one_run("recursive", "flood", KIND_PQ)
        self.assertEqual(default.mode_zero_wrappers, 0)

    def test_a_claim_wider_than_its_witnesses_stays_mode_one(self):
        # wrapper_only never ships an object, so a node holds witnesses only for
        # what it originated. Its claim covers what it folded from peers, which
        # it cannot concatenate -- and must not, since it never received them.
        wrapped = self.one_run("cheapest", "wrapper_only", KIND_PQ)
        self.assertEqual(wrapped.mode_zero_wrappers, 0)


class TestModeZeroEvidenceClass(unittest.TestCase):
    """Mode 0 carries witnesses, so it conveys no recursive proof.

    The distinction is not bookkeeping pedantry. A proof identity installed by a
    witness delivery can be sent back as a 32-byte reference to bytes that never
    crossed the wire, and it prices a later fold as verifying one nested proof
    instead of every signature behind it -- making mode 0 look cheap on the wire
    and free to aggregate, which is exactly backwards.
    """

    def a_run(self, economy):
        overlay = Overlay.random_regular(8, 4, random.Random("mode"),
                                         base_latency=10.0)
        sim = NetworkSim(overlay, seed=3, relay_policy="flood",
                         wrapper_mode="cheapest", wrapper_economy=economy,
                         size_model=SizeModel(),
                         costs=CostTable(verify_recv=1.0))
        for index in range(6):
            sim.schedule_birth(index * 200.0, KIND_PQ)
        sim.schedule_ticks(8)
        sim.schedule_slots(2)
        sim.run(until=8000.0)
        return sim

    def test_witnesses_never_become_a_verified_proof(self):
        sim = self.a_run("full")
        self.assertGreater(sim.mode_zero_wrappers, 0)
        witness_backed = 0
        for node in sim.nodes:
            witness_backed += len(node.view.witness_backed)
            # The two classes are disjoint, and neither can answer for the other.
            for proof_id in node.view.witness_backed:
                self.assertNotIn(proof_id, node.view.verified_proofs)
                self.assertFalse(node.can_resolve_proof(proof_id))
        self.assertGreater(witness_backed, 0)

    def test_no_peer_believes_a_witness_delivery_left_a_proof(self):
        # The belief is what licenses `proof_by_ref` and `skip_unchanged`. If it
        # formed from witnesses, a later emission would answer with a hash of
        # bytes the peer never received.
        sim = self.a_run("lean")
        for node in sim.nodes:
            for peer in node.peers:
                for proof_id in node.view.witness_backed:
                    self.assertFalse(sim.nodes[peer].peer_has_proof(node.id,
                                                                   proof_id))

    def test_folding_witnesses_prices_signatures_not_a_nested_proof(self):
        # One aggregate claiming three signatures. Held as a proof it is one
        # nested verification; held as witnesses it is three signatures, and the
        # circuit basis must charge accordingly.
        overlay = Overlay.from_adjacency([[1], [0]], base_latency=0.0)
        sim = NetworkSim(overlay, prove_cost_basis="circuit",
                         costs=CostTable(prove_recursive=CircuitCost(
                             ms_per_hash=1.0)))
        leaves = [sim.arena.new_leaf(KIND_PQ, 0.0) for _ in range(3)]
        claim = 0
        for leaf in leaves:
            claim |= 1 << leaf.leaf_index
        aggregate = sim.arena.new_aggregate(leaves, 0.0, 0, claimed=claim)
        node = sim.nodes[0]
        node.finish_receive(aggregate, 0.0, claim)

        job = _OneInputJob(aggregate.id)
        as_proof = sim._job_composition(node, job)
        node.view.witness_backed.add(aggregate.id)
        node.view.verified_proofs.discard(aggregate.id)
        as_witnesses = sim._job_composition(node, job)

        self.assertEqual(as_proof, (0, 0, 1))
        self.assertEqual(as_witnesses, (3, 0, 0))


class _OneInputJob:
    """The minimum a composition query reads: one arena input, no dependencies."""

    def __init__(self, object_id):
        self.inputs = (object_id,)
        self.depends_on = ()


class TestClaimSchemesAreNotPossession(unittest.TestCase):
    """Pricing a claim's raw evidence needs schemes; sending it needs witnesses.

    Both questions look like "ask the arena about this claim", and confusing them
    is how a node acquires evidence it never received. They are kept apart by
    what each is allowed to read: `schemes_in` for a size, held objects for an
    encoding a node can actually emit.
    """

    def test_schemes_come_from_the_claim_not_from_holding_it(self):
        # The dependency triple carries the scheme (eip-8288.md:67), so a node
        # holding a claim can price its witnesses without holding any.
        overlay = two_node_overlay(0.0)
        sim = NetworkSim(overlay)
        signature = sim.arena.new_leaf(KIND_PQ, 0.0)
        privacy = sim.arena.new_leaf(KIND_PRIVACY, 0.0)
        claim = (1 << signature.leaf_index) | (1 << privacy.leaf_index)

        sizes = SizeModel()
        self.assertEqual(sim.arena.schemes_in(claim), [KIND_PQ, KIND_PRIVACY])
        self.assertEqual(sim._mode_zero_bytes(claim),
                         sizes.sig_witness + sizes.stark_witness)
        # Nobody holds either leaf, and the size is unaffected: it is a property
        # of the claim.
        self.assertFalse(sim.nodes[0].view.held)

    def test_emitting_mode_zero_still_requires_the_witnesses(self):
        overlay = two_node_overlay(0.0)
        sim = NetworkSim(overlay, wrapper_mode="cheapest")
        leaf = sim.arena.new_leaf(KIND_PQ, 0.0)
        claim = 1 << leaf.leaf_index
        # Cheap enough to prefer, but this node holds nothing.
        self.assertLess(sim._mode_zero_bytes(claim),
                        sim.size_model.recursive_proof)
        self.assertFalse(sim._prefers_mode_zero(sim.nodes[0], claim))
        sim.nodes[0].finish_receive(leaf, 0.0, claim)
        self.assertTrue(sim._prefers_mode_zero(sim.nodes[0], claim))
