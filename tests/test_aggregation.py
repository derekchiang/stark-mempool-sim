#!/usr/bin/env python3
"""Steps 5 and 6: what a node selects to prove, and how the proving is shaped.

Both stages are pure -- no engine, no calendar -- which is what lets a queued
plan be abandoned and re-decided rather than being a closure already committed
to its inputs.
"""

import random
import unittest

from starksim import strategies
from starksim.aggregation import (AggregationPlan, Selection, coverage_greedy,
                                  eip_priority, estimate_cost)
from starksim.arena import Arena, KIND_PQ, KIND_PRIVACY
from starksim.chain import ChainState, InstantNotifier
from starksim.costs import PROVE_RECURSIVE, CostTable
from starksim.netsim import NetworkSim
from starksim.network import Overlay, bytes_per_ms
from starksim.node import Node
from starksim.strategies import get, names, register

from tests.support import two_node_overlay


class Fixture:
    """A node holding whatever the test needs it to hold."""

    def __init__(self, costs=None):
        self.arena = Arena()
        self.chain = ChainState()
        self.notifier = InstantNotifier(self.chain)
        self.node = Node(0, (1, 2), self.notifier)
        self.costs = costs if costs is not None else CostTable(
            prove_recursive=lambda deps: float(deps))

    def leaf(self, fee=0.0, kind=KIND_PQ):
        obj = self.arena.new_leaf(kind, 0.0, fee=fee)
        self.node.admit(obj, 0.0)
        return obj

    def aggregate(self, children, creator=1):
        obj = self.arena.new_aggregate(children, 1.0, creator)
        self.node.admit(obj, 1.0)
        return obj

    def select(self, strategy="eip_priority", budget=1e9, previous=None,
               tick=1):
        return get("selection", strategy)(
            self.node, self.arena, tick, self.costs, budget, previous)


class TestRegistry(unittest.TestCase):
    def test_strategies_are_named_not_branched(self):
        # A branch cannot be enumerated, recorded in a variant, or swept.
        self.assertIn("eip_priority", names("selection"))
        self.assertIn("coverage_greedy", names("selection"))
        self.assertEqual(names("fold_plan"), ["binary", "capped8", "wide"])

    def test_unknown_strategy_names_what_it_knows(self):
        with self.assertRaises(KeyError) as caught:
            get("fold_plan", "spiral")
        self.assertIn("binary", str(caught.exception))


