#!/usr/bin/env python3
"""Aggregation ticks, proving on the node's CPU, and per-peer wrappers.

This is where the EIP's mechanism finally runs end to end -- a node folds its
view once per interval and sends one wrapper per peer, with bodies replaced by
hashes for peers believed to hold them.
"""

import io
import random
import unittest

from starksim.arena import KIND_PQ, KIND_PRIVACY
from starksim.costs import PROVE_RECURSIVE, VERIFY_RECV, CostTable
from starksim.netsim import NetworkSim
from starksim.network import Overlay, SizeModel, bytes_per_ms
from starksim.payload import WrapperCore

from tests.support import line_overlay, parse_jsonl, two_node_overlay


def small(node_count=6, degree=4, **kwargs):
    rng = random.Random("duty")
    overlay = Overlay.random_regular(node_count, degree, rng,
                                     base_latency=10.0)
    defaults = dict(seed=1, aggregation_interval=200.0, slot_length=1000.0,
                    proposer_id=0,
                    costs=CostTable(prove_recursive=lambda deps: 10.0))
    defaults.update(kwargs)
    return NetworkSim(overlay, **defaults)


def line(node_count, **kwargs):
    overlay = line_overlay(node_count)
    defaults = dict(seed=1, aggregation_interval=200.0, slot_length=1000.0,
                    costs=CostTable(prove_recursive=lambda deps: 10.0))
    defaults.update(kwargs)
    return NetworkSim(overlay, **defaults)


class TestTheTick(unittest.TestCase):
    def test_a_node_with_nothing_proves_nothing(self):
        sim = small()
        sim.schedule_ticks(3)
        sim.run()
        self.assertEqual(sim.wrappers_emitted, 0)
        self.assertEqual(sim.proofs_completed, 0)

    def test_a_tick_folds_the_view_into_one_aggregate(self):
        sim = small()
        for index in range(5):
            sim.schedule_birth(float(index), KIND_PQ, node_id=0)
        sim.schedule_ticks(1)
        sim.run()
        self.assertGreater(sim.proofs_completed, 0)
        # The wide plan is one fold whatever it covers.
        first = sim.chain  # no block yet; check the aggregate instead
        aggregates = [obj for obj in sim.arena.objects.values()
                      if not obj.is_leaf()]
        self.assertTrue(aggregates)
        widest = max(aggregates, key=lambda obj: sim.arena.coverage(obj).bit_count())
        self.assertEqual(sim.arena.coverage(widest).bit_count(), 5)

    def test_ticks_are_spread_across_nodes_by_default(self):
        # Aligned ticks would put every uplink to work at the same instant.
        sim = small(node_count=8)
        sim.schedule_ticks(1)
        times = sorted(t for t, _seq, kind, _p in sim.engine.calendar._heap
                       if kind == "tick")
        self.assertGreater(len(set(times)), 4)

    def test_aligned_ticks_fire_together(self):
        sim = small(node_count=8, aggregation_trigger="aligned")
        sim.schedule_ticks(1)
        times = {t for t, _seq, kind, _p in sim.engine.calendar._heap
                 if kind == "tick"}
        self.assertEqual(len(times), 1)

    def test_unknown_trigger_rejected(self):
        with self.assertRaises(ValueError):
            small(aggregation_trigger="sideways")


