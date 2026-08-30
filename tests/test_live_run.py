#!/usr/bin/env python3
"""The live runner: chunked scheduling that keeps the grids aligned, and a
sealed trace whichever way the run ends.

The property worth testing is not the pacing -- sleeping is the wall clock's
business -- but what chunking must not do: scheduling one chunk at a time must
produce exactly the slot grid, birth series and tick stream that scheduling
everything up front would have, with no seam at the chunk boundaries.
"""

import argparse
import io
import random
import unittest

from tests.support import parse_jsonl

from starksim.arena import KIND_PQ
from starksim.costs import CostTable
from starksim.netsim import NetworkSim
from starksim.network import Overlay, SizeModel, bytes_per_ms
from tools.live_run import (LiveSchedule, default_egress_bytes,
                            resolved_settings, run_live)


def live_sim(buffer, **kwargs):
    rng = random.Random("live-test")
    overlay = Overlay.random_regular(10, 4, rng, base_latency=20.0)
    defaults = dict(seed=1, trace=buffer, capacity=bytes_per_ms(50),
                    costs=CostTable(verify_recv=2.0,
                                    prove_recursive=lambda deps: 20.0),
                    proposer_mode="rotating")
    defaults.update(kwargs)
    return NetworkSim(overlay, **defaults)


def events_of(buffer):
    return parse_jsonl(buffer.getvalue())


class TestChunkedScheduling(unittest.TestCase):
    def test_the_grids_survive_the_chunk_boundary(self):
        # Two chunks of four slots each: every seam-sensitive series must look
        # exactly as if it had been scheduled in one call.
        buffer = io.StringIO()
        sim = live_sim(buffer)
        schedule = LiveSchedule(sim, spacing=500.0, lifetime=None,
                                chunk_slots=4)
        status = run_live(sim, schedule, speed=1e9, duration_ms=32_000.0,
                          out=io.StringIO())
        self.assertEqual(status, "complete")
        events = events_of(buffer)
        # The slot grid: every block on a multiple of the slot length, none
        # repeated, none skipped up to the last one built.
        block_times = [event["t"] for event in events if event["e"] == "blk"]
        for when in block_times:
            self.assertEqual(when % sim.slot_length, 0.0)
        self.assertEqual(sorted(set(block_times)), sorted(block_times))
        self.assertGreaterEqual(len(block_times), 7)
        # Births continue across the boundary at the same spacing. 65, not 64:
        # a birth landing exactly on a horizon belongs to the chunk that ends
        # there, and the final horizon's birth is one of them.
        birth_times = [event["t"] for event in events if event["e"] == "arr"]
        self.assertEqual(len(birth_times), 65)
        self.assertEqual(birth_times, sorted(birth_times))
        self.assertIn(16_000.0, birth_times)
        # The boundary birth must win the same-timestamp race against the
        # boundary block, exactly as it does under up-front scheduling --
        # scheduled in the later chunk it lost it, and the horizon
        # transaction could never make the block it precedes. Line order in
        # the trace is event-processing order.
        at_boundary = [event["e"] for event in events
                       if event.get("t") == 16_000.0
                       and event["e"] in ("arr", "blk")]
        self.assertEqual(at_boundary[0], "arr")
        self.assertIn("blk", at_boundary)
        # Ticks fire in the second chunk too, not only the first.
        self.assertGreater(sim.ticks_run, sim.overlay.node_count * 4 - 10)
        # And the trace is terminal.
        self.assertEqual(events[-1]["e"], "end")
        self.assertEqual(events[-1]["status"], "complete")

    def test_a_clockless_trigger_schedules_slots_and_no_ticks(self):
        # when_ready has no ticks to schedule and the model refuses them, so
        # the schedule must leave them out -- the v1 preset runs live this way.
        buffer = io.StringIO()
        sim = live_sim(buffer, aggregation_trigger="when_ready",
                       proving_budget=1e9)
        schedule = LiveSchedule(sim, spacing=500.0, lifetime=None,
                                chunk_slots=1)
        schedule.extend()
        sim.run(until=schedule.horizon)
        schedule.extend()
        sim.run(until=schedule.horizon)
        self.assertGreater(sim.slots_run, 0)
        self.assertGreater(sim.arrived_txs, 0)

    def test_a_chunk_must_hold_whole_aggregation_intervals(self):
        sim = live_sim(io.StringIO(), slot_length=4000.0,
                       aggregation_interval=3000.0)
        with self.assertRaises(ValueError):
            LiveSchedule(sim, spacing=500.0, lifetime=None, chunk_slots=1)


class TestSealing(unittest.TestCase):
    def test_an_interrupt_seals_the_trace_as_cancelled(self):
        buffer = io.StringIO()
        sim = live_sim(buffer)
        schedule = LiveSchedule(sim, spacing=500.0, lifetime=None,
                                chunk_slots=4)

        # A clock that lets a little simulation happen and then interrupts,
        # standing in for Ctrl-C mid-run.
        state = {"calls": 0}

        def interrupting_clock():
            state["calls"] += 1
            if state["calls"] > 6:
                raise KeyboardInterrupt
            return state["calls"] * 1.0

        status = run_live(sim, schedule, speed=1.0, duration_ms=None,
                          out=io.StringIO(), clock=interrupting_clock,
                          sleep=lambda seconds: None)
        self.assertEqual(status, "cancelled")
        events = events_of(buffer)
        self.assertEqual(events[-1]["e"], "end")
        self.assertEqual(events[-1]["status"], "cancelled")
        # Sealed means sealed: nothing may run afterwards.
        with self.assertRaises(ValueError):
            sim.run(until=sim.engine.now + 1.0)



