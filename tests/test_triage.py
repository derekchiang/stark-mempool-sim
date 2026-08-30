#!/usr/bin/env python3
"""Triage policies: what "better" means when the proving budget forces a choice.

Three new policies, each one registered function: `fee_density` selection
(marginal fee per marginal proving cost), `deadline` selection (rescue what is
about to expire), and `fee_value` eviction (a held object's worth is its live
claimed fee, not its transaction count). Each test constructs the exact
situation where the new policy and the old one disagree -- a test both pass is
not a test of the policy.
"""

import unittest

from starksim.arena import Arena, KIND_PQ
from starksim.chain import ChainState, InstantNotifier
from starksim.costs import CostTable
from starksim.netsim import NetworkSim
from starksim.network import Overlay
from starksim.node import Node
from starksim.strategies import get, names

from tests.support import make_node, two_node_overlay


def node_holding(held, notifier=None):
    node = make_node(0, (1,), notifier=notifier)
    for obj in held:
        node.admit(obj, 1.0)
    return node


def per_dep_costs():
    return CostTable(prove_recursive=lambda deps: float(deps))


class TestRegistry(unittest.TestCase):
    def test_the_new_policies_are_registered(self):
        self.assertIn("fee_density", names("selection"))
        self.assertIn("deadline", names("selection"))
        self.assertEqual(names("eviction"), ["fee_value", "usefulness"])

    def test_unknown_eviction_rejected_and_variant_carries_it(self):
        overlay = two_node_overlay(10.0)
        with self.assertRaises(KeyError):
            NetworkSim(overlay, eviction_policy="sentimental")
        # A candidate limit, because choosing an eviction rule for a set that
        # can never fill is refused as inert.
        sim = NetworkSim(overlay, eviction_policy="fee_value",
                         candidate_limit=8)
        self.assertEqual(sim.variant()["eviction_policy"], "fee_value")


class TestDrawnFeesArePaired(unittest.TestCase):
    def test_the_workload_is_a_function_of_the_seed_alone(self):
        # The load-bearing claim of the lognormal fee model: fees come from a
        # dedicated stream consumed only in the birth loop, so two runs with
        # the same seed and different policies face the identical workload --
        # the comparison is paired, and no draw elsewhere can reshuffle it.
        from types import SimpleNamespace
        from bench.triage import one_run
        args = SimpleNamespace(nodes=6, degree=3, uplink_mbps=50.0,
                               latency=10.0, tps=5.0, prove_ms=1.0,
                               candidate_limit=4, window=2000.0, drain=1000.0,
                               fee_model="lognormal", fee_sigma=1.5)
        first = one_run(args, "eip_priority", "usefulness", seed=3)
        second = one_run(args, "fee_density", "fee_value", seed=3)
        self.assertEqual(first["offered_fee"], second["offered_fee"])
        other_seed = one_run(args, "eip_priority", "usefulness", seed=4)
        self.assertNotEqual(first["offered_fee"], other_seed["offered_fee"])


class TestFeeDensity(unittest.TestCase):
    def scene(self):
        # One loose transaction carrying fee 10 at one dependency of cost,
        # against an aggregate adding three fee-1 transactions at three
        # dependencies of cost. Density says the loose transaction (10/dep)
        # beats the aggregate (1/dep); the EIP's tier order says aggregates
        # always come before loose transactions.
        arena = Arena()
        rich = arena.new_leaf(KIND_PQ, 0.0, fee=10.0)
        cheap = [arena.new_leaf(KIND_PQ, 0.0, fee=1.0) for _ in range(3)]
        aggregate = arena.new_aggregate(cheap, 1.0, 1, claimed=0b1110)
        return arena, rich, aggregate

    def test_density_picks_the_dense_transaction_first(self):
        arena, rich, aggregate = self.scene()
        node = node_holding([rich, aggregate])
        selection = get("selection", "fee_density")(
            node, arena, 0, per_dep_costs(), budget=100.0)
        self.assertEqual(selection.inputs[0], rich.id)

    def test_the_eip_order_picks_the_aggregate_first(self):
        # The discriminating counterpart: on the same view the EIP's tier
        # order goes to the aggregate before any loose transaction, which is
        # exactly the behaviour fee_density exists to differ from.
        arena, rich, aggregate = self.scene()
        node = node_holding([rich, aggregate])
        selection = get("selection", "eip_priority")(
            node, arena, 0, per_dep_costs(), budget=100.0)
        self.assertEqual(selection.inputs[0], aggregate.id)

    def test_a_starved_budget_still_selects_a_whole_fold(self):
        # Starved means slow, not stopped -- and not stopped one input short of
        # a fold, which would leave the node proving single objects for ever.
        arena, rich, aggregate = self.scene()
        node = node_holding([rich, aggregate])
        selection = get("selection", "fee_density")(
            node, arena, 0, per_dep_costs(), budget=0.0)
        self.assertEqual(len(selection.inputs), 2)


class TestDeadline(unittest.TestCase):
    def scene(self):
        arena = Arena()
        urgent = arena.new_leaf(KIND_PQ, 0.0, fee=1.0, expiry=1000.0)
        rich = arena.new_leaf(KIND_PQ, 0.0, fee=100.0)
        return arena, urgent, rich

    def test_deadline_rescues_the_expiring_transaction_first(self):
        arena, urgent, rich = self.scene()
        node = node_holding([urgent, rich])
        selection = get("selection", "deadline")(
            node, arena, 0, per_dep_costs(), budget=100.0)
        self.assertEqual(selection.inputs[0], urgent.id)

    def test_fee_orders_pick_the_rich_transaction_first(self):
        arena, urgent, rich = self.scene()
        for strategy in ("eip_priority", "fee_density"):
            node = node_holding([urgent, rich])
            selection = get("selection", strategy)(
                node, arena, 0, per_dep_costs(), budget=100.0)
            self.assertEqual(selection.inputs[0], rich.id, strategy)


class TestFeeValueEviction(unittest.TestCase):
    def scene(self):
        # The wrinkle the policy exists for: a fee-50 loose transaction
        # (usefulness one) against an aggregate claiming five fee-1
        # transactions (usefulness five). Usefulness keeps the aggregate;
        # fee value keeps the transaction.
        arena = Arena()
        rich = arena.new_leaf(KIND_PQ, 0.0, fee=50.0)
        cheap = [arena.new_leaf(KIND_PQ, 0.0, fee=1.0) for _ in range(5)]
        aggregate = arena.new_aggregate(cheap, 1.0, 1, claimed=0b111110)
        return arena, rich, aggregate

    def evict_down_to_one(self, arena, held, policy):
        node = Node(0, (1,), InstantNotifier(ChainState()),
                    candidate_limit=1, eviction=get("eviction", policy))
        for obj in held:
            node.admit(obj, 1.0)
        dropped = node.enforce_candidate_limit(arena)
        return dropped, set(node.view.held)

    def test_usefulness_sacrifices_the_priced_transaction(self):
        arena, rich, aggregate = self.scene()
        dropped, kept = self.evict_down_to_one(arena, [rich, aggregate],
                                               "usefulness")
        self.assertEqual(dropped, [rich.id])
        self.assertEqual(kept, {aggregate.id})

    def test_fee_value_sacrifices_the_mediocre_aggregate(self):
        arena, rich, aggregate = self.scene()
        dropped, kept = self.evict_down_to_one(arena, [rich, aggregate],
                                               "fee_value")
        self.assertEqual(dropped, [aggregate.id])
        self.assertEqual(kept, {rich.id})


if __name__ == "__main__":
    unittest.main()
