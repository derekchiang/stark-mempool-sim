#!/usr/bin/env python3
"""Followed-object knowledge tracing: selection, atomicity, completeness.

Two contracts are under test. Selection (`C3`) is an independent Bernoulli trial
per transaction, or an explicit list -- never a fixed uniform `K`, which cannot be
drawn online without biasing toward whichever transactions were born first.
Atomicity (`C5`) makes the *event* the unit of observation: one calendar event
performs several mutations, so reporting each would manufacture transitions
through states no observer could have seen.
"""

import io
import json
import random
import unittest

from starksim.arena import KIND_PQ
from starksim.costs import CostTable
from starksim.derive import stable_fraction
from starksim.netsim import NetworkSim
from starksim.network import Overlay, bytes_per_ms
from starksim.node import KNOWLEDGE_CLASSES, KNOWLEDGE_NONE, knowledge_class

from tests.support import parse_jsonl


def run(births=10, nodes=8, **kwargs):
    buffer = io.StringIO()
    overlay = Overlay.random_regular(nodes, 4, random.Random("follow:topology"),
                                     base_latency=50.0)
    defaults = dict(seed=1, capacity=bytes_per_ms(50), trace=buffer,
                    costs=CostTable(verify_recv=2.0,
                                    prove_recursive=lambda deps: 20.0),
                    aggregation_interval=250.0, slot_length=500.0)
    defaults.update(kwargs)
    sim = NetworkSim(overlay, **defaults)
    for index in range(births):
        sim.schedule_birth(index * 40.0, KIND_PQ)
    sim.schedule_ticks(6)
    sim.schedule_slots(3)
    sim.run()
    return sim, parse_jsonl(buffer.getvalue())


def transitions(events):
    return [event for event in events if event["e"] == "knw"]


class TestSelection(unittest.TestCase):
    def test_nothing_is_followed_by_default(self):
        _sim, events = run()
        self.assertEqual(transitions(events), [])

    def test_an_explicit_list_follows_exactly_what_was_asked(self):
        _sim, events = run(trace_follow=[2, 5])
        followed = {event["tx"] for event in transitions(events)}
        self.assertEqual(followed, {2, 5})

    def test_explicit_indices_that_were_never_born_follow_nothing(self):
        # A different statement from "none selected", and the panel has to say
        # which -- so the trace must not silently substitute anything.
        _sim, events = run(births=3, trace_follow=[99])
        self.assertEqual(transitions(events), [])

    def test_a_rate_selects_by_the_documented_hash(self):
        sim, events = run(births=40, trace_follow_rate=0.25,
                          trace_follow_seed="fixed")
        followed = {event["tx"] for event in transitions(events)}
        expected = set()
        for leaf_index in range(40):
            if stable_fraction("fixed", "trace-follow", leaf_index) < 0.25:
                expected.add(leaf_index)
        self.assertEqual(followed, expected)
        self.assertTrue(expected)

    def test_the_same_seed_selects_the_same_set(self):
        _a, events_a = run(births=30, trace_follow_rate=0.3,
                           trace_follow_seed="s1")
        _b, events_b = run(births=30, trace_follow_rate=0.3,
                           trace_follow_seed="s1")
        _c, events_c = run(births=30, trace_follow_rate=0.3,
                           trace_follow_seed="s2")
        followed_a = {event["tx"] for event in transitions(events_a)}
        followed_b = {event["tx"] for event in transitions(events_b)}
        followed_c = {event["tx"] for event in transitions(events_c)}
        self.assertEqual(followed_a, followed_b)
        self.assertNotEqual(followed_a, followed_c)

    def test_the_observed_rate_matches_the_asked_rate(self):
        # Reproducibility is not representativeness. This is the part that would
        # fail if selection were "the first K" or otherwise position-dependent.
        for rate in (0.1, 0.5):
            selected = 0
            trials = 4000
            for leaf_index in range(trials):
                if stable_fraction(7, "trace-follow", leaf_index) < rate:
                    selected += 1
            self.assertAlmostEqual(selected / trials, rate, delta=0.03)

    def test_selection_does_not_change_the_simulation(self):
        plain, _ = run(births=20)
        followed, _ = run(births=20, trace_follow_rate=0.5)
        other_seed, _ = run(births=20, trace_follow_rate=0.5,
                            trace_follow_seed="different")
        self.assertEqual(plain.metrics(), followed.metrics())
        self.assertEqual(plain.metrics(), other_seed.metrics())