class TestDerivedEgressBuffer(unittest.TestCase):
    """The send buffer must hold at least one wrapper emission round.

    A wrapper puts one proof-sized copy in the queue per peer, so a tick's burst
    is `degree x recursive_proof` whatever the load. A bound below that is not a
    congestion bound: every node overflows the moment it aggregates, and the
    drops describe the buffer rather than the network. The runner's old fixed
    2 MB default was below one round from degree 8 upward.
    """

    def test_the_default_holds_two_rounds_at_any_degree(self):
        sizes = SizeModel()
        for degree in (4, 6, 8, 16, 32):
            with self.subTest(degree=degree):
                derived = default_egress_bytes(degree, sizes)
                one_round = degree * sizes.recursive_proof
                self.assertGreaterEqual(derived, one_round)
                self.assertEqual(derived, 2 * one_round)

    def test_the_old_fixed_default_was_below_one_round(self):
        # The regression this derivation exists for, stated as arithmetic.
        sizes = SizeModel()
        self.assertLess(2_000_000, 8 * sizes.recursive_proof)
        self.assertGreater(default_egress_bytes(8, sizes),
                           8 * sizes.recursive_proof)

    def test_a_buffer_below_one_round_drops_an_unloaded_network(self):
        sizes = SizeModel()
        one_round = 8 * sizes.recursive_proof
        outcomes = {}
        for label, bound in (("half a round", one_round // 2),
                             ("derived default",
                              default_egress_bytes(8, sizes))):
            rng = random.Random("egress-round")
            overlay = Overlay.random_regular(12, 8, rng, base_latency=20.0)
            sim = NetworkSim(overlay, seed=1, size_model=sizes,
                             capacity=bytes_per_ms(1000),
                             costs=CostTable(verify_recv=2.0,
                                             prove_recursive=lambda deps: 20.0),
                             egress_queue_bytes=bound)
            # One transaction: nothing that could congest a 1 Gbit/s uplink.
            sim.schedule_birth(0.0, KIND_PQ, node_id=0)
            sim.schedule_ticks(4)
            sim.run(until=8000.0)
            outcomes[label] = sim.dropped_messages
        self.assertGreater(outcomes["half a round"], 0)
        self.assertEqual(outcomes["derived default"], 0)


class TestResolvedSettings(unittest.TestCase):
    """A flag given on the command line wins; one left out takes the preset's
    value or the runner's default -- capacity and the egress bound included,
    which `v1_equivalent` unbounds. The header notes come from the settings
    that win, so the header cannot describe a bound the simulation does not
    enforce.
    """

    def resolve(self, **overrides):
        given = dict(seed=7, uplink_mbps=None, egress_bytes=None,
                     candidate_limit=8, degree=6, follow_rate=0.15,
                     counter_interval=500.0, preset=None,
                     relay_policy=None, wrapper_economy=None,
                     supersede_policy=None)
        given.update(overrides)
        return resolved_settings(argparse.Namespace(**given), SizeModel())

    def test_left_out_takes_the_runner_default(self):
        settings, uplink, egress = self.resolve()
        self.assertEqual(settings["capacity"], bytes_per_ms(25.0))
        self.assertEqual(uplink, "25 Mbit/s")
        self.assertIn("(2 rounds)", egress)

    def test_the_preset_unbinds_what_the_runner_bounds(self):
        settings, uplink, egress = self.resolve(preset="v1_equivalent")
        self.assertIsNone(settings["capacity"])
        self.assertIsNone(settings["egress_queue_bytes"])
        self.assertEqual((uplink, egress), ("unbounded", "unbounded"))

    def test_an_explicit_bound_beats_the_preset(self):
        settings, uplink, egress = self.resolve(preset="v1_equivalent",
                                                uplink_mbps=10.0,
                                                egress_bytes=123)
        self.assertEqual(settings["capacity"], bytes_per_ms(10.0))
        self.assertEqual(settings["egress_queue_bytes"], 123)
        self.assertEqual(uplink, "10 Mbit/s")
        self.assertEqual(egress, "0.0 MB")

    def test_an_explicit_zero_unbinds_the_egress_buffer(self):
        settings, _, egress = self.resolve(egress_bytes=0)
        self.assertIsNone(settings["egress_queue_bytes"])
        self.assertEqual(egress, "unbounded")


class TestCounterInterval(unittest.TestCase):
    """Counter records are per node per sample, so the interval is the knob that
    decides how fast a trace grows with node count."""

    def test_the_interval_sets_the_counter_grid(self):
        for interval in (500.0, 2000.0):
            with self.subTest(interval=interval):
                buffer = io.StringIO()
                sim = live_sim(buffer, trace_counter_interval=interval)
                sim.schedule_birth(0.0, KIND_PQ, node_id=0)
                sim.schedule_ticks(4)
                sim.run(until=4000.0)
                grid = sorted({event["t"] for event in events_of(buffer)
                               if event["e"] == "cnt"})
                on_grid = [t for t in grid if t % interval == 0]
                self.assertEqual(grid, on_grid)
                self.assertGreater(len(grid), 1)

    def test_a_coarser_interval_emits_fewer_counters(self):
        counts = {}
        for interval in (500.0, 2000.0):
            buffer = io.StringIO()
            sim = live_sim(buffer, trace_counter_interval=interval)
            sim.schedule_birth(0.0, KIND_PQ, node_id=0)
            sim.schedule_ticks(4)
            sim.run(until=4000.0)
            counts[interval] = sum(1 for event in events_of(buffer)
                                   if event["e"] == "cnt")
        self.assertLess(counts[2000.0], counts[500.0])


if __name__ == "__main__":
    unittest.main()