class TestEipPriority(unittest.TestCase):
    def test_the_previous_aggregate_comes_first(self):
        fixture = Fixture()
        leaves = [fixture.leaf() for _ in range(3)]
        previous = fixture.arena.new_aggregate(leaves[:2], 1.0, 0)
        selection = fixture.select(previous=previous)
        self.assertEqual(selection.inputs[0], previous.id)

    def test_peer_aggregates_are_taken_by_what_they_add(self):
        # Descending order of *new* transactions, not of size.
        fixture = Fixture()
        leaves = [fixture.leaf() for _ in range(6)]
        overlapping = fixture.aggregate(leaves[:4])       # 4 txs
        disjoint = fixture.aggregate(leaves[4:])          # 2 txs, all new
        selection = fixture.select()
        # The bigger one goes first, then the other adds its two.
        self.assertEqual(selection.inputs[0], overlapping.id)
        self.assertIn(disjoint.id, selection.inputs)
        self.assertEqual(selection.claimed.bit_count(), 6)

    def test_an_aggregate_adding_nothing_is_skipped(self):
        fixture = Fixture()
        leaves = [fixture.leaf() for _ in range(4)]
        big = fixture.aggregate(leaves)
        contained = fixture.aggregate(leaves[:2])
        selection = fixture.select()
        self.assertIn(big.id, selection.inputs)
        self.assertNotIn(contained.id, selection.inputs)

    def test_loose_transactions_go_by_fee(self):
        fixture = Fixture()
        cheap = fixture.leaf(fee=1.0)
        dear = fixture.leaf(fee=9.0)
        middling = fixture.leaf(fee=5.0)
        selection = fixture.select()
        self.assertEqual(list(selection.inputs),
                         [dear.id, middling.id, cheap.id])

    def test_equal_fees_break_by_id_so_the_order_is_reproducible(self):
        fixture = Fixture()
        first = fixture.leaf(fee=1.0)
        second = fixture.leaf(fee=1.0)
        self.assertEqual(list(fixture.select().inputs), [first.id, second.id])

    def test_dead_dependencies_are_dropped_from_what_is_claimed(self):
        # The EIP's discards input: a new proof over an old aggregate need not
        # keep re-proving transactions that have since been included, and
        # dropping them is what its "removals" priority amounts to here.
        fixture = Fixture()
        leaves = [fixture.leaf() for _ in range(4)]
        aggregate = fixture.aggregate(leaves)
        fixture.chain.include(fixture.arena.coverage(leaves[0]), 1)
        selection = fixture.select()
        self.assertEqual(selection.claim_union.bit_count(), 4)  # what it folds
        self.assertEqual(selection.claimed.bit_count(), 3)    # what it claims
        self.assertEqual(selection.dep_count(), 3)

    def test_a_wholly_dead_candidate_is_not_selected(self):
        fixture = Fixture()
        leaf = fixture.leaf()
        fixture.chain.include(fixture.arena.coverage(leaf), 1)
        self.assertTrue(fixture.select().is_empty())

    def test_the_budget_stops_selection(self):
        # Cost grows with dependencies, so a budget caps how much can be taken.
        fixture = Fixture(CostTable(prove_recursive=lambda deps: float(deps)))
        for _ in range(20):
            fixture.leaf(fee=1.0)
        selection = fixture.select(budget=5.0)
        self.assertLessEqual(selection.dep_count(), 5)
        self.assertLessEqual(selection.estimated_cost, 5.0)

    def test_a_whole_fold_is_taken_even_if_it_busts_the_budget(self):
        # A node whose cheapest fold does not fit its interval must still make
        # progress. The floor is a *fold*, not an input: stopping at one leaves
        # a node proving single objects for ever -- never stalling, never
        # aggregating, and reporting a proving cost while measuring a budget
        # ceiling. So it takes two and overruns, and the overrun shows up in
        # the queue.
        fixture = Fixture(CostTable(prove_recursive=lambda deps: 1000.0))
        fixture.leaf(fee=1.0)
        fixture.leaf(fee=2.0)
        selection = fixture.select(budget=1.0)
        self.assertEqual(len(selection.inputs), 2)
        self.assertGreater(selection.estimated_cost, 1.0)

    def test_the_floor_cannot_invent_inputs_a_node_does_not_hold(self):
        # A floor on what the budget may refuse, not on what must exist.
        fixture = Fixture(CostTable(prove_recursive=lambda deps: 1000.0))
        fixture.leaf(fee=1.0)
        selection = fixture.select(budget=1.0)
        self.assertEqual(len(selection.inputs), 1)

    def test_past_the_floor_the_budget_binds_again(self):
        # Two inputs regardless; the third is the budget's to refuse.
        fixture = Fixture(CostTable(prove_recursive=lambda deps: 1000.0))
        for _ in range(6):
            fixture.leaf(fee=1.0)
        selection = fixture.select(budget=1.0)
        self.assertEqual(len(selection.inputs), 2)

    def test_the_selection_records_what_it_was_decided_against(self):
        # A plan queued behind other work has to be recognisable as stale.
        fixture = Fixture()
        fixture.leaf()
        first = fixture.select(tick=7)
        self.assertEqual(first.tick, 7)
        self.assertEqual(first.revision, 0)
        fixture.chain.expire(1)
        self.assertEqual(fixture.select(tick=8).revision, 1)

    def test_a_selection_is_immutable(self):
        fixture = Fixture()
        fixture.leaf()
        selection = fixture.select()
        with self.assertRaises(Exception):
            selection.inputs = ()