class TestAtomicity(unittest.TestCase):
    def test_each_transition_chain_is_well_formed(self):
        """`from` continues the previous `to`, and nothing goes to itself.

        A per-mutation report would break this by emitting states the model
        passed through inside one handler -- wrapper completion pops the
        in-flight proof, installs bodies, then admits the aggregate.
        """
        _sim, events = run(births=12, trace_follow_rate=1.0)
        chains = {}
        for event in transitions(events):
            key = (event["n"], event["tx"])
            previous = chains.get(key, KNOWLEDGE_NONE)
            self.assertEqual(event["from"], previous, key)
            self.assertNotEqual(event["from"], event["to"], key)
            self.assertIn(event["to"], KNOWLEDGE_CLASSES)
            chains[key] = event["to"]

    def test_at_most_one_transition_per_pair_per_instant(self):
        # Not a complete proof of atomicity -- two genuinely distinct events can
        # share a rounded timestamp -- but a manufactured intermediate shows up
        # here as two records for one pair at one time.
        _sim, events = run(births=12, trace_follow_rate=1.0)
        seen = set()
        for event in transitions(events):
            key = (event["n"], event["tx"], event["t"])
            self.assertNotIn(key, seen)
            seen.add(key)

    def test_every_transition_names_the_event_that_caused_it(self):
        _sim, events = run(births=8, trace_follow_rate=1.0)
        records = transitions(events)
        self.assertTrue(records)
        for event in records:
            self.assertTrue(event["by"])


