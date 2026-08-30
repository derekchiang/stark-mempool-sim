#!/usr/bin/env python3
"""Cost tables, per-node CPU queues, and proposer block building."""

import random
import unittest

from starksim.arena import KIND_PQ, KIND_PRIVACY
from starksim.costs import (BUILD_BLOCK, PROVE_RECURSIVE, VERIFY_RECV,
                            CircuitCost, CostTable)
from starksim.netsim import NetworkSim, csv_row
from starksim.network import Overlay, SizeModel, bytes_per_ms

from tests.support import make_sim, two_node_overlay


def two_nodes(latency=10.0, capacity=None, costs=None, **kwargs):
    return make_sim(base_latency=latency, capacity=capacity, costs=costs,
                    **kwargs)


class TestCostTable(unittest.TestCase):
    def test_constant_costs(self):
        costs = CostTable(verify_recv=3.0, prove_recursive=1500.0)
        self.assertEqual(costs.duration(VERIFY_RECV), 3.0)
        self.assertEqual(costs.duration(PROVE_RECURSIVE), 1500.0)
        self.assertEqual(costs.duration(BUILD_BLOCK), 0.0)

    def test_cost_may_depend_on_dependency_count(self):
        # Proving over 500 dependencies is not proving over 2, and the EIP's
        # prioritisation only makes sense if cost grows with size.
        costs = CostTable(prove_recursive=lambda deps: 100.0 + 2.0 * deps)
        self.assertEqual(costs.duration(PROVE_RECURSIVE, 1), 102.0)
        self.assertEqual(costs.duration(PROVE_RECURSIVE, 200), 500.0)

    def test_jitter_stays_within_bounds(self):
        costs = CostTable(verify_recv=10.0, jitter_frac=0.5, seed=1)
        draws = [costs.duration(VERIFY_RECV) for _ in range(200)]
        self.assertGreaterEqual(min(draws), 10.0)
        self.assertLessEqual(max(draws), 15.0)
        self.assertGreater(len(set(draws)), 100)     # actually varying

    def test_zero_cost_is_free_however_it_is_expressed(self):
        # Freeness is a property of the computed duration, not of how the value
        # was written. Inspecting the stored value instead meant a cost function
        # returning zero still queued a zero-duration job behind whatever the
        # CPU was doing, so a nominally free verification finished late.
        costs = CostTable()
        self.assertTrue(costs.is_free(VERIFY_RECV))
        self.assertFalse(CostTable(verify_recv=1.0).is_free(VERIFY_RECV))
        self.assertTrue(CostTable(verify_recv=lambda d: 0.0).is_free(VERIFY_RECV))
        # ...and a function that is free only for some sizes is free only there.
        sized = CostTable(verify_recv=lambda deps: 0.0 if deps < 2 else 5.0)
        self.assertTrue(sized.is_free(VERIFY_RECV, 1))
        self.assertFalse(sized.is_free(VERIFY_RECV, 2))

    def test_a_free_cost_function_does_not_queue(self):
        overlay = two_node_overlay(0.0)
        sim = NetworkSim(overlay, seed=1,
                         costs=CostTable(verify_recv=lambda deps: 0.0))
        from starksim.engine import Job
        sim.cpu[1].submit(Job(100.0))          # the CPU is already busy
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        self.assertEqual(sim.admission_times(sim.arena.objects[0])[1], 0.0)

    def test_negative_costs_rejected(self):
        with self.assertRaises(ValueError):
            CostTable(verify_recv=-1.0)
        with self.assertRaises(ValueError):
            CostTable(jitter_frac=-0.1)