class TestCoverageGreedyBaseline(unittest.TestCase):
    """The no-priority baseline, kept so the EIP's ordering can be measured
    against something rather than assumed to help."""

    def _ordering(self, strategy, fees):
        fixture = Fixture()
        for fee in fees:
            fixture.leaf(fee=fee)
        return list(fixture.select(strategy=strategy).inputs)

    def test_greedy_output_does_not_depend_on_fees(self):
        # Whatever adds the most transactions goes next, and a loose transaction
        # adds exactly one whatever it pays.
        flat = self._ordering("coverage_greedy", [0.0, 0.0, 0.0])
        varied = self._ordering("coverage_greedy", [1.0, 100.0, 5.0])
        self.assertEqual(flat, varied)

    def test_eip_output_does_depend_on_fees(self):
        flat = self._ordering("eip_priority", [1.0, 2.0, 3.0])
        reversed_fees = self._ordering("eip_priority", [3.0, 2.0, 1.0])
        self.assertNotEqual(flat, reversed_fees)

    def test_the_eip_order_puts_every_aggregate_before_any_loose_tx(self):
        # Tier 3 before tier 4, even when the loose transaction pays far more
        # and the aggregate adds no more than it does.
        fixture = Fixture()
        leaves = [fixture.leaf(fee=0.0) for _ in range(3)]
        aggregate = fixture.aggregate(leaves[:2])
        dear = fixture.leaf(fee=1000.0)
        selection = fixture.select()
        self.assertEqual(selection.inputs[0], aggregate.id)
        self.assertIn(dear.id, selection.inputs)
        self.assertGreater(selection.inputs.index(dear.id),
                           selection.inputs.index(aggregate.id))


class TestPlans(unittest.TestCase):
    def _selection(self, count):
        # One input per bit, so each job's output mask is checkable.
        return Selection(tuple(range(count)),
                         tuple(1 << index for index in range(count)),
                         (1 << count) - 1, (1 << count) - 1, 1, 0, 1.0)

    def test_wide_is_one_fold(self):
        plan = get("fold_plan", "wide")(self._selection(9))
        self.assertEqual(plan.job_count(), 1)
        self.assertEqual(plan.jobs[0].inputs, tuple(range(9)))
        self.assertEqual(plan.root, 0)
        self.assertEqual(plan.jobs[0].dep_count, 9)

    def test_each_job_is_priced_by_what_it_proves(self):
        # Charging every fold the whole selection's dependency count made all
        # plan shapes cost the same, which is exactly the comparison the plans
        # exist to support.
        plan = get("fold_plan", "binary")(self._selection(8))
        self.assertEqual([job.dep_count for job in plan.jobs],
                         [2, 2, 2, 2, 4, 4, 8])
        self.assertEqual(plan.jobs[plan.root].dep_count, 8)
        # A job's output is exactly the union of what fed it.
        for job in plan.jobs:
            expected = 0
            for object_id in job.inputs:
                expected |= 1 << object_id
            for produced in job.depends_on:
                expected |= plan.jobs[produced].output_mask
            self.assertEqual(job.output_mask, expected)

    def test_a_fan_in_below_two_is_refused(self):
        from starksim.aggregation import _capped_plan
        with self.assertRaises(ValueError):
            _capped_plan(self._selection(4), 1)

    def test_binary_costs_k_minus_one_proofs(self):
        # Same coverage as one wide fold, k-1 proofs instead of one, and the root
        # arrives only when the last finishes. That difference is why both exist.
        for count in (2, 3, 8, 9, 17):
            with self.subTest(count=count):
                plan = get("fold_plan", "binary")(self._selection(count))
                self.assertEqual(plan.job_count(), count - 1)
                self.assertEqual(plan.root, plan.job_count() - 1)

    def test_capped_sits_between_the_two(self):
        selection = self._selection(64)
        wide = get("fold_plan", "wide")(selection)
        capped = get("fold_plan", "capped8")(selection)
        binary = get("fold_plan", "binary")(selection)
        self.assertLess(wide.job_count(), capped.job_count())
        self.assertLess(capped.job_count(), binary.job_count())

    def test_every_input_appears_exactly_once(self):
        for plan_name in ("wide", "binary", "capped8"):
            for count in (1, 2, 5, 9, 33):
                with self.subTest(plan=plan_name, count=count):
                    plan = get("fold_plan", plan_name)(self._selection(count))
                    seen = []
                    for job in plan.jobs:
                        seen.extend(job.inputs)
                    self.assertEqual(sorted(seen), list(range(count)))

    def test_every_job_but_the_root_feeds_exactly_one_other(self):
        for plan_name in ("binary", "capped8"):
            for count in (2, 5, 9, 33):
                with self.subTest(plan=plan_name, count=count):
                    plan = get("fold_plan", plan_name)(self._selection(count))
                    consumers = {}
                    for job in plan.jobs:
                        for produced in job.depends_on:
                            consumers[produced] = consumers.get(produced, 0) + 1
                    for job in plan.jobs:
                        if job.index == plan.root:
                            self.assertNotIn(job.index, consumers)
                        else:
                            self.assertEqual(consumers.get(job.index), 1,
                                             f"job {job.index} of {plan_name}")

    def test_dependencies_point_backwards_so_order_is_a_valid_schedule(self):
        plan = get("fold_plan", "binary")(self._selection(17))
        for job in plan.jobs:
            for produced in job.depends_on:
                self.assertLess(produced, job.index)

    def test_a_single_input_is_one_reproof(self):
        for plan_name in ("wide", "binary", "capped8"):
            plan = get("fold_plan", plan_name)(self._selection(1))
            self.assertEqual(plan.job_count(), 1)
            self.assertEqual(plan.jobs[0].inputs, (0,))

    def test_an_empty_selection_plans_nothing(self):
        empty = Selection((), (), 0, 0, 1, 0, 0.0)
        for plan_name in ("wide", "binary", "capped8"):
            plan = get("fold_plan", plan_name)(empty)
            if plan.jobs:
                self.assertEqual(plan.jobs[0].inputs, ())
            else:
                self.assertEqual(plan.root, -1)