class TestCompleteness(unittest.TestCase):
    """The guard against a dirty mark that was never made.

    Checking only the final state is worthless here, and a first version of this
    class did exactly that: `mark_all()` fires at every slot boundary, so the
    tracker re-syncs continually and its *end* state is correct no matter which
    intermediate marks are missing. Every single mark could be deleted and the
    suite stayed green.

    So the invariant is checked after **every event**, which is where the
    contract puts it. The hook registers after the tracer's own, and hooks run in
    registration order, so it sees the state the tracer has just finished
    publishing.
    """

    def _assert_tracker_consistent(self, **kwargs):
        sim, events = self._run_checked(**kwargs)
        return sim, events

    def _assert_emitted_trace_consistent(self, births=14, nodes=8, **kwargs):
        """Check the *emitted records*, not the tracker's own cache.

        The other check compares `KnowledgeTracer.classes` with
        `knowledge_class`, which proves those two agree -- and a transition that
        updated the cache but failed to emit its `knw` record would pass. So this
        replays only what the trace actually said and compares that to the
        simulator, after every event.
        """
        buffer = io.StringIO()
        overlay = Overlay.random_regular(nodes, 4,
                                        random.Random("emitted:topology"),
                                        base_latency=50.0)
        defaults = dict(seed=1, capacity=bytes_per_ms(50), trace=buffer,
                        costs=CostTable(verify_recv=2.0,
                                        prove_recursive=lambda deps: 20.0),
                        aggregation_interval=250.0, slot_length=500.0,
                        trace_follow_rate=1.0)
        defaults.update(kwargs)
        sim = NetworkSim(overlay, **defaults)
        replayed = {}
        seen_lines = [0]
        checks = [0]

        def verify(kind):
            # Consume whatever the trace has said since the last event, and
            # rebuild class state from that alone.
            lines = buffer.getvalue().split("\n")
            for index in range(seen_lines[0], len(lines) - 1):
                line = lines[index].strip()
                seen_lines[0] = index + 1
                if not line:
                    continue
                event = json.loads(line)
                if event.get("e") == "knw":
                    replayed[(event["n"], event["tx"])] = event["to"]
            for node in sim.nodes:
                for leaf_index in sim.knowledge.followed:
                    actual = knowledge_class(node, leaf_index, sim.arena)
                    rebuilt = replayed.get((node.id, leaf_index),
                                           KNOWLEDGE_NONE)
                    self.assertEqual(rebuilt, actual,
                                     f"node {node.id} leaf {leaf_index} "
                                     f"after {kind}: trace says {rebuilt}, "
                                     f"simulator says {actual}")
            checks[0] += 1

        sim.engine.after_event(verify)
        for index in range(births):
            sim.schedule_birth(index * 40.0, KIND_PQ)
        sim.schedule_ticks(6)
        sim.schedule_slots(3)
        sim.run()
        self.assertGreater(checks[0], 50)
        return sim

    def test_the_emitted_records_alone_rebuild_the_true_state(self):
        sim = self._assert_emitted_trace_consistent()
        self.assertTrue(sim.knowledge.followed)

    def test_the_emitted_records_hold_up_under_eviction(self):
        self._assert_emitted_trace_consistent(candidate_limit=3)

    def test_the_emitted_records_hold_up_under_announce_fetch(self):
        self._assert_emitted_trace_consistent(relay_policy="announce_fetch")

    def test_the_emitted_records_hold_up_with_a_link_cap(self):
        self._assert_emitted_trace_consistent(link_capacity=bytes_per_ms(20))

    def test_the_emitted_records_hold_up_when_plans_are_abandoned(self):
        self._assert_emitted_trace_consistent(on_tick_overrun="abandon")

    def test_the_emitted_records_hold_up_with_random_proposers(self):
        self._assert_emitted_trace_consistent(proposer_mode="random")

    def _run_checked(self, births=14, nodes=8, **kwargs):
        buffer = io.StringIO()
        overlay = Overlay.random_regular(nodes, 4,
                                         random.Random("follow:topology"),
                                         base_latency=50.0)
        defaults = dict(seed=1, capacity=bytes_per_ms(50), trace=buffer,
                        costs=CostTable(verify_recv=2.0,
                                        prove_recursive=lambda deps: 20.0),
                        aggregation_interval=250.0, slot_length=500.0)
        defaults.update(kwargs)
        sim = NetworkSim(overlay, **defaults)
        checks = [0]

        def verify(_kind):
            for node in sim.nodes:
                for leaf_index in sim.knowledge.followed:
                    actual = knowledge_class(node, leaf_index, sim.arena)
                    tracked = sim.knowledge.classes.get(
                        (node.id, leaf_index), KNOWLEDGE_NONE)
                    self.assertEqual(tracked, actual,
                                     f"node {node.id} leaf {leaf_index} "
                                     f"after {_kind}")
            checks[0] += 1

        sim.engine.after_event(verify)
        for index in range(births):
            sim.schedule_birth(index * 40.0, KIND_PQ)
        sim.schedule_ticks(6)
        sim.schedule_slots(3)
        sim.run()
        self.assertGreater(checks[0], 50)
        return sim, parse_jsonl(buffer.getvalue())

    def test_the_tracker_agrees_with_a_full_recount(self):
        sim, events = self._assert_tracker_consistent(
            births=14, trace_follow_rate=1.0)
        self.assertTrue(transitions(events))
        self.assertTrue(sim.knowledge.followed)

    def test_it_agrees_under_eviction_too(self):
        self._assert_tracker_consistent(
            births=14, trace_follow_rate=1.0, candidate_limit=3)

    def test_it_agrees_under_announce_fetch(self):
        self._assert_tracker_consistent(
            births=14, trace_follow_rate=1.0,
            relay_policy="announce_fetch")

    def test_it_agrees_when_requests_time_out(self):
        # A timeout that fires before any answer can arrive, so requests time
        # out and are retried or abandoned all run long. Clearing `requested`
        # is a knowledge change like any other.
        sim, _events = self._assert_tracker_consistent(
            births=14, trace_follow_rate=1.0,
            relay_policy="announce_fetch", request_timeout=60.0)
        self.assertGreater(sim.requests_timed_out, 0)

    def test_it_agrees_when_chain_facts_propagate(self):
        self._assert_tracker_consistent(
            births=14, trace_follow_rate=1.0, chain_notify="overlay")

    def test_it_agrees_with_expiring_transactions(self):
        buffer = io.StringIO()
        overlay = Overlay.random_regular(8, 4, random.Random("expiry"),
                                         base_latency=50.0)
        sim = NetworkSim(overlay, seed=1, capacity=bytes_per_ms(50),
                         trace=buffer, aggregation_interval=250.0,
                         slot_length=500.0, trace_follow_rate=1.0,
                         costs=CostTable(verify_recv=2.0,
                                         prove_recursive=lambda deps: 20.0))

        def verify(kind):
            for node in sim.nodes:
                for leaf_index in sim.knowledge.followed:
                    self.assertEqual(
                        sim.knowledge.classes.get((node.id, leaf_index),
                                                  KNOWLEDGE_NONE),
                        knowledge_class(node, leaf_index, sim.arena),
                        f"node {node.id} leaf {leaf_index} after {kind}")

        sim.engine.after_event(verify)
        for index in range(12):
            sim.schedule_birth(index * 40.0, KIND_PQ, lifetime=300.0)
        sim.schedule_ticks(6)
        sim.schedule_slots(3)
        sim.run()
        self.assertGreater(sim.chain.expired_count(), 0)