class TestVerificationOnTheRelayPath(unittest.TestCase):
    def test_processing_delays_admission_by_exactly_its_cost(self):
        costs = CostTable(verify_recv=7.0)
        sim = two_nodes(latency=10.0, costs=costs)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = sim.arena.objects[0]
        # one hop, then processing before the node counts as holding it
        self.assertAlmostEqual(sim.admission_times(obj)[1], 17.0, places=9)

    def test_processing_sits_on_the_relay_path(self):
        # A line of four: each hop pays latency plus processing, because a node
        # cannot forward what it has not finished handling.
        adjacency = [[1], [0, 2], [1, 3], [2]]
        overlay = Overlay.from_adjacency(adjacency, base_latency=10.0)
        sim = NetworkSim(overlay, seed=1, costs=CostTable(verify_recv=5.0))
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = sim.arena.objects[0]
        self.assertEqual(sim.admission_times(obj),
                         [0.0, 15.0, 30.0, 45.0])

    def test_free_verification_reproduces_the_latency_only_model(self):
        # Phases 1 and 2 must stay exactly reachable, as a baseline.
        adjacency = [[1], [0, 2], [1, 3], [2]]
        overlay = Overlay.from_adjacency(adjacency, base_latency=10.0)
        sim = NetworkSim(overlay, seed=1, costs=CostTable())
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        self.assertEqual(sim.admission_times(sim.arena.objects[0]),
                         [0.0, 10.0, 20.0, 30.0])
        self.assertEqual(sim.max_cpu_depth(), 0)

    def test_cpu_utilisation_matches_offered_work(self):
        # One message every 20 units, each costing 5 to process: 25% busy.
        overlay = two_node_overlay(1.0)
        sim = NetworkSim(overlay, seed=1, costs=CostTable(verify_recv=5.0))
        for index in range(200):
            sim.schedule_birth(index * 20.0, KIND_PQ, node_id=0)
        horizon = 4000.0
        sim.run(until=horizon)
        busy = sim.cpu[1].busy_time_at(horizon)
        self.assertLess(abs(busy / horizon - 0.25), 0.02)

    def test_saturated_cpu_queues_and_delays_admission(self):
        # Processing costs more than the arrival interval, so the queue grows
        # and admission falls further and further behind arrival.
        overlay = two_node_overlay(1.0)
        sim = NetworkSim(overlay, seed=1, costs=CostTable(verify_recv=20.0))
        for index in range(60):
            sim.schedule_birth(index * 10.0, KIND_PQ, node_id=0)
        sim.run()
        lags = []
        for obj in sim.arena.objects.values():
            admitted = sim.nodes[1].view.admitted_at(obj)
            lags.append(admitted - obj.born)
        self.assertGreater(sim.max_cpu_depth(), 10)
        self.assertGreater(lags[-1], lags[0] * 5)

    def test_verification_and_block_building_share_one_cpu(self):
        # The single server is the point: assembling a block delays processing
        # of everything that arrives while it runs.
        overlay = two_node_overlay(1.0)
        sim = NetworkSim(overlay, seed=1,
                         costs=CostTable(verify_recv=1.0, build_block=500.0),
                         slot_length=100.0, proposer_id=1)
        for index in range(20):
            sim.schedule_birth(index * 10.0, KIND_PQ, node_id=0)
        sim.schedule_slots(3)
        sim.run()
        # The proposer's CPU carries both kinds of work.
        self.assertGreater(sim.cpu[1].busy_time, 500.0)
        self.assertGreater(sim.cpu[1].served, 20)