if __name__ == "__main__":
    unittest.main()


class TestAFoldNeedsTwoInputs(unittest.TestCase):
    """A fold combines two objects or more. The paths that would spend a CPU on
    fewer are a re-proof of something already proven and a plan over objects
    that have left the arena; both must reach the prover as nothing at all. The
    exception is a lone leaf, whose single proving job is what gives a bare
    dependency its first recursive proof."""

    def test_a_selection_of_what_a_node_does_not_hold_is_refused(self):
        # The stronger form of what this used to check. A plan over objects
        # that have left the arena spends no CPU -- but it is no longer
        # reachable through a strategy, because choosing something the node
        # does not hold is refused before planning, and a held id whose object
        # has departed is pruned at the top of the tick. The abandoned-plan
        # path remains as a defence, not as a behaviour a policy can trigger.
        overlay = two_node_overlay(0.0)
        sim = NetworkSim(overlay, seed=1, aggregation_trigger="aligned",
                         costs=CostTable(prove_recursive=100.0))
        node = sim.nodes[0]
        selection = Selection(inputs=(999,), input_claims=(1,), claim_union=1,
                              claimed=1, tick=0,
                              revision=node.notifier.generation(node.id),
                              estimated_cost=0.0)
        try:
            register("selection", "stale_only")(lambda *a, **k: selection)
            sim.selection_strategy = "stale_only"
            before = sim.cpu[0].served
            with self.assertRaises(ValueError) as caught:
                sim._tick(0, 0)
            self.assertIn("does not hold", str(caught.exception))
            self.assertEqual(sim.cpu[0].served, before)
            self.assertEqual(sim.proofs_completed, 0)
        finally:
            sim.selection_strategy = "eip_priority"

    def test_a_lone_aggregate_is_wrapped_and_not_reproved(self):
        overlay = two_node_overlay(0.0)
        sim = NetworkSim(overlay, seed=1, aggregation_trigger="aligned",
                         costs=CostTable(prove_recursive=100.0))
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_birth(1.0, KIND_PQ, node_id=0)
        sim.schedule_ticks(3)
        sim.run(until=4000.0)
        # Two leaves fold once; later ticks re-wrap that aggregate without
        # spending another proof on the same coverage.
        self.assertGreater(sim.wrappers_emitted, sim.proofs_completed)

    def test_a_lone_leaf_still_gets_its_one_proof(self):
        overlay = two_node_overlay(0.0)
        sim = NetworkSim(overlay, seed=1, aggregation_trigger="aligned",
                         costs=CostTable(prove_recursive=100.0))
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.schedule_ticks(1)
        sim.run(until=2000.0)
        # Both nodes hold the flooded leaf, and each proves it once: a bare
        # signature has no recursive proof until someone spends one on it.
        for node_id in (0, 1):
            self.assertEqual(
                sim.cpu[node_id].served_by_kind.get(PROVE_RECURSIVE, 0), 1)


