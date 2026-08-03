#!/usr/bin/env python3
"""Unit tests for v2 networking: visibility after l, herd duplication,
discard of fully-included duplicate folds, single inclusion, stale
builder view."""

import io
import json
import unittest

from starksim.objects import AggregatedSTARK, PrivacyTx
from starksim.sim import Simulation, csv_row


def run_traced(**kw):
    buf = io.StringIO()
    sim = Simulation(trace=buf, paranoid=True, **kw)
    m = sim.run()
    events = [json.loads(line) for line in buf.getvalue().splitlines()]
    return sim, m, events


class TestDefaults(unittest.TestCase):
    def test_constructor_defaults_to_v1(self):
        # library default is the v1 instant pool; networking is opt-in
        # (the CLI opts in with --l 100 by default)
        _, m, _ = run_traced(r_ms=500, n_agg=1, lam_priv=0, lam_pq=0,
                             slots_n=1, seed=1, arrivals=[])
        self.assertEqual(m["l_ms"], 0)


class TestVisibility(unittest.TestCase):
    def test_arrival_becomes_foldable_after_l(self):
        sim, m, ev = run_traced(r_ms=500, n_agg=1, lam_priv=0, lam_pq=0,
                                slots_n=1, seed=1, l_ms=100,
                                arrivals=[(0, "priv"), (0, "priv")])
        fs = [e for e in ev if e["e"] == "fs"]
        fd = [e for e in ev if e["e"] == "fd"]
        self.assertEqual(fs[0]["t"], 100)       # not 0: one hop first
        self.assertEqual(fd[0]["t"], 600)       # then exactly r
        self.assertEqual(m["wasted_consumptions"], 0)

    def test_producer_chains_own_output_instantly(self):
        sim, m, ev = run_traced(r_ms=100, n_agg=1, lam_priv=0, lam_pq=0,
                                slots_n=1, seed=1, l_ms=100,
                                arrivals=[(0, "priv")] * 3)
        fs = [e for e in ev if e["e"] == "fs"]
        # the lone aggregator never waits l for its own output: at 200 the
        # only usable pair is that fresh output (id 3, born 200) plus the
        # leftover tx, so folds run back to back at 100, 200
        self.assertEqual([e["t"] for e in fs], [100, 200])
        leftover = ({0, 1, 2} - {fs[0]["a"], fs[0]["b"]}).pop()
        self.assertEqual(sorted((fs[1]["a"], fs[1]["b"])),
                         sorted((3, leftover)))

    def test_builder_does_not_see_last_l_ms(self):
        # no aggregators: raw txs flow straight to the builder's view
        sim, m, ev = run_traced(r_ms=500, n_agg=0, lam_priv=0, lam_pq=0,
                                slots_n=2, seed=1, l_ms=100,
                                arrivals=[(0, "priv"), (0, "priv"),
                                          (3950, "priv")])
        self.assertEqual([b.n_priv for b in sim.blocks], [2, 1])
        # the no-8288 baseline builder is equally blind to the late tx
        self.assertEqual(sim.blocks[0].folds_baseline, 1)
        self.assertEqual(sim.blocks[0].folds_8288, 1)


class TestSupersedure(unittest.TestCase):
    def test_supersedure_window_is_l(self):
        sim = Simulation(500, 2, 0, 0, 1, 1, arrivals=[], l_ms=100)
        x, y = PrivacyTx(0, 0.0), PrivacyTx(1, 0.0)
        fold = AggregatedSTARK(2, x, y, 400.0, 0)
        self.assertTrue(sim.superseded(x, 0, 401))       # creator: instant
        self.assertFalse(sim.superseded(x, 1, 499))      # still alive there
        self.assertTrue(sim.superseded(x, 1, 500))
        self.assertFalse(sim.visible(fold, 1, 499))
        self.assertTrue(sim.visible(fold, 1, 500))