class TestServiceTimesPerKind(unittest.TestCase):
    """Observed service time must decompose by the configured work classes."""

    def test_busy_time_decomposes_by_computation_kind(self):
        overlay = two_node_overlay(1.0)
        costs = CostTable(verify_recv=7.0, build_block=13.0)
        sim = NetworkSim(overlay, seed=1, costs=costs, slot_length=200.0,
                         proposer_id=1)
        for index in range(10):
            sim.schedule_birth(index * 50.0, KIND_PQ, node_id=0)
        sim.schedule_slots(3)
        sim.run()
        cpu = sim.cpu[1]
        # Ten messages verified at 7, three blocks assembled at 13.
        self.assertEqual(cpu.served_by_kind[VERIFY_RECV], 10)
        self.assertEqual(cpu.served_by_kind[BUILD_BLOCK], 3)
        self.assertAlmostEqual(cpu.service_by_kind[VERIFY_RECV], 70.0)
        self.assertAlmostEqual(cpu.service_by_kind[BUILD_BLOCK], 39.0)
        self.assertAlmostEqual(cpu.busy_time, 109.0)
        self.assertAlmostEqual(sum(cpu.service_by_kind.values()), cpu.busy_time)

    def test_an_empty_block_costs_nothing_for_a_sized_cost(self):
        # A cost function must be asked about zero transactions, not about one:
        # inventing a dependency charges for a block that included nothing.
        seen = []
        overlay = two_node_overlay(1.0)
        costs = CostTable(build_block=lambda count: seen.append(count) or 0.0)
        sim = NetworkSim(overlay, seed=1, costs=costs, slot_length=100.0,
                         proposer_id=0)
        # Construction samples the cost function for the run's variant identity,
        # which is not what this test is about.
        seen.clear()
        sim.schedule_slots(2)
        sim.run()
        self.assertEqual(seen, [0, 0])


class TestWorkBooking(unittest.TestCase):
    def test_consumption_is_booked_when_a_job_starts(self):
        # The promised semantics need a hook at service start: a job cancelled
        # or still running at the end of a run has spent its inputs even though
        # no aggregate exists.
        from starksim.engine import Engine, Job
        engine = Engine()
        server = engine.new_server("cpu")
        booked = []
        blocker = Job(100.0)
        server.submit(blocker)
        server.submit(Job(10.0, on_start=lambda job: booked.append(engine.now)))
        engine.run(until=50.0)
        self.assertEqual(booked, [])              # queued, not started
        engine.run(until=100.0)
        self.assertEqual(booked, [100.0])         # booked at start, not at end

    def test_cancelled_work_is_never_booked(self):
        from starksim.engine import Engine, Job
        engine = Engine()
        server = engine.new_server("cpu")
        booked = []
        server.submit(Job(100.0))
        server.submit(Job(10.0, on_start=lambda job: booked.append(1),
                          generation=1))
        server.cancel_waiting(lambda job: job.generation == 1)
        engine.run()
        self.assertEqual(booked, [])