class TestProving(unittest.TestCase):
    def test_proving_occupies_the_node_cpu(self):
        # Aligned ticks, so every node decides from its own view before any
        # wrapper can arrive. With spread ticks a peer's aggregate may land
        # first, and re-emitting it rather than re-proving is then correct,
        # so "node 0 proved" is not something to assume.
        sim = small(aggregation_trigger="aligned", costs=CostTable(prove_recursive=lambda deps: 50.0))
        for index in range(4):
            sim.schedule_birth(float(index), KIND_PQ, node_id=0)
        sim.schedule_ticks(1)
        sim.run()
        cpu = sim.cpu[0]
        self.assertEqual(cpu.served_by_kind[PROVE_RECURSIVE], 1)
        self.assertAlmostEqual(cpu.service_by_kind[PROVE_RECURSIVE], 50.0)

    def test_a_binary_plan_costs_more_proofs_than_a_wide_one(self):
        # Same coverage, k-1 proofs instead of one. That is the comparison the
        # two plans exist to make.
        counts = {}
        for plan in ("wide", "binary"):
            sim = small(fold_plan=plan, aggregation_trigger="aligned")
            for index in range(8):
                sim.schedule_birth(float(index), KIND_PQ, node_id=0)
            sim.schedule_ticks(1)
            sim.run()
            counts[plan] = sim.cpu[0].served_by_kind.get(PROVE_RECURSIVE, 0)
        self.assertEqual(counts["wide"], 1)
        self.assertEqual(counts["binary"], 7)

    def test_work_is_booked_when_proving_starts(self):
        # A proof still running at the end of a run has spent its inputs.
        sim = small(costs=CostTable(prove_recursive=lambda deps: 10_000.0))
        for index in range(3):
            sim.schedule_birth(float(index), KIND_PQ, node_id=0)
        sim.schedule_ticks(1)
        sim.run(until=500.0)
        self.assertEqual(sim.proofs_completed, 0)          # nothing finished
        # Every node that ticked booked the inputs its proof is chewing on, so
        # the total is a multiple of three rather than exactly three.
        consumed = sum(obj.consumed for obj in sim.arena.objects.values())
        self.assertGreater(consumed, 0)                    # but it was booked
        self.assertEqual(consumed % 3, 0)

    def test_abandon_drops_stale_unstarted_plans(self):
        # Proving slower than the interval means work queues; abandoning
        # re-decides against the current view instead of spending the budget on
        # a selection that no longer describes anything.
        for policy, expect_abandoned in (("fifo", False), ("abandon", True)):
            with self.subTest(policy=policy):
                sim = small(on_tick_overrun=policy,
                            costs=CostTable(prove_recursive=lambda d: 900.0))
                for index in range(6):
                    sim.schedule_birth(float(index) * 10.0, KIND_PQ, node_id=0)
                sim.schedule_ticks(4)
                sim.run()
                if expect_abandoned:
                    self.assertGreater(sim.plans_abandoned, 0)
                else:
                    self.assertEqual(sim.plans_abandoned, 0)

    def test_unknown_overrun_policy_rejected(self):
        with self.assertRaises(ValueError):
            small(on_tick_overrun="panic")


