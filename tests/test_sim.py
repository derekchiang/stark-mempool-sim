#!/usr/bin/env python3
"""Unit tests for sim.py (acceptance check 5): event ordering, fold timing,
slot snapshot, builder budget, no double-fold, determinism."""

import io
import json
import unittest

from starksim.sim import Simulation, csv_row

SLOT = 4000.0


def run_traced(**kw):
    buf = io.StringIO()
    sim = Simulation(trace=buf, paranoid=True, **kw)
    m = sim.run()
    events = [json.loads(line) for line in buf.getvalue().splitlines()]
    return sim, m, events


class TestFoldTiming(unittest.TestCase):
    def test_fold_takes_exactly_r(self):
        sim, m, ev = run_traced(r_ms=500, n_agg=1, lam_priv=0, lam_pq=0,
                                slots_n=2, seed=1,
                                arrivals=[(100, "priv"), (150, "priv")])
        fs = [e for e in ev if e["e"] == "fs"]
        fd = [e for e in ev if e["e"] == "fd"]
        self.assertEqual(len(fs), 1)
        self.assertEqual(len(fd), 1)
        self.assertEqual(fs[0]["t"], 150)          # starts when 2nd item lands
        self.assertEqual(fd[0]["t"], 650)          # exactly r later
        self.assertEqual(fd[0]["tot"], 2)
        # both txs included at the first boundary
        self.assertEqual(m["included_txs"], 2)
        self.assertAlmostEqual(m["delay_mean_slots"],
                               ((SLOT - 100) + (SLOT - 150)) / 2 / SLOT)


class TestEventOrdering(unittest.TestCase):
    def test_same_time_arrivals_keep_schedule_order(self):
        sim, m, ev = run_traced(r_ms=500, n_agg=0, lam_priv=0, lam_pq=0,
                                slots_n=1, seed=1,
                                arrivals=[(100, "priv"), (100, "pq")])
        self.assertEqual([e["kind"] for e in ev if e["e"] == "arr"],
                         ["priv", "pq"])

    def test_boundary_processed_before_same_time_fold_done(self):
        # fold finishes exactly at t=4000: the block-1 builder must not see
        # the aggregate -- he folds the two raw STARKs himself (1 fold),
        # and the aggregate lands right after with nothing left pending.
        sim, m, ev = run_traced(r_ms=500, n_agg=1, lam_priv=0, lam_pq=0,
                                slots_n=2, seed=1,
                                arrivals=[(3400, "priv"), (3500, "priv")])
        fd = [e for e in ev if e["e"] == "fd"]
        inc = [e for e in ev if e["e"] == "inc"]
        self.assertEqual(fd[0]["t"], 4000)
        self.assertEqual(fd[0]["size"], 0)          # boundary ran first
        self.assertEqual([b.n_priv for b in sim.blocks], [2, 0])
        self.assertEqual(sorted(inc[0]["items"]), [0, 1])   # the raw STARKs
        self.assertEqual(inc[0]["folds"], 1)

    def test_events_processed_in_time_order(self):
        sim, m, ev = run_traced(r_ms=100, n_agg=2, lam_priv=20, lam_pq=20,
                                slots_n=5, seed=7)
        ts = [e["t"] for e in ev if e["e"] != "cfg"]
        self.assertEqual(ts, sorted(ts))


class TestPqArrivals(unittest.TestCase):
    def test_each_pq_tx_is_its_own_item(self):
        # no aggregators: 3 pq txs -> 3 single-tx objects; the builder folds
        # them into the block himself (2 folds)
        sim, m, ev = run_traced(r_ms=500, n_agg=0, lam_priv=0, lam_pq=0,
                                slots_n=1, seed=1,
                                arrivals=[(10, "pq"), (20, "pq"), (30, "pq")])
        self.assertEqual(m["included_txs"], 3)
        self.assertEqual([b.folds_used for b in sim.blocks], [2])
        # the included sigs cover nothing pending -- discarded at the boundary
        self.assertEqual(len(sim.pool), 0)


class TestSlotSnapshot(unittest.TestCase):
    def test_handcrafted_metrics(self):
        # 3 priv txs at 100/200/300, N=1, r=500:
        # fold(tx0,tx1) 200->700, fold(agg2,tx2) 700->1200 => one item of 3.
        sim, m, ev = run_traced(r_ms=500, n_agg=1, lam_priv=0, lam_pq=0,
                                slots_n=2, seed=1,
                                arrivals=[(100, "priv"), (200, "priv"), (300, "priv")])
        slot1 = [e for e in ev if e["e"] == "slot"][0]
        self.assertEqual(slot1["items"], 1)      # single completed aggregate
        self.assertEqual(slot1["inc_priv"], 3)
        self.assertEqual(slot1["pending"], 0)    # after inclusion
        # builder folds seen by block 1: with-8288 = 1 item - 1 = 0,
        # baseline = 3 raw txs - 1 = 2
        self.assertEqual(sim.blocks[0].folds_8288, 0)
        self.assertEqual(sim.blocks[0].folds_baseline, 2)
        self.assertEqual(sim.blocks[1].folds_8288, 0)   # empty giant remains, 0 pending
        self.assertEqual(sim.blocks[1].folds_baseline, 0)
        d = [(SLOT - 100) / SLOT, (SLOT - 200) / SLOT, (SLOT - 300) / SLOT]
        self.assertAlmostEqual(m["delay_mean_slots"], sum(d) / 3)
        self.assertAlmostEqual(m["delay_p95_slots"], max(d))
        self.assertEqual(m["priv_per_block"], 1.5)

    def test_locked_items_still_included(self):
        # agg(2) completes at 3200, immediately re-locked folding with the
        # raw tx that lands at 3300 (fold ends 6300). The block-1 builder
        # must still see both locked proofs and fold them together; the
        # in-flight fold result must not resurrect the included txs.
        arr = [(100, "priv"), (200, "priv"), (3300, "priv")]
        sim, m, ev = run_traced(r_ms=3000, n_agg=1, lam_priv=0, lam_pq=0,
                                slots_n=3, seed=1, arrivals=arr)
        inc = [e for e in ev if e["e"] == "inc"]
        self.assertEqual([b.n_priv for b in sim.blocks], [3, 0, 0])
        self.assertEqual(len(inc), 1)
        self.assertEqual(sorted(inc[0]["items"]), [2, 3])   # agg + locked raw
        self.assertEqual(inc[0]["folds"], 1)
        fd = [e for e in ev if e["e"] == "fd"]
        self.assertEqual(fd[-1]["size"], 0)