class TestBlockBuilding(unittest.TestCase):
    def _sim(self, node_count=12, degree=4, **kwargs):
        rng = random.Random("blocks")
        overlay = Overlay.random_regular(node_count, degree, rng,
                                         base_latency=20.0)
        return NetworkSim(overlay, seed=1, **kwargs)

    def test_budget_caps_objects_per_block(self):
        # B folds buys B+1 objects: a binary tree over k leaves costs k-1 folds.
        sim = self._sim(block_budget=3, slot_length=1000.0)
        for index in range(20):
            sim.schedule_birth(10.0 + index, KIND_PQ, node_id=0)
        sim.schedule_slots(4)
        sim.run()
        for block in sim.chain.blocks:
            self.assertLessEqual(len(block.object_ids), 4)
        self.assertEqual(sim.chain.included_count(), 16)   # 4 blocks x 4

    def test_a_transaction_is_included_once(self):
        sim = self._sim(slot_length=1000.0)
        for index in range(4):
            sim.schedule_birth(10.0 + index, KIND_PQ, node_id=0)
        sim.schedule_slots(3)
        sim.run()
        self.assertEqual(sim.chain.included_count(), 4)
        self.assertEqual([block.count() for block in sim.chain.blocks],
                         [4, 0, 0])

    def test_the_proposer_builds_from_its_own_view(self):
        # A transaction that has not reached the proposer cannot be included,
        # however long it has existed.
        adjacency = [[1], [0, 2], [1]]
        overlay = Overlay.from_adjacency(adjacency, base_latency=1000.0)
        sim = NetworkSim(overlay, seed=1, slot_length=1500.0, proposer_id=0)
        sim.schedule_birth(0.0, KIND_PQ, node_id=2)     # two hops from proposer
        sim.schedule_slots(1)
        sim.run(until=1500.0)
        self.assertFalse(sim.nodes[0].view.holds(sim.arena.objects[0]))
        self.assertEqual(sim.chain.blocks[0].count(), 0)
        sim.run()                                        # let it arrive
        self.assertTrue(sim.nodes[0].view.holds(sim.arena.objects[0]))

    def test_block_records_what_it_needs_to_be_reconstructed(self):
        sim = self._sim(slot_length=1000.0, proposer_id=3)
        sim.schedule_birth(10.0, KIND_PQ, node_id=3)
        sim.schedule_slots(1)
        sim.run()
        block = sim.chain.blocks[0]
        self.assertEqual(block.slot, 1)
        self.assertEqual(block.proposer, 3)
        self.assertEqual(block.time, 1000.0)
        self.assertEqual(block.count(), 1)
        self.assertEqual(len(block.object_ids), 1)
        self.assertAlmostEqual(block.delays[0], (1000.0 - 10.0) / 1000.0)

    def test_rotating_proposer_spreads_blocks(self):
        sim = self._sim(node_count=4, degree=2, slot_length=500.0,
                        proposer_mode="rotating")
        for index in range(20):
            sim.schedule_birth(index * 100.0, KIND_PQ)
        sim.schedule_slots(8)
        sim.run()
        proposers = [block.proposer for block in sim.chain.blocks]
        self.assertEqual(proposers, [1, 2, 3, 0, 1, 2, 3, 0])

    def test_delays_are_measured_in_slots(self):
        sim = self._sim(slot_length=1000.0, proposer_id=0)
        sim.schedule_birth(250.0, KIND_PQ, node_id=0)
        sim.schedule_slots(1)
        sim.run()
        self.assertAlmostEqual(sim.delay_mean(), 0.75)


class TestLowLoadThresholds(unittest.TestCase):
    """Low load waits roughly one slot including propagation and processing."""

    def test_low_load_mean_delay_is_about_one_slot(self):
        rng = random.Random("lowload")
        overlay = Overlay.random_regular(50, 6, rng, base_latency=100.0,
                                         jitter_frac=0.1, latency_seed=1)
        costs = CostTable(verify_recv=2.0, build_block=5.0, seed=1)
        sim = NetworkSim(overlay, seed=1, capacity=bytes_per_ms(10),
                         costs=costs, slot_length=4000.0, block_budget=5)
        slots = 100
        # Well inside the budget of 6 objects per block, so nothing queues.
        for index in range(slots):
            sim.schedule_birth(index * 4000.0 + 500.0, KIND_PQ)
        sim.schedule_slots(slots)
        sim.run()
        limit = 1.0 + costs.base(PROVE_RECURSIVE) / 4000.0 + 0.25
        self.assertLessEqual(sim.delay_mean(), limit)
        self.assertGreater(sim.chain.included_count(), slots - 3)

    def test_same_seed_gives_a_byte_identical_csv_row(self):
        def one_run(seed):
            rng = random.Random(f"{seed}:topology")
            overlay = Overlay.random_regular(20, 4, rng, base_latency=50.0,
                                             jitter_frac=0.1, latency_seed=seed)
            sim = NetworkSim(overlay, seed=seed, capacity=bytes_per_ms(10),
                             costs=CostTable(verify_recv=2.0, jitter_frac=0.1,
                                             seed=seed))
            for index in range(30):
                sim.schedule_birth(index * 500.0, KIND_PQ)
            sim.schedule_slots(6)
            sim.run()
            return csv_row(sim.metrics())

        self.assertEqual(one_run(7), one_run(7))
        self.assertNotEqual(one_run(7), one_run(8))


