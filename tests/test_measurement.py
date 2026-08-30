#!/usr/bin/env python3
"""Measurement properties independent of how observation is set up.

Equal windows make objects comparable, thresholds that are never reached are
censored, and extending a run at the same birth rate does not move its statistic.
The statistic must measure the network rather than its own procedure.
"""

import random
import unittest

from starksim.arena import KIND_PQ
from starksim.costs import CircuitCost, CostTable
from starksim.netsim import NetworkSim
from starksim.network import Overlay, bytes_per_ms

from tests.support import line_overlay, two_node_overlay


def idle_line(node_count, latency=100.0):
    """A line graph with no bandwidth or computation cost.

    Admission time is then exactly the summed hop latency, which makes the
    expected value of every statistic below arithmetic rather than emergent.
    """
    return line_overlay(node_count, base_latency=latency)


class TestAdmissionDelays(unittest.TestCase):
    def test_entry_node_has_zero_delay_and_delays_ascend(self):
        sim = NetworkSim(idle_line(5), seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = next(iter(sim.arena.objects.values()))
        delays = sim.admission_delays(obj)
        self.assertEqual(delays, [0.0, 100.0, 200.0, 300.0, 400.0])

    def test_window_excludes_admissions_beyond_it(self):
        sim = NetworkSim(idle_line(5), seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = next(iter(sim.arena.objects.values()))
        # A 250 ms window covers hops 0, 1 and 2 and nothing further.
        self.assertEqual(sim.admission_delays(obj, 250.0), [0.0, 100.0, 200.0])
        self.assertEqual(sim.coverage_fraction_within(obj, 250.0), 3 / 5)

    def test_delays_are_measured_from_birth_not_from_zero(self):
        sim = NetworkSim(idle_line(4), seed=1)
        sim.schedule_birth(7000.0, KIND_PQ, node_id=0)
        sim.run()
        obj = next(iter(sim.arena.objects.values()))
        self.assertEqual(sim.admission_delays(obj), [0.0, 100.0, 200.0, 300.0])


class TestTimeToCoverage(unittest.TestCase):
    def test_full_coverage_is_the_slowest_admission(self):
        sim = NetworkSim(idle_line(5), seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = next(iter(sim.arena.objects.values()))
        self.assertEqual(sim.time_to_coverage(obj, 1.0), 400.0)

    def test_fraction_rounds_up_to_a_whole_node(self):
        # Five nodes: half of them means three of them, not two and a half, so
        # the answer is the third smallest delay.
        sim = NetworkSim(idle_line(5), seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = next(iter(sim.arena.objects.values()))
        self.assertEqual(sim.time_to_coverage(obj, 0.5), 200.0)
        self.assertEqual(sim.time_to_coverage(obj, 0.2), 0.0)

    def test_short_window_censors_instead_of_returning_a_large_delay(self):
        sim = NetworkSim(idle_line(5), seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = next(iter(sim.arena.objects.values()))
        # Reachable given time, but not within the window: the answer has to be
        # "not observed", because the run cannot say when it would have been.
        self.assertIsNone(sim.time_to_coverage(obj, 1.0, 250.0))
        self.assertEqual(sim.time_to_coverage(obj, 0.5, 250.0), 200.0)

    def test_unreachable_node_censors_full_coverage_forever(self):
        overlay = Overlay.from_adjacency([[1], [0], [3], [2]],
                                         base_latency=10.0)
        sim = NetworkSim(overlay, seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        obj = next(iter(sim.arena.objects.values()))
        self.assertIsNone(sim.time_to_coverage(obj, 1.0))
        self.assertEqual(sim.time_to_coverage(obj, 0.5), 10.0)


class TestEqualWindowsRemoveTheAgeConfound(unittest.TestCase):
    """Equal observation windows keep age out of the reach statistic."""

    def _two_objects_far_apart(self):
        # Same entry node and no jitter, so the two objects differ in nothing
        # except when they were born. An idle network means no congestion can
        # explain a difference either.
        sim = NetworkSim(idle_line(6), seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_birth(5000.0, KIND_PQ, node_id=0)
        sim.run(until=5250.0)
        ordered = sorted(sim.arena.objects.values(), key=lambda obj: obj.born)
        return sim, ordered

    def test_shared_horizon_sampling_confounds_age_with_reach(self):
        # What the old harness did: sample every object at the end of the run.
        # The two objects are identical, so any difference here is the flaw.
        sim, (first, last) = self._two_objects_far_apart()
        self.assertEqual(sim.residency_fraction(first), 1.0)
        self.assertEqual(sim.residency_fraction(last), 3 / 6)

    def test_equal_windows_measure_identical_objects_identically(self):
        # What the harness does now: each object observed for the same span
        # from its own birth. The difference is gone, because it was never
        # about the network.
        sim, (first, last) = self._two_objects_far_apart()
        window = 250.0
        self.assertEqual(sim.coverage_fraction_within(first, window),
                         sim.coverage_fraction_within(last, window))
        self.assertEqual(sim.time_to_coverage(first, 0.5, window),
                         sim.time_to_coverage(last, 0.5, window))

    def test_coverage_within_window_is_stable_as_the_run_lengthens(self):
        # At a fixed birth rate, a longer run offers the same instantaneous
        # load and should give the same answer. The old statistic drifted with
        # object count for no physical reason; this one must not.
        spacing = 500.0
        window = 400.0
        results = []
        for objects in (4, 8, 16):
            rng = random.Random("stability:topology")
            overlay = Overlay.random_regular(30, 4, rng, base_latency=50.0)
            sim = NetworkSim(overlay, seed=3, capacity=bytes_per_ms(100.0),
                             costs=CostTable(verify_recv=1.0))
            for index in range(objects):
                sim.schedule_birth(index * spacing, KIND_PQ)
            sim.run(until=objects * spacing + window)
            coverages = []
            for obj in sim.arena.objects.values():
                coverages.append(sim.coverage_fraction_within(obj, window))
            results.append(sum(coverages) / len(coverages))
        for value in results[1:]:
            self.assertAlmostEqual(results[0], value, delta=0.02)


class TestInclusionByDistance(unittest.TestCase):
    def test_counts_every_transaction_born_not_only_included_ones(self):
        # The delay correlation can only speak for transactions that reached a
        # block. If distance stops a transaction being included at all, that
        # effect is invisible there and has to be visible here.
        rng = random.Random("inclusion:topology")
        overlay = Overlay.random_regular(24, 4, rng, base_latency=100.0)
        sim = NetworkSim(overlay, seed=2, capacity=bytes_per_ms(50.0),
                         costs=CostTable(prove_recursive=lambda deps: 30.0),
                         aggregation_interval=500.0, slot_length=1000.0,
                         block_budget=1)
        for index in range(60):
            sim.schedule_birth(index * 100.0, KIND_PQ)
        sim.schedule_ticks(12)
        sim.schedule_slots(6)
        sim.run()
        table = sim.inclusion_by_distance(0)
        born = sum(count for count, _ in table.values())
        included = sum(count for _, count in table.values())
        self.assertEqual(born, sim.arrived_txs)
        self.assertEqual(included, sim.chain.included_count())
        # The point of the table: some were never included, and the budget here
        # guarantees it.
        self.assertLess(included, born)
        for hops, (count, got) in table.items():
            self.assertLessEqual(got, count)


class TestInclusionDistanceCorrelation(unittest.TestCase):
    def _run(self, lifetime):
        # Expiry, not the block budget, is what actually excludes transactions
        # here: one fold carries many of them, so a budget of two folds per
        # block absorbs everything this load offers. A deadline is the realistic
        # way a transaction fails to be included at all.
        rng = random.Random("corr:topology")
        overlay = Overlay.random_regular(30, 4, rng, base_latency=200.0)
        sim = NetworkSim(overlay, seed=4, capacity=bytes_per_ms(50.0),
                         costs=CostTable(prove_recursive=lambda deps: 30.0),
                         aggregation_interval=500.0, slot_length=1000.0,
                         proposer_mode="fixed", proposer_id=0, block_budget=2)
        for index in range(120):
            sim.schedule_birth(index * 50.0, KIND_PQ, lifetime=lifetime)
        sim.schedule_ticks(20)
        sim.schedule_slots(10)
        sim.run()
        return sim

    def test_undefined_when_nothing_was_excluded(self):
        # Every transaction included means the inclusion indicator never
        # varies. The honest answer is "no such effect to measure", not zero:
        # zero would report that distance was tested and found not to matter.
        sim = self._run(lifetime=2500.0)
        self.assertEqual(sim.chain.included_count(), sim.arrived_txs)
        self.assertIsNone(sim.inclusion_distance_correlation(0))

    def test_negative_once_a_deadline_excludes_the_distant(self):
        # And it has to work when there *is* something to measure, or the
        # structural None above would be indistinguishable from a broken metric.
        # Negative is the direction the claim predicts: with a fixed proposer,
        # entering far away means expiring before ever being included.
        sim = self._run(lifetime=800.0)
        self.assertLess(sim.chain.included_count(), sim.arrived_txs)
        correlation = sim.inclusion_distance_correlation(0)
        self.assertLess(correlation, -0.2)

class TestReachVersusResidency(unittest.TestCase):
    """The distinction that made `coverage_fraction_within` a hybrid."""

    def _run_that_retires_objects(self):
        rng = random.Random("retire:topology")
        overlay = Overlay.random_regular(20, 4, rng, base_latency=50.0)
        sim = NetworkSim(overlay, seed=1, capacity=bytes_per_ms(50.0),
                         costs=CostTable(prove_recursive=lambda deps: 20.0),
                         aggregation_interval=250.0, slot_length=500.0)
        for index in range(30):
            sim.schedule_birth(index * 50.0, KIND_PQ)
        sim.schedule_ticks(8)
        sim.schedule_slots(4)
        sim.run()
        return sim

    def test_the_propagation_shape_has_no_departures(self):
        # Which is the only reason the hybrid was ever exact, and the reason it
        # went unnoticed: the harness that uses it cannot expose it.
        sim = NetworkSim(idle_line(5), seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run()
        self.assertEqual(sim.total_departures(), 0)
        self.assertEqual(sim.residency_fraction(sim.arena.leaves[0]), 1.0)
        self.assertEqual(len(sim.admission_delays(sim.arena.leaves[0])), 5)

    def test_the_refusal_is_per_object_not_per_run(self):
        """A departed object is refused; its neighbours are still answerable.

        An unrelated eviction must not block an answer about a retained object.
        Eviction under a candidate bound gives a run with both kinds in it.
        """
        rng = random.Random("mixed:topology")
        overlay = Overlay.random_regular(10, 4, rng, base_latency=50.0)
        sim = NetworkSim(overlay, seed=2, capacity=bytes_per_ms(50.0),
                         candidate_limit=2)
        for index in range(8):
            sim.schedule_birth(index * 60.0, KIND_PQ)
        sim.run()
        departed, kept = [], []
        for leaf in sim.arena.leaves:
            (departed if leaf.id in sim._departed else kept).append(leaf)
        # The fixture has to contain both, or it proves only one half.
        self.assertTrue(departed)
        self.assertTrue(kept)
        with self.assertRaises(ValueError):
            sim.admission_delays(departed[0])
        delays = sim.admission_delays(kept[0])
        self.assertIn(0.0, delays)
        self.assertEqual(len(delays), sim.overlay.node_count)

    def test_a_window_the_run_has_not_observed_is_refused(self):
        """Asking about 1000 units after observing 1 answered for the 1.

        The number described one unit of observation while claiming a thousand,
        which is the same species of error as reporting residency as reach.
        """
        sim = NetworkSim(idle_line(5), seed=1)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run(until=150.0)
        leaf = sim.arena.leaves[0]
        # Elapsed: answerable.
        self.assertEqual(sim.coverage_fraction_within(leaf, 100.0), 2 / 5)
        # Not elapsed: refused rather than answered for a shorter window.
        with self.assertRaises(ValueError):
            sim.coverage_fraction_within(leaf, 5000.0)
        with self.assertRaises(ValueError):
            sim.time_to_coverage(leaf, 1.0, 5000.0)

    def test_a_folding_run_refuses_the_reach_question(self):
        sim = self._run_that_retires_objects()
        self.assertGreater(sim.total_departures(), 0)
        leaf = sim.arena.leaves[0]
        # Residency is answerable and honest: it has fallen to nothing because
        # the object was folded away and pruned, not because it never spread.
        self.assertEqual(sim.residency_fraction(leaf), 0.0)
        # Reach is not answerable from final state, so it is refused rather than
        # answered with residency-among-survivors under a propagation name.
        for call in (lambda: sim.admission_delays(leaf),
                     lambda: sim.coverage_fraction_within(leaf, 500.0),
                     lambda: sim.time_to_coverage(leaf, 0.5, 500.0)):
            with self.assertRaises(ValueError):
                call()

if __name__ == "__main__":
    unittest.main()


class TestStructuralAbsenceIsNotZero(unittest.TestCase):
    """A configuration that cannot answer a question must say so.

    `-1` is the project's sentinel for "this run cannot answer this", and the
    reason is that a zero is indistinguishable from evidence: "no job exceeded
    the trace ceiling" and "nothing here has a trace ceiling" are different
    claims, and so are "blocks carried no transactions" and "no block was built".
    """

    def test_transactions_per_block_with_no_block_is_not_zero(self):
        sim = NetworkSim(two_node_overlay(0.0))
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run(until=100.0)
        self.assertEqual(sim.chain.blocks, [])
        self.assertEqual(sim.metrics()["txs_per_block"], -1.0)

    def test_over_capacity_is_a_sentinel_without_a_circuit_ceiling(self):
        for basis, expected in (("deps", -1), ("inputs", -1)):
            sim = NetworkSim(two_node_overlay(0.0), prove_cost_basis=basis)
            self.assertEqual(sim.metrics()["proof_jobs_over_capacity"], expected,
                             f"basis {basis}")
        circuit = NetworkSim(two_node_overlay(0.0), prove_cost_basis="circuit",
                             costs=CostTable(prove_recursive=CircuitCost(
                                 ms_per_hash=1.0)))
        self.assertEqual(circuit.metrics()["proof_jobs_over_capacity"], 0)
