#!/usr/bin/env python3
"""Settings a run cannot act on are refused, loudly, before it starts.

Every measurement fault this project has recorded has the same shape: a
configuration that looks like it is asking a question and is not. A policy field
that cannot affect the run is that fault in miniature, so it is a construction
error rather than a silent default.

The rule is deliberately narrow in two directions, and both are tested here. It
refuses a *chosen* value, never a default left alone -- otherwise every run would
have to disclaim the fields it does not use. And it refuses "cannot affect",
never "might surprise": bounding a candidate set without expecting eviction is a
legitimate experiment, while a request timeout under a policy that never
requests anything cannot do anything at all.
"""

import inspect
import unittest

from starksim.netsim import INERT_SETTINGS, NetworkSim, _field_default

from tests.support import two_node_overlay


class TestInertSettingsAreRefused(unittest.TestCase):
    def overlay(self):
        return two_node_overlay(10.0)

    def test_an_eviction_rule_without_a_bounded_set(self):
        with self.assertRaises(ValueError) as caught:
            NetworkSim(self.overlay(), eviction_policy="fee_value")
        self.assertIn("candidate_limit", str(caught.exception))

    def test_a_request_timeout_without_requests(self):
        for policy in ("flood", "wrapper_only"):
            with self.subTest(relay_policy=policy):
                with self.assertRaises(ValueError):
                    NetworkSim(self.overlay(), relay_policy=policy,
                               request_timeout=500.0)

    def test_an_overrun_rule_without_a_clock(self):
        with self.assertRaises(ValueError):
            NetworkSim(self.overlay(), aggregation_trigger="when_ready",
                       proving_budget=1000.0, on_tick_overrun="abandon")

    def test_a_budget_with_no_interval_to_default_to(self):
        # Not an inert setting but the same principle: rather than invent a
        # budget, which would decide how wide every claim gets, say so.
        with self.assertRaises(ValueError) as caught:
            NetworkSim(self.overlay(), aggregation_trigger="when_ready")
        self.assertIn("proving_budget", str(caught.exception))

    def test_each_refusal_names_the_field_and_the_reason(self):
        with self.assertRaises(ValueError) as caught:
            NetworkSim(self.overlay(), eviction_policy="fee_value")
        message = str(caught.exception)
        self.assertIn("eviction_policy", message)
        self.assertIn("cannot affect this run", message)
        # A refusal that does not say what to do instead is an obstacle.
        self.assertIn("Remove it, or configure the run", message)


class TestTheRuleStaysNarrow(unittest.TestCase):
    def test_the_pairing_that_makes_each_setting_live_is_accepted(self):
        overlay = two_node_overlay(10.0)
        self.assertTrue(NetworkSim(overlay, eviction_policy="fee_value",
                                   candidate_limit=8))
        self.assertTrue(NetworkSim(overlay, relay_policy="announce_fetch",
                                   request_timeout=500.0))
        # One of two nodes: a subset that is genuinely smaller than the network.
        self.assertTrue(NetworkSim(overlay, entry_subset=1))

    def test_defaults_left_alone_are_never_refused(self):
        # Including the configurations in which those defaults do nothing.
        self.assertTrue(NetworkSim(two_node_overlay(10.0)))

    def test_a_default_passed_explicitly_is_indistinguishable_from_silence(self):
        # The documented limit of the rule: it compares against the signature
        # default, so it cannot see a deliberate restatement of one.
        self.assertTrue(NetworkSim(two_node_overlay(10.0),
                                   eviction_policy=_field_default(
                                       "eviction_policy")))

    def test_a_bound_without_a_chosen_rule_is_allowed(self):
        # "Cannot affect", not "might surprise": bounding the set without
        # naming an eviction rule is an experiment, not an error.
        self.assertTrue(NetworkSim(two_node_overlay(10.0), candidate_limit=4))


class TestTheTableIsWellFormed(unittest.TestCase):
    def test_every_policed_field_exists_and_has_a_default(self):
        signature = inspect.signature(NetworkSim.__init__)
        for field, _predicate, why in INERT_SETTINGS:
            with self.subTest(field=field):
                self.assertIn(field, signature.parameters)
                # A default of None is legitimate -- `request_timeout=None`
                # means "never retry" -- so the check is that one exists.
                self.assertIsNot(signature.parameters[field].default,
                                 inspect.Parameter.empty)
                self.assertTrue(why, "a refusal must carry its reason")

    def test_the_defaults_are_read_from_the_signature(self):
        # Not restated, so a changed default cannot leave the rule behind.
        self.assertEqual(_field_default("relay_policy"), "flood")
        self.assertEqual(_field_default("eviction_policy"), "usefulness")


if __name__ == "__main__":
    unittest.main()


class TestSettingsThatCannotBindEither(unittest.TestCase):
    """Cases review 13 found still accepted while doing nothing."""

    def test_a_subset_the_size_of_the_network(self):
        # entry_subset=N on N nodes resolves to the same entry set as None,
        # and produced an identical variant, so a run could claim to
        # concentrate entry while concentrating nothing.
        with self.assertRaises(ValueError):
            NetworkSim(two_node_overlay(10.0), entry_subset=2)

    def test_an_interval_without_a_clock(self):
        with self.assertRaises(ValueError):
            NetworkSim(two_node_overlay(10.0),
                       aggregation_trigger="when_ready",
                       proving_budget=1000.0, aggregation_interval=500.0)

    def test_the_same_settings_where_they_do_bind(self):
        overlay = two_node_overlay(10.0)
        self.assertTrue(NetworkSim(overlay, entry_subset=1))
        self.assertTrue(NetworkSim(overlay, aggregation_interval=500.0))
        # proposer_id is never inert: the round-robin's start under
        # rotation, and the reference the distance correlations measure from
        # under every mode.
        self.assertTrue(NetworkSim(overlay, proposer_mode="rotating",
                                   proposer_id=1))
        self.assertTrue(NetworkSim(overlay, proposer_mode="random",
                                   proposer_id=1))

    def test_a_follow_rate_beside_an_explicit_follow_list(self):
        with self.assertRaises(ValueError):
            NetworkSim(two_node_overlay(10.0), trace_follow=[0],
                       trace_follow_rate=0.5)

    def test_a_follow_rate_on_its_own_is_fine(self):
        self.assertTrue(NetworkSim(two_node_overlay(10.0),
                                   trace_follow_rate=0.5))