class TestFoldThreshold(unittest.TestCase):
    """Declining to fold while the claim's own witnesses are cheaper than the
    proof that would replace them. The policy needs something else to carry the
    transactions, so it is a signature-pool policy rather than a general one."""

    def small_run(self, threshold, relay="flood", kind=KIND_PQ):
        overlay = two_node_overlay(1.0)
        sim = NetworkSim(overlay, seed=2, relay_policy=relay,
                         fold_threshold=threshold,
                         costs=CostTable(verify_recv=1.0))
        for index in range(4):
            sim.schedule_birth(index * 100.0, kind)
        sim.schedule_ticks(6)
        sim.schedule_slots(2)
        sim.run(until=6000.0)
        return sim

    def test_a_small_signature_claim_is_not_worth_proving(self):
        deferred = self.small_run("crossover")
        self.assertGreater(deferred.folds_deferred, 0)
        self.assertEqual(deferred.proofs_completed, 0)

    def test_the_default_proves_regardless(self):
        always = self.small_run("always")
        self.assertEqual(always.folds_deferred, 0)
        self.assertGreater(always.proofs_completed, 0)

    def test_a_privacy_claim_is_always_worth_proving(self):
        # One leanSTARK witness already costs a recursive proof, so there is no
        # width at which concatenating them is cheaper.
        privacy = self.small_run("crossover", kind=KIND_PRIVACY)
        self.assertEqual(privacy.folds_deferred, 0)
        self.assertGreater(privacy.proofs_completed, 0)

    def test_deferring_saves_the_bytes_it_declines_to_spend(self):
        always = self.small_run("always")
        deferred = self.small_run("crossover")
        self.assertLess(deferred.total_offered_bytes(),
                        always.total_offered_bytes())