class TestRaces(unittest.TestCase):
    def herd(self, **kw):
        # two aggregators, two txs at t=0: both go idle at the same instant
        # and, blind to each other's in-flight folds, fold the same pair
        return run_traced(r_ms=500, n_agg=2, lam_priv=0, lam_pq=0,
                          seed=1, slot_length=700.0,
                          arrivals=[(0, "priv"), (0, "priv")], **kw)

    def test_same_instant_herd_duplicates_the_fold(self):
        sim, m, ev = self.herd(slots_n=1, l_ms=100)
        fs = [e for e in ev if e["e"] == "fs"]
        # herd at 100: neither aggregator can see the other's fold, both
        # fold the same pair; at the 700 boundary the block includes the
        # raw txs, so the two identical aggregates arriving one tick
        # later are dead on the spot -- discarded, never re-folded
        # a fold's input order is a random draw; the pair is what matters
        self.assertEqual([(e["t"], *sorted((e["a"], e["b"]))) for e in fs],
                         [(100, 0, 1), (100, 0, 1)])
        self.assertEqual(m["agg_folds_total"], 2)
        self.assertEqual(m["wasted_consumptions"], 2)
        self.assertEqual(m["waste_frac"], 0.5)
        # the duplicate folds' hop lands exactly at the boundary, one tick
        # too late for the builder: it includes the two raw txs instead,
        # each once
        inc = [e for e in ev if e["e"] == "inc"]
        self.assertEqual(inc[0]["items"], [0, 1])
        self.assertEqual(m["included_txs"], 2)

    def test_l0_still_races(self):
        # even at l = 0 an in-flight fold is invisible until it completes,
        # so the same-instant herd duplicates the fold and re-herds once
        # on the identical aggregates it produced
        sim, m, ev = self.herd(slots_n=1, l_ms=0)
        self.assertEqual(m["agg_folds_total"], 4)
        self.assertEqual(m["wasted_consumptions"], 4)
        self.assertEqual(m["included_txs"], 2)

    def test_txs_counted_once_and_dead_duplicates_discarded(self):
        sim, m, ev = self.herd(slots_n=2, l_ms=100)
        # block 2 has nothing new: the duplicate aggregates, covering only
        # already-included txs, were discarded at the block-1 boundary, so
        # nobody folds or propagates them
        self.assertEqual([b.n_priv for b in sim.blocks], [2, 0])
        self.assertEqual(m["included_txs"], 2)
        self.assertEqual(m["agg_folds_total"], 2)
        self.assertEqual(m["wasted_consumptions"], 2)
        self.assertEqual(m["end_pool_items"], 0)


class TestOverlappingAggregates(unittest.TestCase):
    # two racing folds consume the same inputs and produce two aggregates
    # covering the same txs; a later fold merges the duplicates

    def test_union_coverage_additive_total(self):
        a, b = PrivacyTx(0, 0.0), PrivacyTx(1, 0.0)
        duplicate1 = AggregatedSTARK(2, a, b, 500.0, 0)
        duplicate2 = AggregatedSTARK(3, a, b, 500.0, 1)
        heal = AggregatedSTARK(4, duplicate1, duplicate2, 1000.0, 0)
        self.assertEqual(heal.pending, [a, b])   # each tx kept once
        self.assertEqual(heal.total, 4)          # embodied work is additive
        self.assertEqual(a.parents, [duplicate1, duplicate2])
        self.assertEqual(heal.depth, 2)

    def test_union_drops_included_txs(self):
        a, b = PrivacyTx(0, 0.0), PrivacyTx(1, 0.0)
        duplicate1 = AggregatedSTARK(2, a, b, 500.0, 0)
        duplicate2 = AggregatedSTARK(3, a, b, 500.0, 1)
        a.included = True
        heal = AggregatedSTARK(4, duplicate1, duplicate2, 1000.0, 0)
        self.assertEqual(heal.pending, [b])


class TestDeterminism(unittest.TestCase):
    def test_same_seed_same_csv_row_with_latency(self):
        kw = dict(r_ms=500, n_agg=10, lam_priv=5, lam_pq=5, slots_n=50,
                  seed=7, l_ms=100)
        self.assertEqual(csv_row(Simulation(**kw).run()),
                         csv_row(Simulation(**kw).run()))
        kw["r_jitter"] = 0.1
        self.assertEqual(csv_row(Simulation(**kw).run()),
                         csv_row(Simulation(**kw).run()))


if __name__ == "__main__":
    unittest.main()
