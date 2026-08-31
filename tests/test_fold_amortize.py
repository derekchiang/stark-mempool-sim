#!/usr/bin/env python3
"""The amortization factor on the crossover fold threshold."""

import unittest

from starksim.arena import KIND_PQ
from starksim.costs import CostTable
from tests.support import make_sim


def crossover_sim(**kwargs):
    """Two PQ births whose witnesses weigh far less than a recursive proof.

    Two signature leaves carry roughly 6 kB of witnesses against a 256 kB
    proof, so plain crossover always defers here and only amortization can
    make the fold happen.
    """
    sim = make_sim(costs=CostTable(),
                   fold_threshold="crossover",
                   aggregation_trigger="aligned",
                   proving_budget=1e9, **kwargs)
    for _ in range(2):
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
    sim.schedule_ticks(1)
    sim.run(until=2000.0)
    return sim.metrics()


class TestFoldAmortize(unittest.TestCase):
    def test_the_local_reading_defers_what_amortization_folds(self):
        local = crossover_sim()
        self.assertEqual(local["proofs_completed"], 0)
        self.assertGreater(local["folds_deferred"], 0)
        # ~6 kB of witnesses at factor 100 outweigh the 256 kB proof.
        amortized = crossover_sim(fold_amortize=100.0)
        self.assertGreater(amortized["proofs_completed"], 0)
        self.assertEqual(amortized["folds_deferred"], 0)

    def test_a_factor_still_below_the_proof_defers(self):
        # 2 x (96 + 2560 + 512) = 6336 B of witnesses; at factor 10 that is
        # 63 360 B, still under the 262 144 B proof, so the comparison must
        # still defer -- the factor scales the comparison, it does not
        # replace it.
        still_local = crossover_sim(fold_amortize=10.0)
        self.assertEqual(still_local["proofs_completed"], 0)
        self.assertGreater(still_local["folds_deferred"], 0)

    def test_the_factor_is_refused_where_it_is_inert(self):
        # Under any other threshold nothing consults it (docs/pitfalls.md:
        # a configuration that cannot affect the run must be refused, not
        # recorded).
        with self.assertRaises(ValueError):
            make_sim(fold_amortize=4.0)

    def test_below_one_is_refused(self):
        with self.assertRaises(ValueError):
            make_sim(fold_threshold="crossover", fold_amortize=0.5)

    def test_a_numeric_string_is_coerced_for_the_override_syntax(self):
        # Bench preset overrides arrive as strings.
        sim = make_sim(fold_threshold="crossover", fold_amortize="4")
        self.assertEqual(sim.fold_amortize, 4.0)
        self.assertEqual(sim.variant()["fold_amortize"], 4.0)

    def test_the_variant_carries_the_factor(self):
        sim = make_sim(fold_threshold="crossover", fold_amortize=16.0)
        self.assertEqual(sim.variant()["fold_amortize"], 16.0)
        default = make_sim()
        self.assertEqual(default.variant()["fold_amortize"], 1.0)


if __name__ == "__main__":
    unittest.main()