class TestRandomPair(unittest.TestCase):
    """The no-coordination baseline: two objects, drawn uniformly, kind-blind.

    Worth having as a floor. Every other strategy claims to beat *something*,
    and without this the something is unstated -- v1 characterised aggregation
    with exactly this rule, and it is what the EIP's priority order has to
    justify itself against.
    """

    def selection(self, fixture, tick=0, budget=1e9):
        return get("selection", "random_pair")(
            fixture.node, fixture.arena, tick, fixture.costs, budget)

    def test_it_takes_a_pair_and_never_more(self):
        fixture = Fixture()
        for _ in range(12):
            fixture.leaf()
        for tick in range(6):
            with self.subTest(tick=tick):
                self.assertEqual(len(self.selection(fixture, tick).inputs), 2)

    def test_it_never_folds_an_object_with_itself(self):
        fixture = Fixture()
        for _ in range(8):
            fixture.leaf()
        for tick in range(20):
            inputs = self.selection(fixture, tick).inputs
            self.assertEqual(len(set(inputs)), len(inputs))

    def test_one_object_is_selected_alone_rather_than_doubled(self):
        fixture = Fixture()
        fixture.leaf()
        self.assertEqual(len(self.selection(fixture).inputs), 1)

    def test_nothing_held_selects_nothing(self):
        fixture = Fixture()
        self.assertTrue(self.selection(fixture).is_empty())

    def test_the_draw_is_derived_rather_than_drawn(self):
        # Same node, same tick, same answer -- no hidden generator state, so
        # asking twice cannot change what a run does.
        fixture = Fixture()
        for _ in range(10):
            fixture.leaf()
        self.assertEqual(self.selection(fixture, 3).inputs,
                         self.selection(fixture, 3).inputs)
        # And different ticks do move it, or "uniform" would be a fiction.
        picks = {self.selection(fixture, tick).inputs for tick in range(12)}
        self.assertGreater(len(picks), 1)

    def test_the_budget_cannot_refuse_a_pair(self):
        # A pair is the cheapest fold there is, so a budget has nothing to say
        # about it: this policy asks for exactly the floor.
        fixture = Fixture(CostTable(prove_recursive=lambda deps: 1e6))
        fixture.leaf()
        fixture.leaf()
        self.assertEqual(len(self.selection(fixture, budget=1.0).inputs), 2)

    def test_it_is_blind_to_kind_and_to_fee(self):
        # Over many ticks a low-fee transaction is drawn as readily as a rich
        # one. eip_priority would take the rich one every time.
        fixture = Fixture()
        poor = [fixture.leaf(fee=0.0).id for _ in range(6)]
        rich = [fixture.leaf(fee=100.0).id for _ in range(6)]
        drawn = set()
        for tick in range(40):
            drawn.update(self.selection(fixture, tick).inputs)
        self.assertTrue(drawn & set(poor), "no low-fee object was ever drawn")
        self.assertTrue(drawn & set(rich))


class TestTheEstimateIsInExecutionsUnit(unittest.TestCase):
    """An estimate in a unit nothing charges is a budget ceiling in disguise.

    The same fault has now been found twice -- once under `inputs`, once under
    `circuit` -- so the size a selection is priced by travels with the cost
    table the wiring hands it, rather than being guessed at the call site.
    """

    def table(self, basis):
        return CostTable(prove_recursive=lambda size: 100.0 * size).for_basis(
            basis)

    def test_inputs_prices_objects_and_deps_prices_dependencies(self):
        # Three inputs whose claims total twelve dependencies. Under `inputs`
        # execution charges three; a dependency count would charge twelve and
        # stop the selection four times too early.
        inputs = self.table("inputs")
        deps = self.table("deps")
        self.assertEqual(estimate_cost(inputs, 12, None, None, (1, 2, 3)),
                         300.0)
        self.assertEqual(estimate_cost(deps, 12, None, None, (1, 2, 3)),
                         1200.0)

    def test_the_two_agree_when_every_input_is_a_leaf(self):
        # A leaf claims itself, so the counts coincide -- which is why the
        # divergence only shows once aggregates are being folded.
        for basis in ("inputs", "deps"):
            with self.subTest(basis=basis):
                self.assertEqual(
                    estimate_cost(self.table(basis), 3, None, None, (1, 2, 3)),
                    300.0)

    def test_a_table_is_not_stamped_in_place(self):
        # Tables outlive runs and can be shared between them.
        shared = CostTable(prove_recursive=lambda size: 100.0 * size)
        stamped = shared.for_basis("inputs")
        self.assertEqual(shared.prove_cost_basis, "deps")
        self.assertEqual(stamped.prove_cost_basis, "inputs")

    def test_the_wiring_hands_selection_a_stamped_table(self):
        from starksim.netsim import NetworkSim
        from tests.support import two_node_overlay
        sim = NetworkSim(two_node_overlay(0.0), prove_cost_basis="inputs",
                         costs=CostTable(prove_recursive=lambda size: 1.0))
        self.assertEqual(sim._selection_costs.prove_cost_basis, "inputs")
        self.assertEqual(sim.costs.prove_cost_basis, "deps")