class TestVariantIdentity(unittest.TestCase):
    def test_the_metrics_say_which_variant_produced_them(self):
        # Two runs differing only in relay policy must be distinguishable from
        # their output alone.
        rng = random.Random("variant")
        overlay = Overlay.random_regular(10, 4, rng, base_latency=10.0)
        rows = {}
        for policy in ("flood", "announce_fetch"):
            sim = NetworkSim(overlay, seed=1, relay_policy=policy)
            sim.schedule_birth(0.0, KIND_PQ, node_id=0)
            sim.schedule_slots(1)
            sim.run()
            rows[policy] = sim.metrics()
        self.assertEqual(rows["flood"]["relay_policy"], "flood")
        self.assertEqual(rows["announce_fetch"]["relay_policy"],
                         "announce_fetch")
        self.assertNotEqual(csv_row(rows["flood"]),
                            csv_row(rows["announce_fetch"]))

    def test_unknown_prove_cost_basis_is_refused(self):
        overlay = two_node_overlay(0.0)
        with self.assertRaises(ValueError):
            NetworkSim(overlay, prove_cost_basis="folds")

    def _fold_busy_time(self, basis):
        """Total CPU time spent folding: two leaves, then aggregate + leaf.

        One isolated node, aligned ticks every 1000. The first fold takes two
        leaves (2 inputs, 2 deps -- the bases agree). The second folds the
        node's own aggregate with one fresh leaf: 2 inputs but 3 asserted
        dependencies, which is exactly where the bases diverge. With cost
        100 * size, total fold time is 200 + 200 under `inputs` and 200 + 300
        under `deps`.
        """
        overlay = Overlay.from_adjacency([[]], base_latency=0.0)
        sim = NetworkSim(overlay, seed=1, aggregation_trigger="aligned",
                         costs=CostTable(
                             prove_recursive=lambda size: 100.0 * size),
                         prove_cost_basis=basis)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_birth(10.0, KIND_PQ, node_id=0)
        sim.schedule_birth(1500.0, KIND_PQ, node_id=0)
        sim.schedule_ticks(2)
        sim.run(until=3000.0)
        return sim.cpu[0].busy_time_at(3000.0)

    def test_deps_basis_prices_a_fold_by_its_asserted_claim(self):
        self.assertEqual(self._fold_busy_time("deps"), 500.0)

    def test_inputs_basis_prices_a_fold_by_what_it_folds(self):
        self.assertEqual(self._fold_busy_time("inputs"), 400.0)

    def test_variant_covers_every_switchable_choice(self):
        sim = two_nodes()
        variant = sim.variant()
        for key in ("nodes", "degree", "latency_base", "latency_jitter",
                    "capacity", "link_capacity", "relay_policy",
                    "chain_notify", "proposer_mode", "slot_length",
                    "block_budget", "cost_verify_recv", "seed"):
            self.assertIn(key, variant)


if __name__ == "__main__":
    unittest.main()