class TestTracerCost(unittest.TestCase):
    """The tracer must not cost more than linearly in what it observes.

    It once did: every event reclassified the dirty node against *every* followed
    transaction, so the work per event grew with the run and observation was
    quadratic while the simulation it watched was linear. At fifty slots that was
    2.3 seconds of simulation carrying 48 seconds of tracing.

    Asserted in classifications rather than seconds. Wall clock on a shared
    machine is not signal -- this project lost an afternoon to a neighbour's
    build -- and the count is exactly the quantity that was growing.
    """

    def _classifications(self, slots):
        duration = slots * 4000.0
        overlay = Overlay.random_regular(12, 4, random.Random("cost:topology"),
                                         base_latency=100.0)
        sim = NetworkSim(overlay, seed=1, capacity=bytes_per_ms(50),
                         trace=io.StringIO(), candidate_limit=8,
                         trace_follow_rate=0.5,
                         costs=CostTable(prove_recursive=lambda deps: 30.0))
        for index in range(int(duration / 400.0)):
            sim.schedule_birth(index * 400.0, KIND_PQ)
        sim.schedule_ticks(int(duration / 1000.0))
        sim.schedule_slots(slots)
        sim.run()
        return sim.knowledge.classifications, len(sim.knowledge.followed)

    def test_the_work_grows_about_linearly_with_the_run(self):
        small, followed_small = self._classifications(6)
        large, followed_large = self._classifications(24)
        self.assertGreater(small, 0)
        # Four times the run, and four times the followed set with it.
        self.assertAlmostEqual(followed_large / followed_small, 4, delta=1.5)
        # Quadratic is about sixteen; the old behaviour was worse than that. The
        # bound sits below it because some superlinear growth is real rather than
        # wasteful: aggregates widen as a run goes on, so one wrapper delivery
        # legitimately changes what a node knows about more and more followed
        # transactions. What this catches is a return to reclassifying the whole
        # followed set on every event, which is a different thing entirely.
        self.assertLess(large / small, 14,
                        f"{small} -> {large} classifications for 4x the run")

if __name__ == "__main__":
    unittest.main()
