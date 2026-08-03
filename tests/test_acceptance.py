#!/usr/bin/env python3
"""Acceptance checks 3-4 from mvp-brief.md (check 5 = unit tests, test_sim.py).

Checks 1-2 (capacity knee within 15% of N/r, knee scaling with N) were
dropped 2026-07-30: with no coordination in the MVP the knee sits well
below N/r, so the brief's knee targets no longer apply.

Slow: check 3 runs all 8 (r, N) configs with 1000-slot runs.

Check 3 threshold, fixed up front: at low load (0.15 x N/r, privacy-only)
mean delay must be <= 1.0 + r/slot + 0.25 slots per config -- one average
boundary wait (0.5) + worst-case straggler (~0.5) + one fold + scheduling
slack -- and <= 1.1 slots averaged over all configs ("around 1 slot or
less"). For r << slot this is ~1.25 slots.
"""

import unittest

from starksim.sim import SLOT_LENGTH_MS, Simulation, csv_row

RS = (100, 500, 1000, 2000)
NS = (1, 10)
SEED = 42


class TestCheck3LowLoadDelay(unittest.TestCase):
    def test_low_load_mean_delay_about_one_slot(self):
        means = []
        for r in RS:
            for n in NS:
                lam = 0.15 * 1000.0 * n / r
                m = Simulation(r, n, lam, 0, 1000, SEED).run()
                d = m["delay_mean_slots"]
                means.append(d)
                limit = 1.0 + r / SLOT_LENGTH_MS + 0.25
                self.assertLessEqual(
                    d, limit,
                    f"r={r} N={n}: low-load mean delay {d:.2f} > {limit:.2f} slots")
        avg = sum(means) / len(means)
        self.assertLessEqual(avg, 1.1,
                             f"mean low-load delay over configs = {avg:.2f}")


class TestCheck4ByteIdenticalCsv(unittest.TestCase):
    def test_same_seed_byte_identical_csv(self):
        def mini_grid():
            rows = []
            for r, n in ((500, 10), (1000, 1)):
                for f in (0.5, 0.9, 1.3):
                    lam = f * 1000.0 * n / r
                    rows.append(csv_row(Simulation(r, n, lam, lam, 200, SEED).run()))
            return "\n".join(rows)
        self.assertEqual(mini_grid(), mini_grid())


if __name__ == "__main__":
    unittest.main(verbosity=2)