class TestCircuitCost(unittest.TestCase):
    """Proving priced by what the circuit verifies, not by input count.

    EIP-8288's recursive circuit runs three different verifiers over a flat
    input list, and on the project's estimates one nested proof is worth about
    twenty signatures. A cost that cannot tell them apart bills a signature at a
    nested proof's price."""

    def test_the_anchor_reproduces_a_measured_pairwise_fold(self):
        cost = CircuitCost.anchored_on_pairwise_fold(889.0)
        self.assertAlmostEqual(cost.duration(0, 0, 2), 889.0, places=6)

    def test_a_signature_is_far_cheaper_than_a_nested_proof(self):
        cost = CircuitCost.anchored_on_pairwise_fold(889.0)
        ratio = cost.duration(0, 0, 1) / cost.duration(1, 0, 0)
        self.assertAlmostEqual(ratio, cost.recursive_hashes / cost.sphincs_hashes,
                               places=6)
        self.assertGreater(ratio, 10.0)

    def test_the_trace_cap_bounds_one_proof(self):
        cost = CircuitCost.anchored_on_pairwise_fold(889.0)
        self.assertTrue(cost.fits(100, 0, 0))
        self.assertFalse(cost.fits(100_000, 0, 0))

    def test_used_as_a_bare_callable_it_assumes_nested_proofs(self):
        # The conservative reading, for a caller with only a count to hand.
        cost = CircuitCost.anchored_on_pairwise_fold(889.0)
        self.assertEqual(cost(3), cost.duration(0, 0, 3))

    def test_a_binding_budget_does_not_narrow_a_signature_claim(self):
        # The estimate must be in the unit execution charges. Read as nested
        # proofs, three signatures estimate at ~1 333 ms against an actual
        # 62 ms, so the default one-interval budget refused the third -- a
        # budget ceiling reported as a proving cost. This is the fault that
        # produced one withdrawn result under `prove_cost_basis="inputs"`.
        cost = CircuitCost.anchored_on_pairwise_fold(889.0)
        self.assertGreater(cost(3), 1000.0)
        self.assertLess(cost.duration(3, 0, 0), 1000.0)

        widths = {}
        for budget in (1000.0, 1e9):
            sim = NetworkSim(Overlay.from_adjacency([[]], base_latency=0.0),
                             prove_cost_basis="circuit", aggregation_trigger="aligned",
                             proving_budget=budget,
                             costs=CostTable(prove_recursive=cost,
                                             verify_recv=0.0))
            for _ in range(3):
                sim.schedule_birth(0.0, KIND_PQ, node_id=0)
            sim.schedule_ticks(1)
            sim.run(until=5000.0)
            claims = [obj.claimed.bit_count() for obj in sim.arena.objects.values()
                      if not obj.is_leaf()]
            widths[budget] = claims
            # And what it charged is what it estimated: one wide fold over
            # three signatures, not three nested verifications.
            self.assertAlmostEqual(
                sim.cpu[0].service_by_kind[PROVE_RECURSIVE],
                cost.duration(3, 0, 0), places=6)
        self.assertEqual(widths[1000.0], [3])
        self.assertEqual(widths[1000.0], widths[1e9])

    def test_the_basis_needs_a_circuit_cost(self):
        overlay = two_node_overlay(0.0)
        with self.assertRaises(ValueError):
            NetworkSim(overlay, prove_cost_basis="circuit",
                       costs=CostTable(prove_recursive=100.0))

    def test_signatures_and_aggregates_are_charged_differently(self):
        # Two PQ leaves folded together cost two signature verifications; the
        # same node folding its own aggregate with a leaf pays a nested proof.
        cost = CircuitCost.anchored_on_pairwise_fold(889.0)
        durations = []
        for kind in (KIND_PQ, KIND_PRIVACY):
            overlay = two_node_overlay(0.0)
            sim = NetworkSim(overlay, seed=1, aggregation_trigger="aligned",
                             prove_cost_basis="circuit",
                             costs=CostTable(prove_recursive=cost))
            sim.schedule_birth(0.0, kind, node_id=0)
            sim.schedule_birth(1.0, kind, node_id=0)
            sim.schedule_ticks(1)
            sim.run(until=20000.0)
            durations.append(
                sim.cpu[0].service_by_kind.get(PROVE_RECURSIVE, 0.0))
        signatures, leanstarks = durations
        self.assertAlmostEqual(signatures, cost.duration(2, 0, 0), places=6)
        self.assertAlmostEqual(leanstarks, cost.duration(0, 2, 0), places=6)
        self.assertGreater(leanstarks, signatures * 10)