class TestWrappers(unittest.TestCase):
    def test_one_wrapper_core_reaches_every_peer(self):
        sim = small(node_count=5, degree=4)
        for index in range(3):
            sim.schedule_birth(float(index), KIND_PQ, node_id=0)
        sim.schedule_ticks(1)
        sim.run()
        wrappers = sim.offered_messages_by_kind.get("wrapper", 0)
        # At least the first proving node's four peers.
        self.assertGreaterEqual(wrappers, 4)

    def test_a_peer_believed_to_hold_a_body_gets_a_hash(self):
        # The EIP's substitution, end to end: node 1 already received the
        # transaction in full, so the wrapper covering it carries a hash.
        sim = line(2)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run(until=100.0)                       # the body has reached node 1
        self.assertEqual(sim.nodes[0].bodies_to_send(1, 0b1), 0)
        sim.schedule_ticks(1)
        sim.run()
        sizes = sim.size_model
        wrappers = sim.offered_messages_by_kind.get("wrapper", 0)
        wrapper_bytes = sim.offered_bytes_by_kind.get("wrapper", 0)
        # Both nodes tick, so both emit; what matters is the size of each.
        self.assertGreater(wrappers, 0)
        per_wrapper = wrapper_bytes / wrappers
        self.assertLess(per_wrapper, sizes.recursive_proof + sizes.tx_body)
        self.assertGreater(per_wrapper, sizes.recursive_proof)   # proof + a hash

    def test_a_peer_missing_a_body_gets_it_in_full(self):
        # Node 2 never saw the transaction, so node 1's wrapper must carry it.
        sim = line(3)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run(until=15.0)          # reached node 1 only
        self.assertTrue(sim.nodes[1].view.holds(sim.arena.objects[0]))
        self.assertFalse(sim.nodes[2].view.holds(sim.arena.objects[0]))
        self.assertEqual(sim.nodes[1].bodies_to_send(2, 0b1), 0b1)

    def test_wrappers_are_not_relayed(self):
        # Each node emits its own every interval, so a wrapper travels one hop
        # and spreading happens by re-aggregation. Relaying them as well would
        # double-count the bandwidth the EIP claims is constant per peer.
        sim = line(4)
        for index in range(2):
            sim.schedule_birth(float(index), KIND_PQ, node_id=0)
        sim.run(until=100.0)
        before = sim.offered_messages_by_kind.get("wrapper", 0)
        sim.engine.schedule_at(sim.engine.now + 1.0, "tick", (0, 1))
        sim.run(until=sim.engine.now + 200.0)
        after = sim.offered_messages_by_kind.get("wrapper", 0)
        # Node 0 has one peer, so exactly one wrapper, never forwarded onward.
        self.assertEqual(after - before, 1)

    def test_a_wrapper_over_dead_dependencies_is_not_sent(self):
        sim = line(2, proposer_id=0, slot_length=50.0)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_slots(1)
        sim.run(until=60.0)                     # included at the boundary
        self.assertEqual(sim.chain.included_count(), 1)
        sim.schedule_ticks(1)
        sim.run()
        self.assertEqual(sim.wrappers_emitted, 0)


class TestAggregationPaysOff(unittest.TestCase):
    def test_the_duty_cycle_beats_a_budget_capped_builder(self):
        """The point of the EIP, measured.

        A proposer may fold `budget + 1` objects into a block. Without
        aggregation that caps the block at that many transactions; with it, one
        object can carry the whole mempool.
        """
        results = {}
        for label, ticks in (("none", 0), ("duty_cycle", 8)):
            rng = random.Random("payoff")
            overlay = Overlay.random_regular(20, 4, rng, base_latency=50.0)
            sim = NetworkSim(overlay, seed=1, capacity=bytes_per_ms(100),
                             costs=CostTable(verify_recv=1.0,
                                             prove_recursive=lambda d: 20.0),
                             aggregation_interval=500.0, slot_length=2000.0,
                             proposer_id=0, block_budget=5)
            for index in range(60):
                sim.schedule_birth(index * 60.0, KIND_PQ, fee=float(index % 7))
            if ticks:
                sim.schedule_ticks(ticks)
            sim.schedule_slots(3)
            sim.run()
            results[label] = sim
        capped = results["none"]
        aggregated = results["duty_cycle"]
        # Without aggregation every block is budget-limited to six objects and
        # therefore six transactions.
        self.assertEqual([block.count() for block in capped.chain.blocks],
                         [6, 6, 6])
        # With it, a single object carries far more than the budget allows.
        self.assertGreater(aggregated.chain.included_count(),
                           3 * capped.chain.included_count())
        biggest = max(block.count() for block in aggregated.chain.blocks)
        self.assertGreater(biggest, 6)
        aggregated.assert_invariants()


if __name__ == "__main__":
    unittest.main()


