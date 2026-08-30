#!/usr/bin/env python3
"""The named presets say what they claim to say.

A preset is a claim about a configuration -- "this is the EIP", "this is v1",
"this is every lever that pays" -- and each claim is held to something other
than itself: the constructor's defaults, the settings v1's calibration was
written with, and the levers bench's own table.
"""

import inspect
import unittest

from bench.levers import LEVERS
from starksim.netsim import NetworkSim
from starksim.presets import (EIP_FAITHFUL, HIGH_THROUGHPUT, PRESETS,
                              V1_EQUIVALENT, names, preset)

from tests.support import two_node_overlay


class TestThePresets(unittest.TestCase):
    def test_the_eip_preset_is_the_constructor_default(self):
        defaults = inspect.signature(NetworkSim.__init__).parameters
        for field, value in EIP_FAITHFUL.items():
            with self.subTest(field=field):
                self.assertEqual(defaults[field].default, value)

    def test_the_eip_preset_covers_every_documented_policy_choice(self):
        from tests.test_eip_faithful import DOCUMENT, faithful_rows
        documented = faithful_rows(DOCUMENT.read_text())
        self.assertEqual(set(documented), set(EIP_FAITHFUL))

    # The settings bench.v1_calibration was written with, spelled out here
    # rather than read from the preset: the bench now builds from the preset,
    # so comparing the two would compare a thing with itself.
    V1_AS_CALIBRATED = {
        "capacity": None, "link_capacity": None, "egress_queue_bytes": None,
        "prove_cost_basis": "deps", "proving_budget": 1e9,
        "relay_policy": "flood", "chain_notify": "instant",
        "proposer_mode": "dedicated", "proposer_id": 0,
        "inclusion_policy": "claim_only", "selection_strategy": "random_pair",
        "fold_plan": "wide", "aggregation_trigger": "when_ready",
    }

    def test_v1_is_the_calibration_as_it_was_written(self):
        self.assertEqual(V1_EQUIVALENT, self.V1_AS_CALIBRATED)

    def test_the_calibration_bench_builds_v1(self):
        import argparse
        from bench.v1_calibration import v1_like
        args = argparse.Namespace(aggregators=3, latency=100.0, fold_ms=200.0,
                                  fold_jitter=0.1, seed=1, slot_length=1000.0,
                                  block_budget=5)
        sim = v1_like(args, "keep_all")
        variant = sim.variant()
        for field, value in self.V1_AS_CALIBRATED.items():
            if field in variant:
                with self.subTest(field=field):
                    # The variant writes an absent bound as -1, its sentinel
                    # for "no bound", never as a null.
                    self.assertEqual(variant[field], -1 if value is None else value)
        self.assertIsNone(sim.capacity)
        self.assertIsNone(sim.link_capacity)

    def test_high_throughput_is_every_lever_but_the_capped_plan(self):
        # `capped8` is the lever the throughput measurement excludes: it caps
        # inclusion through the proving budget once the rate is high. The
        # preset is the levers table minus exactly that one, so a lever
        # added to the table has to be argued into or out of the preset.
        everything = {}
        for name, settings in LEVERS.items():
            if name != "capped8":
                everything.update(settings)
        self.assertEqual(HIGH_THROUGHPUT, everything)
        self.assertNotIn("fold_plan", HIGH_THROUGHPUT)

    def test_every_preset_builds_a_simulation(self):
        for name in names():
            with self.subTest(preset=name):
                NetworkSim(two_node_overlay(), **preset(name))

    def test_a_preset_is_a_copy(self):
        first = preset("eip_faithful")
        first["relay_policy"] = "sideways"
        self.assertEqual(PRESETS["eip_faithful"]["relay_policy"], "flood")

    def test_an_unknown_name_names_the_known_ones(self):
        with self.assertRaises(KeyError) as caught:
            preset("fastest")
        for name in names():
            self.assertIn(name, str(caught.exception))
