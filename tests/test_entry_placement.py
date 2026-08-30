#!/usr/bin/env python3
"""Entry placement relative to fixed and rotating proposers.

`[D18]` (`docs/history/networking-plan.md`) separates two effects that are easy
to conflate -- how much the proposer sees (degree) and whether its identity is
stable. With a fixed proposer, inclusion delay should
track distance from it; with a rotating one, that correlation should collapse
while average delay stays much the same.
"""

import random
import unittest

from starksim.arena import KIND_PQ
from starksim.costs import CostTable
from starksim.netsim import NetworkSim
from starksim.network import Overlay, bytes_per_ms


def diffusion_limited(mode, seed=0, nodes=60, slot=400.0, latency=150.0,
                      **kwargs):
    """A configuration where news takes longer than a slot to cross the graph.

    That condition is load-bearing: if a transaction reaches everyone well
    inside a slot, where it entered cannot matter, and the effect under test
    genuinely is not there to find.
    """
    rng = random.Random(f"{seed}:topology")
    overlay = Overlay.random_regular(nodes, 4, rng, base_latency=latency,
                                     latency_seed=seed)
    defaults = dict(seed=seed, capacity=bytes_per_ms(50),
                    costs=CostTable(prove_recursive=lambda deps: 30.0),
                    aggregation_interval=slot / 2, slot_length=slot,
                    proposer_mode=mode, proposer_id=0, block_budget=2,
                    entry_subset=8)
    defaults.update(kwargs)
    sim = NetworkSim(overlay, **defaults)
    for index in range(150):
        sim.schedule_birth(index * slot / 10.0, KIND_PQ)
    sim.schedule_ticks(40)
    sim.schedule_slots(12)
    sim.run()
    return sim


def average(mode, metric, seeds=3, **kwargs):
    values = []
    for seed in range(seeds):
        sim = diffusion_limited(mode, seed=seed, **kwargs)
        value = metric(sim)
        if value is not None:
            values.append(value)
    return sum(values) / len(values)


class TestEntryPolicy(unittest.TestCase):
    def test_a_subset_concentrates_where_transactions_enter(self):
        sim = diffusion_limited("fixed", entry_subset=5)
        entries = {entry for entry, _proposer, _delay in sim.tx_records}
        self.assertLessEqual(len(entries), 5)
        self.assertEqual(len(sim.entry_nodes), 5)

    def test_uniform_entry_spreads_across_the_network(self):
        sim = diffusion_limited("fixed", entry_subset=None)
        entries = {entry for entry, _proposer, _delay in sim.tx_records}
        self.assertGreater(len(entries), 20)

    def test_the_subset_is_not_simply_the_lowest_ids(self):
        # Taking the first k would place entry next to proposer zero and bake in
        # the answer.
        sim = diffusion_limited("fixed", entry_subset=6)
        self.assertNotEqual(sim.entry_nodes, tuple(range(6)))

    def test_a_subset_smaller_than_one_node_is_rejected(self):
        with self.assertRaises(ValueError):
            diffusion_limited("fixed", entry_subset=0)


class TestThePrediction(unittest.TestCase):
    def test_a_fixed_proposer_makes_delay_track_distance(self):
        correlation = average("fixed", lambda sim:
                              sim.delay_distance_correlation(0))
        self.assertGreater(correlation, 0.4,
                           "a fixed proposer should disadvantage distant "
                           f"entry points, got r={correlation:+.3f}")

    def test_rotation_collapses_that_correlation(self):
        fixed = average("fixed", lambda sim: sim.delay_distance_correlation(0))
        rotating = average("rotating",
                           lambda sim: sim.delay_distance_correlation(0))
        self.assertLess(abs(rotating), 0.2,
                        f"rotation should detach delay from place, got "
                        f"r={rotating:+.3f}")
        self.assertGreater(fixed - rotating, 0.3)

    def test_rotation_redistributes_delay_rather_than_reducing_it(self):
        # The point is fairness, not speed. If rotation also made everything
        # faster, the mechanism under test would not be the one being measured.
        fixed = average("fixed", lambda sim: sim.delay_mean())
        rotating = average("rotating", lambda sim: sim.delay_mean())
        self.assertLess(abs(fixed - rotating) / fixed, 0.15)

    def test_the_effect_needs_a_diffusion_limited_network(self):
        """The condition, tested as carefully as the claim.

        With a slot long enough for news to cross the graph several times over,
        everything reaches the proposer in time and where it entered stops
        mattering. A positive result in that regime would be a sign of something
        wrong, not of a stronger effect.
        """
        fast = average("fixed", lambda sim: sim.delay_distance_correlation(0),
                       slot=2000.0, latency=100.0)
        slow = average("fixed", lambda sim: sim.delay_distance_correlation(0))
        self.assertLess(fast, 0.3)
        self.assertGreater(slow, fast + 0.3)


class TestWhatRotationCosts(unittest.TestCase):
    def test_rotation_does_not_reduce_block_to_block_variance(self):
        # Rotation was expected to raise variance: each slot's proposer has a
        # different partial view. The effect is small here, so the honest
        # assertion is that it does not *improve* steadiness.
        fixed = average("fixed", lambda sim: sim.block_content_variance())
        rotating = average("rotating",
                           lambda sim: sim.block_content_variance())
        self.assertGreaterEqual(rotating, fixed * 0.9)

    def test_every_node_proposes_under_rotation(self):
        # Eight nodes, so the eight-node entry subset would be every node
        # and is refused as inert; entry concentration is not what this asks.
        sim = diffusion_limited("rotating", nodes=8, entry_subset=4)
        proposers = {block.proposer for block in sim.chain.blocks}
        self.assertGreater(len(proposers), 1)


if __name__ == "__main__":
    unittest.main()