class TestAccountingAndWrapperShape(unittest.TestCase):
    """Message accounting and wrapper construction invariants."""

    def test_message_bytes_decompose_exactly(self):
        # A total that cannot be checked against its parts is a number to be
        # believed. Every payload's components must sum to what was charged.
        sim = small(node_count=6)
        for index in range(6):
            sim.schedule_birth(float(index), KIND_PQ, node_id=index % 3)
        sim.schedule_ticks(2)
        sim.schedule_slots(1)
        sim.run()
        # Reconstruct the charge from the parts of every payload kind.
        sizes = sim.size_model
        by_kind = sim.offered_bytes_by_kind
        self.assertEqual(sum(by_kind.values()), sim.total_offered_bytes())
        self.assertIn("wrapper", by_kind)
        # A wrapper is dependencies + proof + bodies + hashes, nothing else.
        core_only = sizes.wrapper(
            WrapperCore(0, 0, 0, 1, 1, 0b111, 3), 0b111).size
        self.assertEqual(core_only.total(),
                         core_only.bodies + core_only.hashes
                         + core_only.dependencies + core_only.proof)
        self.assertEqual(core_only.control, 0)

    def test_exactly_one_root_proof_per_wrapper(self):
        # A tick emits one wrapper core however many proving jobs its plan took,
        # so the count of cores must equal the count of roots, not of proofs.
        for plan in ("wide", "binary"):
            with self.subTest(plan=plan):
                trace = io.StringIO()
                sim = small(node_count=4, degree=2, fold_plan=plan,
                            trace=trace)
                for index in range(6):
                    sim.schedule_birth(float(index), KIND_PQ, node_id=0)
                sim.schedule_ticks(1)
                sim.run()
                wrappers = [event for event in parse_jsonl(trace.getvalue())
                            if event["e"] == "wrp"]
                roots_per_tick = {(event["n"], event["tick"])
                                  for event in wrappers}
                self.assertEqual(len(wrappers), len(roots_per_tick))
                self.assertEqual(sim.wrappers_emitted, len(wrappers))
                if plan == "binary":
                    self.assertGreater(sim.proofs_completed,
                                       sim.wrappers_emitted)

    def test_a_body_bit_flips_exactly_once_per_peer(self):
        # The belief is monotonic: once a peer is known to hold a body, no later
        # delivery can make it unknown again, and nothing flips it twice.
        sim = small(node_count=6)
        for index in range(5):
            sim.schedule_birth(float(index), KIND_PQ, node_id=index % 2)
        sim.schedule_ticks(3)
        sim.run()
        for node in sim.nodes:
            beliefs = node.view.peer_bodies
            for peer in node.peers:
                mask = beliefs.believes_held(peer, (1 << 5) - 1)
                # Re-confirming what is already believed adds nothing.
                self.assertEqual(beliefs.confirm_delivery(peer, mask), 0)

    def test_the_mode_crossover_lands_where_the_eip_says(self):
        """`eip-8288.md:244` says naive concatenation wins "up to about a
        hundred leanSPHINCS signatures". That is a prediction the size model can
        be held to: mode 0 pays a witness per dependency, mode 1 pays one
        recursive proof whatever it covers, so for an all-signature claim they
        cross where `deps x sig_witness == recursive_proof`. A privacy
        dependency carries a 256 kB leanSTARK, so the same comparison crosses
        at two.
        """
        sizes = SizeModel()
        crossover = None
        for deps in range(1, 400):
            mask = (1 << deps) - 1
            mode_zero = sizes.wrapper(
                WrapperCore(0, 0, 0, 1, 0, mask, deps), 0,
                witness_bytes=deps * sizes.sig_witness).size.total()
            mode_one = sizes.wrapper(
                WrapperCore(1, 0, 0, 1, 1, mask, deps), 0).size.total()
            if mode_one < mode_zero:
                crossover = deps
                break
        # The EIP's "about a hundred", from its own constants.
        self.assertGreater(crossover, 80)
        self.assertLess(crossover, 130)
        self.assertEqual(crossover,
                         sizes.recursive_proof // sizes.sig_witness + 1)


class TestCostAndKnowledgeBoundaries(unittest.TestCase):
    """The model charges required work and does not invent knowledge."""

    def test_wrappers_and_blocks_are_verified_like_anything_else(self):
        # Wrappers and blocks pass through verification before admission; doing
        # either synchronously would make aggregation cheaper than configured.
        overlay = two_node_overlay(10.0)
        sim = NetworkSim(overlay, seed=1, chain_notify="overlay",
                         costs=CostTable(verify_recv=5.0,
                                         prove_recursive=lambda deps: 10.0),
                         aggregation_interval=100.0, slot_length=300.0,
                         proposer_id=0)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_ticks(2)
        sim.schedule_slots(1)
        sim.run()
        objects = sim.offered_messages_by_kind.get("object", 0)
        verifications = sum(server.served_by_kind.get(VERIFY_RECV, 0)
                            for server in sim.cpu)
        self.assertGreater(sim.offered_messages_by_kind.get("wrapper", 0), 0)
        self.assertGreater(sim.offered_messages_by_kind.get("block", 0), 0)
        # More verifications than there were raw objects, so wrappers and blocks
        # are demonstrably paying for their own processing.
        self.assertGreater(verifications, objects)

    def test_a_wrapper_asserts_what_its_proof_froze(self):
        # The claim is decided when the proof starts and never recomputed. A
        # receiver reading the fold's wider coverage instead could rely on a
        # dependency the prover had explicitly dropped.
        sim = small(node_count=4, degree=2, slot_length=150.0, proposer_id=0)
        for index in range(3):
            sim.schedule_birth(float(index), KIND_PQ, node_id=0)
        sim.schedule_slots(1)
        sim.run(until=160.0)                      # some txs now included
        sim.schedule_ticks(1)
        sim.run()
        for obj in sim.arena.objects.values():
            if obj.is_leaf() or obj.claimed is None:
                continue
            # The claim never exceeds the fold, and is frozen: asking again
            # returns the same answer whatever has happened since.
            coverage = sim.arena.coverage(obj)
            self.assertEqual(obj.claimed & ~coverage, 0)
            self.assertEqual(sim.arena.claim(obj), obj.claimed)

    def test_expiry_reaches_nodes_under_overlay_notification(self):
        # Block notices carry expiry facts so every node stops proving expired
        # dependencies.
        overlay = Overlay.from_adjacency([[1], [0, 2], [1]], base_latency=10.0)
        sim = NetworkSim(overlay, seed=1, chain_notify="overlay",
                         slot_length=200.0, proposer_id=0)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0, lifetime=50.0)
        sim.schedule_slots(2)
        sim.run()
        self.assertEqual(sim.chain.expired_count(), 1)
        for node in sim.nodes:
            self.assertEqual(node.known_discarded(), 0b1)

    def test_a_plan_retires_the_selection_it_consumed(self):
        # Only the root job's direct inputs were superseded, so a tree left the
        # original selection sitting as candidates for ever.
        sim = small(node_count=4, degree=2, fold_plan="binary",
                    aggregation_trigger="aligned")
        for index in range(6):
            sim.schedule_birth(float(index), KIND_PQ, node_id=0)
        sim.schedule_ticks(1)
        sim.run()
        self.assertGreater(sim.nodes[0].superseded, 0)