class TestDuplicateFolds(unittest.TestCase):
    def test_repeat_consumptions_counted_as_waste(self):
        sim, m, ev = run_traced(r_ms=200, n_agg=4, lam_priv=15, lam_pq=15,
                                slots_n=30, seed=3)
        # aggregators do not coordinate: an item may be handed to several
        # folds, and every repeat consumption is booked as waste
        inputs = []
        for e in ev:
            if e["e"] == "fs":
                inputs.append(e["a"])
                inputs.append(e["b"])
        self.assertEqual(len(inputs) - len(set(inputs)),
                         m["wasted_consumptions"])
        # every fold that started also finished, with matching inputs
        fs = {(e["a"], e["b"]) for e in ev if e["e"] == "fs"}
        fd = set()
        for e in ev:
            if e["e"] == "fd" and e["t"] <= 30 * SLOT - 200:
                fd.add((e["a"], e["b"]))
        self.assertTrue(fd <= fs)


class TestBuilderBudget(unittest.TestCase):
    def test_budget_caps_items_per_block(self):
        # 10 raw privacy STARKs, no aggregators, B=5: block 1 takes the
        # top 6 objects (5 folds), block 2 the remaining 4 (3 folds)
        arr = [(10 * i, "priv") for i in range(1, 11)]
        sim, m, ev = run_traced(r_ms=500, n_agg=0, lam_priv=0, lam_pq=0,
                                slots_n=2, seed=1, arrivals=arr)
        self.assertEqual([b.n_priv for b in sim.blocks], [6, 4])
        self.assertEqual([b.folds_used for b in sim.blocks], [5, 3])

    def test_lone_pq_sig_costs_one_fold(self):
        # B=1: included, at the cost of the sig-proving fold
        sim, m, _ = run_traced(r_ms=500, n_agg=0, lam_priv=0, lam_pq=0,
                               slots_n=1, seed=1, b_folds=1,
                               arrivals=[(10, "pq")])
        self.assertEqual(m["included_txs"], 1)
        self.assertEqual([b.folds_used for b in sim.blocks], [1])

    def test_lone_raw_stark_is_free(self):
        # a single raw privacy STARK is included as-is, no folds spent
        sim, m, _ = run_traced(r_ms=500, n_agg=0, lam_priv=0, lam_pq=0,
                               slots_n=1, seed=1, b_folds=1,
                               arrivals=[(10, "priv")])
        self.assertEqual(m["included_txs"], 1)
        self.assertEqual([b.folds_used for b in sim.blocks], [0])

    def test_b_folds_zero_rejected(self):
        # B=0 would strand bare PQ sigs; the constructor rejects it
        with self.assertRaises(ValueError):
            Simulation(500, 0, 0, 0, 1, 1, b_folds=0)


class TestSlotLength(unittest.TestCase):
    def test_slot_length_configurable(self):
        # 8 s slots: boundary fires at t=8000 and delays normalize to it
        sim, m, ev = run_traced(r_ms=500, n_agg=1, lam_priv=0, lam_pq=0,
                                slots_n=1, seed=1, slot_length=8000,
                                arrivals=[(100, "priv"), (150, "priv")])
        slot = [e for e in ev if e["e"] == "slot"][0]
        self.assertEqual(slot["t"], 8000)
        self.assertEqual(m["included_txs"], 2)
        self.assertAlmostEqual(m["delay_mean_slots"],
                               ((8000 - 100) + (8000 - 150)) / 2 / 8000)


class TestDeterminism(unittest.TestCase):
    def test_same_seed_same_csv_row(self):
        kw = dict(r_ms=500, n_agg=10, lam_priv=12, lam_pq=12, slots_n=50, seed=42)
        r1 = csv_row(Simulation(**kw).run())
        r2 = csv_row(Simulation(**kw).run())
        self.assertEqual(r1, r2)

    def test_different_seed_differs(self):
        kw = dict(r_ms=500, n_agg=10, lam_priv=12, lam_pq=12, slots_n=50)
        r1 = csv_row(Simulation(seed=1, **kw).run())
        r2 = csv_row(Simulation(seed=2, **kw).run())
        self.assertNotEqual(r1, r2)


if __name__ == "__main__":
    unittest.main()