class TestCircuitIdentity(unittest.TestCase):
    """A circuit cost must be identifiable from a result row.

    The scalar probes in the variant sample the prover at one and a hundred
    nested proofs, which two very different circuits can agree on. The weights
    and the trace cap decide whether a fold is possible at all, so a row that
    omits them cannot attribute its own outcome.
    """

    def a_sim(self, **kwargs):
        table = CostTable(
            prove_recursive=CircuitCost.anchored_on_pairwise_fold(889.0,
                                                                 **kwargs))
        return NetworkSim(two_node_overlay(0.0), prove_cost_basis="circuit",
                          costs=table)

    def test_every_circuit_parameter_reaches_the_variant(self):
        baseline = self.a_sim()
        for parameter, value in (("sphincs_hashes", 100_000),
                                 ("leanstark_hashes", 1.0),
                                 ("recursive_hashes", 5_000),
                                 ("overhead_hashes", 1e6),
                                 ("trace_rows_per_hash", 300),
                                 ("trace_cap", 1)):
            other = self.a_sim(**{parameter: value})
            self.assertNotEqual(baseline.variant(), other.variant(),
                                f"{parameter} is invisible in the variant")

    def test_the_trace_cap_changes_an_outcome_it_must_identify(self):
        # The pairing the omission allowed: identical configuration records,
        # opposite results.
        outcomes = {}
        for cap in (2 ** 24, 1):
            sim = self.a_sim(trace_cap=cap)
            sim.schedule_birth(0.0, KIND_PQ, node_id=0)
            sim.schedule_birth(1.0, KIND_PQ, node_id=0)
            sim.schedule_ticks(1)
            sim.run(until=20_000.0)
            outcomes[cap] = (sim.proofs_completed, sim.proof_jobs_over_capacity)
        proved, refused = outcomes[2 ** 24]
        self.assertGreater(proved, 0)
        self.assertEqual(refused, 0)
        proved, refused = outcomes[1]
        self.assertEqual(proved, 0)
        self.assertGreater(refused, 0)

    def test_absence_of_a_circuit_cost_is_a_sentinel_not_a_zero(self):
        plain = NetworkSim(two_node_overlay(0.0))
        for name in ("circuit_ms_per_hash", "circuit_sphincs_hashes",
                     "circuit_trace_cap"):
            self.assertEqual(plain.variant()[name], -1)


class TestOverCapacityPlansCostNothing(unittest.TestCase):
    """An impossible plan must be refused before it is charged for.

    `fits` is a pure function of the plan, so capacity is knowable before any
    submission. Deciding it job by job instead let earlier jobs start -- an idle
    server begins service synchronously, and cancellation cannot recall work in
    progress -- so a plan reported as abandoned had spent CPU and completed
    internal proofs.
    """

    def test_a_refused_plan_spends_no_proving_time(self):
        cost = CircuitCost.anchored_on_pairwise_fold(889.0, trace_cap=10_000)
        sim = NetworkSim(two_node_overlay(0.0), prove_cost_basis="circuit",
                         fold_plan="binary", aggregation_trigger="aligned",
                         proving_budget=1e9,
                         costs=CostTable(prove_recursive=cost))
        # Four signatures in a binary tree: the leaf folds fit, the root does
        # not, because it verifies two nested proofs plus the overhead.
        for _ in range(4):
            sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_ticks(1)
        sim.run(until=3000.0)

        self.assertGreater(sim.proof_jobs_over_capacity, 0)
        self.assertEqual(sim.proof_jobs_over_capacity, sim.plans_abandoned)
        self.assertEqual(sim.proofs_completed, 0)
        self.assertEqual(sim.cpu[0].service_by_kind.get(PROVE_RECURSIVE, 0.0),
                         0.0)

    def test_a_plan_that_fits_is_still_proved(self):
        # The preflight must not refuse work that is merely large.
        cost = CircuitCost.anchored_on_pairwise_fold(889.0)
        sim = NetworkSim(two_node_overlay(0.0), prove_cost_basis="circuit",
                         fold_plan="binary", aggregation_trigger="aligned",
                         proving_budget=1e9,
                         costs=CostTable(prove_recursive=cost))
        for _ in range(4):
            sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_ticks(1)
        sim.run(until=20_000.0)
        self.assertEqual(sim.proof_jobs_over_capacity, 0)
        self.assertGreater(sim.proofs_completed, 0)