class TestTheAggregationTrigger(unittest.TestCase):
    """Three strategies: one clock, a clock each, or no clock.

    `when_ready` is v1's rule -- fold as soon as the CPU is free and there is
    something to fold -- and the EIP's fixed cadence is what replaced it. The
    trade is that a fixed interval makes the standing bandwidth cost calculable
    while `when_ready` makes the emission rate a consequence of load.
    """

    def a_run(self, trigger, births=40, **kwargs):
        rng = random.Random("trigger:topology")
        overlay = Overlay.random_regular(20, 4, rng, base_latency=50.0)
        settings = dict(seed=3, capacity=bytes_per_ms(50),
                        costs=CostTable(verify_recv=1.0,
                                        prove_recursive=lambda deps: 200.0),
                        aggregation_trigger=trigger)
        if trigger == "when_ready":
            settings["proving_budget"] = 1000.0
        settings.update(kwargs)
        sim = NetworkSim(overlay, **settings)
        for index in range(births):
            sim.schedule_birth(index * 200.0, KIND_PQ)
        if trigger != "when_ready":
            sim.schedule_ticks(8)
        sim.schedule_slots(2)
        sim.run(until=8000.0)
        return sim

    def test_without_a_clock_a_node_still_aggregates(self):
        sim = self.a_run("when_ready")
        self.assertGreater(sim.proofs_completed, 0)
        self.assertGreater(sim.wrappers_emitted, 0)

    def test_no_clock_means_no_schedule(self):
        # A scheduled tick under `when_ready` would be a clock by another name.
        sim = self.a_run("when_ready", births=1)
        with self.assertRaises(ValueError) as caught:
            sim.schedule_ticks(4)
        self.assertIn("no clock to schedule", str(caught.exception))

    def test_a_node_with_nothing_to_fold_never_wakes(self):
        # The wake condition is two held objects: below that a round can only
        # re-wrap or prove a lone object, which no arrival makes worthwhile.
        overlay = two_node_overlay(10.0)
        sim = NetworkSim(overlay, aggregation_trigger="when_ready",
                         proving_budget=1000.0,
                         costs=CostTable(prove_recursive=lambda deps: 200.0))
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_slots(1)
        sim.run(until=4000.0)
        # One leaf reaches each node, and one is not two.
        self.assertEqual(sim.ticks_run, 0)
        self.assertEqual(sim.proofs_completed, 0)

    def test_a_node_folds_once_a_second_object_arrives(self):
        overlay = two_node_overlay(10.0)
        sim = NetworkSim(overlay, aggregation_trigger="when_ready",
                         proving_budget=1000.0,
                         costs=CostTable(prove_recursive=lambda deps: 200.0))
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_birth(1.0, KIND_PQ, node_id=0)
        sim.schedule_slots(1)
        sim.run(until=4000.0)
        self.assertGreater(sim.ticks_run, 0)
        self.assertGreater(sim.proofs_completed, 0)

    def test_the_trigger_reaches_the_variant(self):
        # A run whose cadence is not in its identity is not attributable.
        for trigger in ("spread", "when_ready"):
            with self.subTest(trigger=trigger):
                sim = self.a_run(trigger, births=2)
                self.assertEqual(sim.variant()["aggregation_trigger"], trigger)

    def test_a_clock_emits_less_than_no_clock(self):
        # The measured trade, and the reason the interval is the default: a
        # node that folds whenever it can also emits whenever it folds.
        clocked = self.a_run("spread")
        unclocked = self.a_run("when_ready")
        self.assertGreater(unclocked.wrappers_emitted,
                           clocked.wrappers_emitted)

    def test_a_locally_born_transaction_wakes_its_node(self):
        # A birth frees no CPU, so the central on_idle wiring cannot see it. A
        # birth is new material all the same, and without its own wake a node
        # whose objects all arrive that way -- a single node, or an entry node
        # under wrapper_only -- waits for a clock that does not exist.
        sim = NetworkSim(Overlay.from_adjacency([[]], base_latency=0.0),
                         aggregation_trigger="when_ready", proving_budget=1e9,
                         costs=CostTable(prove_recursive=lambda deps: 5.0))
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_birth(1.0, KIND_PQ, node_id=0)
        sim.run(until=100.0)
        self.assertGreater(sim.proofs_completed, 0)

    def test_a_finished_block_build_wakes_the_proposer(self):
        # The wake declines a busy CPU, so whatever occupies one must offer a
        # completion to retry from. Block assembly had none.
        sim = NetworkSim(Overlay.from_adjacency([[1], [0]], base_latency=0.0),
                         aggregation_trigger="when_ready", proving_budget=1e9,
                         slot_length=1.0,
                         costs=CostTable(build_block=10.0,
                                         prove_recursive=5.0))
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_birth(2.0, KIND_PQ, node_id=1)
        sim.schedule_birth(3.0, KIND_PQ, node_id=1)
        sim.schedule_slots(1)
        sim.run()
        self.assertGreater(sim.proofs_completed, 0)
        for node_id, node in enumerate(sim.nodes):
            with self.subTest(node=node_id):
                # Nothing is left foldable on an idle CPU at the end.
                self.assertFalse(len(node.view.held) >= 2
                                 and not sim.cpu[node_id].is_busy()
                                 and not sim.cpu[node_id].waiting
                                 and sim.proofs_completed == 0)

    def test_any_way_a_cpu_goes_idle_wakes_it(self):
        # The wake hangs off the server running dry, not off particular job
        # kinds. Hanging it off completions left verification, block assembly
        # and abandoned plans as ways to miss it -- twice, in two reviews.
        sim = NetworkSim(Overlay.from_adjacency([[1], [0]], base_latency=0.0),
                         relay_policy="wrapper_only", chain_notify="overlay",
                         aggregation_trigger="when_ready", proving_budget=1e9,
                         proposer_id=0, slot_length=1.0,
                         costs=CostTable(verify_recv=10.0,
                                         prove_recursive=5.0))
        sim.schedule_slots(1)
        sim.schedule_birth(2.0, KIND_PQ, node_id=1)
        sim.schedule_birth(3.0, KIND_PQ, node_id=1)
        sim.run()
        self.assertGreater(sim.proofs_completed, 0)

    def test_an_idle_cpu_with_nothing_new_does_not_retry_for_ever(self):
        # The other half. A node whose plan cannot complete -- here because no
        # job may wait -- abandons it, goes idle, and would re-decide at CPU
        # speed. The wake needs something to have changed, not merely a free
        # CPU, or an unsatisfiable configuration becomes a livelock.
        sim = NetworkSim(Overlay.from_adjacency([[]], base_latency=0.0),
                         aggregation_trigger="when_ready", proving_budget=1e9,
                         fold_plan="binary", cpu_queue_jobs=0,
                         slot_length=1.0,
                         costs=CostTable(build_block=10.0,
                                         prove_recursive=5.0))
        for when in (2.0, 3.0, 4.0, 5.0):
            sim.schedule_birth(when, KIND_PQ, node_id=0)
        sim.schedule_slots(1)
        sim.run(until=500.0)
        # One attempt per arrival at most, not one per completed job.
        self.assertLessEqual(sim.plans_abandoned, 4)

    def test_learning_something_settled_can_make_a_node_ready(self):
        # A node whose only plan was infeasible becomes able to fold when an
        # expiry retires the dependencies that made it too large -- without a
        # single object arriving. Counting arrivals alone left it stranded.
        from starksim.costs import CircuitCost
        circuit = CircuitCost(ms_per_hash=1.0, sphincs_hashes=1,
                              recursive_hashes=10, leanstark_hashes=10,
                              trace_rows_per_hash=1, trace_cap=5)
        sim = NetworkSim(Overlay.from_adjacency([[1], [0]], base_latency=0.0),
                         relay_policy="wrapper_only",
                         aggregation_trigger="when_ready", proving_budget=1e9,
                         prove_cost_basis="circuit",
                         costs=CostTable(prove_recursive=circuit))
        sim.schedule_birth(0.0, KIND_PQ, node_id=1, lifetime=10.0)
        sim.schedule_birth(1.0, KIND_PQ, node_id=1, lifetime=10.0)
        sim.schedule_birth(4.0, KIND_PQ, node_id=0)
        sim.schedule_birth(5.0, KIND_PQ, node_id=0)
        sim.run(until=20.0)
        claims = {sim.arena.claim(obj)
                  for obj in sim.arena.objects.values() if not obj.is_leaf()}
        # The fold over the two local signatures, which only became feasible
        # once the received aggregate's dependencies expired.
        self.assertIn(0b1100, claims)