class TestSelectionOnlyPicksWhatIsHeld(unittest.TestCase):
    """A node may fold only what it holds.

    The property, rather than any one way of breaking it. Strategies find their
    own candidates in `view.held` and are safe by construction, but the wiring
    also hands them `previous_aggregate` -- a pointer to the node's last proof,
    written when that proof completed and never cleared. Eviction can take the
    object while the pointer still names it, and folding it then would be
    proving over evidence the node dropped.
    """

    def selections_under(self, **kwargs):
        """Every selection made during a run, with what its node held."""
        observed = []
        inner = strategies.get("selection", kwargs.pop("strategy",
                                                       "eip_priority"))

        def watched(node, arena, tick, costs, budget, previous_aggregate=None):
            selection = inner(node, arena, tick, costs, budget,
                              previous_aggregate)
            observed.append((set(selection.inputs), set(node.view.held)))
            return selection

        name = f"watched_{len(observed)}_{id(watched)}"
        strategies.register("selection", name)(watched)
        rng = random.Random("held:topology")
        overlay = Overlay.random_regular(16, 4, rng, base_latency=50.0)
        settings = dict(seed=2, capacity=bytes_per_ms(25),
                        costs=CostTable(verify_recv=1.0,
                                        prove_recursive=lambda deps: 150.0),
                        selection_strategy=name)
        settings.update(kwargs)
        sim = NetworkSim(overlay, **settings)
        for index in range(120):
            sim.schedule_birth(index * 60.0, KIND_PQ)
        sim.schedule_ticks(20)
        sim.schedule_slots(4)
        sim.run(until=12000.0)
        return observed

    def assert_all_held(self, observed):
        self.assertTrue(observed, "the fixture produced no selections")
        for inputs, held in observed:
            self.assertTrue(inputs <= held,
                            f"selected {sorted(inputs - held)} while holding "
                            f"{len(held)} objects")

    def test_with_an_unbounded_candidate_set(self):
        self.assert_all_held(self.selections_under())

    def test_with_a_tight_candidate_set(self):
        # The reachable case: at this bound a node's own aggregate is evicted
        # often, and the pointer to it outlives the object.
        self.assert_all_held(self.selections_under(candidate_limit=4,
                                                   pin_own_aggregate=False))

    def test_with_its_own_aggregate_pinned(self):
        self.assert_all_held(self.selections_under(candidate_limit=4))

    def test_across_the_strategies(self):
        for strategy in ("eip_priority", "coverage_greedy", "fee_density",
                         "deadline", "random_pair"):
            with self.subTest(strategy=strategy):
                self.assert_all_held(
                    self.selections_under(strategy=strategy,
                                          candidate_limit=4,
                                          pin_own_aggregate=False))


class TestPlansPriceWhatTheyAssert(unittest.TestCase):
    """A job's dependency count is what its proof will assert, not what its
    inputs structurally cover.

    The two differ once anything a selected input covers has settled. Counting
    the structural union charges execution for dependencies the frozen claim
    drops, and disagrees with the estimate selection was stopped by -- the same
    unit fault as pricing an estimate in the wrong basis, one level down.
    """

    def a_selection(self, discarded):
        fixture = Fixture()
        leaves = [fixture.leaf() for _ in range(4)]
        selection = fixture.select()
        # Re-frame the selection as though one of its transactions had settled.
        settled = 1 << leaves[discarded].leaf_index
        return Selection(selection.inputs, selection.input_claims,
                         selection.claim_union,
                         selection.claim_union & ~settled,
                         selection.tick, selection.revision,
                         selection.estimated_cost)

    def test_a_settled_dependency_is_not_charged_for(self):
        selection = self.a_selection(discarded=0)
        for plan_name in ("wide", "binary", "capped8"):
            with self.subTest(fold_plan=plan_name):
                plan = get("fold_plan", plan_name)(selection)
                root = plan.jobs[plan.root]
                self.assertEqual(root.dep_count,
                                 selection.claimed.bit_count())
                self.assertLess(root.dep_count,
                                selection.claim_union.bit_count())

    def test_with_nothing_settled_the_two_agree(self):
        fixture = Fixture()
        for _ in range(4):
            fixture.leaf()
        selection = fixture.select()
        plan = get("fold_plan", "wide")(selection)
        self.assertEqual(plan.jobs[plan.root].dep_count,
                         selection.claim_union.bit_count())
