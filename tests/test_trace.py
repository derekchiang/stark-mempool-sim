#!/usr/bin/env python3
"""Trace completeness and reconstruction from the trace alone.

"Per-node propagation and utilisation series reproducible from the trace alone"
is a strong requirement: it means a reader who has only the file can recover what
the run measured, without re-running it and without trusting a summary.
"""

import io
import json
import unittest

from starksim.arena import KIND_PQ
from starksim.costs import CostTable
from starksim.netsim import CSV_COLUMNS, NetworkSim
from starksim.network import Overlay, SizeModel, bytes_per_ms
from starksim.trace import SCHEMA_VERSION

from starksim.chain import ChainState, InstantNotifier
from starksim.knowledge import KnowledgeTracer
from starksim.node import Node
from starksim.trace import Tracer

from tests.support import line_overlay, parse_jsonl, two_node_overlay


def traced(node_count=6, **kwargs):
    buffer = io.StringIO()
    adjacency = []
    for node in range(node_count):
        adjacency.append([peer for peer in range(node_count) if peer != node])
    overlay = Overlay.from_adjacency(adjacency, base_latency=20.0)
    defaults = dict(seed=1, slot_length=400.0, aggregation_interval=200.0,
                    capacity=bytes_per_ms(50), proposer_id=0,
                    costs=CostTable(verify_recv=2.0,
                                    prove_recursive=lambda deps: 20.0),
                    trace=buffer)
    defaults.update(kwargs)
    sim = NetworkSim(overlay, **defaults)
    for index in range(12):
        sim.schedule_birth(index * 30.0, KIND_PQ, fee=float(index % 3))
    sim.schedule_ticks(6)
    sim.schedule_slots(4)
    sim.run()
    return sim, parse_jsonl(buffer.getvalue())


class TestTraceShape(unittest.TestCase):
    def test_the_header_records_the_variant_and_the_sampling(self):
        # A sampled trace and a quiet network look identical from the inside, so
        # the reader has to be told which it is holding.
        sim, events = traced(trace_sample=4, trace_focus=[0, 1])
        header = events[0]
        self.assertEqual(header["e"], "cfg")
        self.assertEqual(header["trace_sample"], 4)
        self.assertEqual(header["trace_focus"], [0, 1])
        self.assertEqual(header["nodes"], 6)
        self.assertIn("relay_policy", header)
        self.assertIn("fold_plan", header)

    def test_focus_restricts_the_trace_and_says_so(self):
        sim, events = traced(trace_focus=[2])
        nodes = {event["n"] for event in events[1:] if "n" in event}
        self.assertEqual(nodes, {2})
        self.assertGreater(sim.tracer.suppressed, 0)

    def test_sampling_thins_only_message_events(self):
        # Admissions and counters must survive sampling, or the series they
        # support cannot be rebuilt.
        _sim, dense = traced(trace_sample=1)
        _sim, sparse = traced(trace_sample=10)
        def count(events, kind):
            return sum(1 for event in events if event["e"] == kind)
        self.assertLess(count(sparse, "snd"), count(dense, "snd"))
        self.assertEqual(count(sparse, "adm"), count(dense, "adm"))
        self.assertEqual(count(sparse, "cnt"), count(dense, "cnt"))

    def test_no_trace_means_no_cost(self):
        sim, _events = traced()
        quiet = NetworkSim(
            two_node_overlay(1.0), seed=1)
        self.assertFalse(quiet.tracer.enabled())
        self.assertEqual(quiet.tracer.written, 0)

    def test_sample_below_one_rejected(self):
        with self.assertRaises(ValueError):
            traced(trace_sample=0)


class TestReconstructionFromTheTraceAlone(unittest.TestCase):
    """The acceptance criterion, taken literally: rebuild it from the file."""

    def test_propagation_is_recoverable_when_state_no_longer_holds_it(self):
        """The clearest argument for keeping a trace at all.

        By the end of a run every included transaction has been pruned from
        every node's view, so the final state cannot answer "when did node 4
        learn of this?" -- the information is gone. The trace still has it,
        because it recorded the event when it happened.
        """
        sim, events = traced()
        arrivals = {}
        admissions = {}
        for event in events:
            if event["e"] == "arr":
                arrivals[event["o"]] = (event["n"], event["t"])
            elif event["e"] == "adm" and "via" not in event:
                # Admissions tagged with `via` came through aggregation: the
                # object is a proof, not a transaction, so it has no `arr`.
                admissions.setdefault(event["o"], []).append(
                    (event["n"], event["t"] - event["born"]))
        self.assertTrue(admissions)
        # The state has lost it; the trace has not.
        pruned = 0
        for object_id in admissions:
            obj = sim.arena.objects[object_id]
            if all(node.view.admitted_at(obj) is None for node in sim.nodes):
                pruned += 1
        self.assertGreater(pruned, 0)
        for object_id, records in admissions.items():
            entry_node, _born_at = arrivals[object_id]
            learned = [node_id for node_id, _delay in records]
            # A node never "learns" of what it created, and no node twice.
            self.assertNotIn(entry_node, learned)
            self.assertEqual(len(learned), len(set(learned)))
            # Nothing arrives faster than one hop.
            for _node_id, delay in records:
                self.assertGreaterEqual(delay, 20.0 - 1e-9)

    def test_knowledge_gained_through_aggregation_is_traced_too(self):
        # A propagation series rebuilt without it would miss every transaction a
        # node learned about via a wrapper rather than a body.
        _sim, events = traced()
        via_wrapper = [event for event in events
                       if event["e"] == "adm" and event.get("via") == "wrapper"]
        self.assertTrue(via_wrapper)
        for event in via_wrapper:
            self.assertGreaterEqual(event["t"], event["born"])

    def test_per_node_propagation_means_are_recoverable(self):
        _sim, events = traced()
        totals = {}
        for event in events:
            if event["e"] == "adm":
                delay = event["t"] - event["born"]
                count, total = totals.get(event["n"], (0, 0.0))
                totals[event["n"]] = (count + 1, total + delay)
        self.assertGreater(len(totals), 1)
        for node_id, (count, total) in totals.items():
            self.assertGreater(total / count, 0.0)

    def test_utilisation_series_is_recoverable(self):
        sim, events = traced()
        # Cumulative counters differenced over their own interval give the
        # utilisation of any window a reader chooses.
        samples = {}
        for event in events:
            if event["e"] == "cnt":
                samples.setdefault(event["n"], []).append((event["t"], event["eb"]))
        self.assertTrue(samples)
        for node_id, series in samples.items():
            series.sort()
            for (earlier_t, earlier_busy), (later_t, later_busy) in zip(
                    series, series[1:]):
                window = later_t - earlier_t
                utilisation = (later_busy - earlier_busy) / window
                # A utilisation is a fraction of real time, always.
                self.assertGreaterEqual(utilisation, -1e-9)
                self.assertLessEqual(utilisation, 1.0 + 1e-9)
            # The final reading must agree with the simulation's own counter.
            final_t, final_busy = series[-1]
            self.assertAlmostEqual(
                final_busy, round(sim.egress[node_id].busy_time_at(final_t), 3),
                places=3)

    def test_the_block_series_is_recoverable(self):
        sim, events = traced()
        blocks = [event for event in events if event["e"] == "blk"]
        self.assertEqual(len(blocks), len(sim.chain.blocks))
        self.assertEqual([event["txs"] for event in blocks],
                         [block.count() for block in sim.chain.blocks])
        # The proposer is a data field, not the event's tracer identity.
        self.assertEqual([event["proposer"] for event in blocks],
                         [block.proposer for block in sim.chain.blocks])
        for event in blocks:
            self.assertNotIn("n", event)

    def test_blocks_survive_a_focus_that_excludes_their_proposer(self):
        """A block is chain truth, so no node subset can hide one.

        This is what a tracer node id costs when it is used for something merely
        *about* a node: focus filtering is keyed on it, so passing the proposer
        made a focused trace drop every block built elsewhere -- most of the
        chain, once the proposer rotates.
        """
        full_sim, full_events = traced(proposer_mode="rotating")
        focused_sim, focused_events = traced(proposer_mode="rotating",
                                             trace_focus=[3])
        def ribbon(events):
            rows = []
            for event in events:
                if event["e"] == "blk":
                    rows.append((event["slot"], event["proposer"],
                                 event["txs"], event["objs"], event["wasted"]))
            return rows
        proposers = {block.proposer for block in full_sim.chain.blocks}
        # The fixture has to actually exercise the bug: if node 3 built every
        # block, filtering could not have dropped one.
        self.assertTrue(proposers - {3})
        self.assertEqual(ribbon(focused_events), ribbon(full_events))

    def test_bytes_on_the_wire_are_recoverable_when_unsampled(self):
        sim, events = traced(trace_sample=1)
        traced_bytes = sum(event["b"] for event in events
                           if event["e"] == "snd")
        self.assertEqual(traced_bytes, sim.total_offered_bytes())


def assert_metrics_equal(case, left, right):
    """Compare metric dicts, treating NaN as equal to NaN.

    A metric is NaN where there is nothing to average -- `delay_mean_slots` in a
    run that builds no blocks -- and `nan != nan`, so a plain dict comparison
    reports a difference that is Python's rather than the model's.
    """
    case.assertEqual(sorted(left), sorted(right))
    for key, value in left.items():
        other = right[key]
        both_nan = (isinstance(value, float) and isinstance(other, float)
                    and value != value and other != other)
        if both_nan:
            continue
        case.assertEqual(value, other, key)


class TestObservationCadence(unittest.TestCase):
    """Counters are available even when no block boundary occurs."""

    def _propagation_only(self, births=4, until=2000.0, **kwargs):
        """Births and nothing else: no ticks, no slots, no blocks.

        This is the shape `bench/propagation.py` runs, so counter availability
        cannot depend on block construction.
        """
        buffer = io.StringIO()
        adjacency = []
        for node in range(5):
            adjacency.append([peer for peer in range(5) if peer != node])
        overlay = Overlay.from_adjacency(adjacency, base_latency=20.0)
        defaults = dict(seed=1, capacity=bytes_per_ms(50), trace=buffer,
                        trace_counter_interval=500.0)
        defaults.update(kwargs)
        if defaults.get("trace") is None:
            defaults["trace"] = None
        sim = NetworkSim(overlay, **defaults)
        for index in range(births):
            sim.schedule_birth(index * 200.0, KIND_PQ)
        sim.run(until=until)
        return sim, parse_jsonl(buffer.getvalue())

    def test_a_run_that_builds_no_blocks_still_reports_counters(self):
        _sim, events = self._propagation_only()
        counters = [event for event in events if event["e"] == "cnt"]
        self.assertTrue(counters)
        self.assertFalse([event for event in events if event["e"] == "blk"])

    def test_counters_bracket_the_run_and_land_on_a_global_grid(self):
        _sim, events = self._propagation_only()
        times = sorted({event["t"] for event in events if event["e"] == "cnt"})
        # The exact expected sequence, not "every time is a multiple". The
        # weaker assertion passed while a run whose calendar drained early lost
        # every boundary in the quiet tail: nothing emitted was wrong, plenty
        # was missing.
        self.assertEqual(times, [0.0, 500.0, 1000.0, 1500.0, 2000.0])
        # Never two records at one instant: a reader divides by elapsed time.
        for node_id in range(5):
            node_times = [event["t"] for event in events
                          if event["e"] == "cnt" and event["n"] == node_id]
            self.assertEqual(len(node_times), len(set(node_times)))

    def test_a_quiet_tail_still_gets_its_boundaries(self):
        """A run that drains its calendar long before `until`.

        Nothing happens after the single birth, so every boundary between it and
        the horizon exists only because the observer walks the quiet interval.
        """
        _sim, events = self._propagation_only(births=1, until=3000.0)
        times = sorted({event["t"] for event in events if event["e"] == "cnt"})
        self.assertEqual(times, [0.0, 500.0, 1000.0, 1500.0, 2000.0, 2500.0,
                                 3000.0])

    def test_segment_boundaries_are_labelled(self):
        _sim, events = self._propagation_only()
        labels = {event.get("seg") for event in events if event["e"] == "cnt"}
        self.assertIn("segment_start", labels)
        self.assertIn("segment_end", labels)
        self.assertIn(None, labels)          # the grid boundaries in between

    def test_observing_does_not_change_the_simulation(self):
        """Instrumentation must not move the thing it measures.

        The observer advances the clock to each boundary as it passes, so a
        traced run and an untraced one would follow different clock
        trajectories if the observer were registered only when tracing. It is
        registered always, and this asserts the consequence.
        """
        traced_sim, events = self._propagation_only()
        untraced_sim, untraced_events = self._propagation_only(trace=None)
        self.assertTrue(events)
        self.assertFalse(untraced_events)
        assert_metrics_equal(self, traced_sim.metrics(),
                             untraced_sim.metrics())

    def test_a_finer_grid_gives_more_samples_and_the_same_totals(self):
        coarse, coarse_events = self._propagation_only(
            trace_counter_interval=1000.0)
        fine, fine_events = self._propagation_only(
            trace_counter_interval=100.0)
        # Resolution is not identity: watching the same run more closely must
        # not make it a different run, which is why the interval lives in the
        # trace header rather than in the variant.
        assert_metrics_equal(self, coarse.metrics(), fine.metrics())
        def counters(events):
            return [e for e in events if e["e"] == "cnt"]
        self.assertGreater(len(counters(fine_events)),
                           len(counters(coarse_events)))

class TestSchemaAndIdentity(unittest.TestCase):
    def test_the_header_declares_a_schema_version(self):
        _sim, events = traced()
        self.assertEqual(events[0]["e"], "cfg")
        self.assertEqual(events[0]["trace_schema"], SCHEMA_VERSION)

    def test_arrivals_carry_both_identities(self):
        sim, events = traced()
        arrivals = [event for event in events if event["e"] == "arr"]
        self.assertTrue(arrivals)
        for event in arrivals:
            obj = sim.arena.objects[event["o"]]
            self.assertEqual(event["tx"], obj.leaf_index)

    def test_the_two_identity_spaces_actually_diverge(self):
        """Otherwise carrying both would be harmless duplication.

        A leaf advances the object counter and the leaf counter; an aggregate
        advances only the object counter. So once a tick folds anything, an
        object id stops predicting a leaf index -- which is the whole reason a
        transaction-level record needs `tx`.
        """
        sim, events = traced()
        arrivals = [event for event in events if event["e"] == "arr"]
        self.assertTrue(any(event["o"] != event["tx"] for event in arrivals))

def residency_from_trace(events, node_count):
    """Rebuild per-object residency from admissions and departures alone.

    Deliberately independent of the simulator: comparing a reconstruction
    against `residency_fraction` is only meaningful if the reconstruction does
    not consult the thing it is checking. `arr` seeds the origin, which never
    emits `adm`; `via`-tagged admissions are proofs, not the transactions they
    cover; `drop` removes.
    """
    holders = {}
    for event in events:
        if event["e"] == "arr":
            holders.setdefault(event["o"], set()).add(event["n"])
        elif event["e"] == "adm" and "via" not in event:
            holders.setdefault(event["o"], set()).add(event["n"])
        elif event["e"] == "drop":
            for object_id in event["o"]:
                if object_id in holders:
                    holders[object_id].discard(event["n"])
    fractions = {}
    for object_id, nodes in holders.items():
        fractions[object_id] = len(nodes) / node_count
    return fractions


def reach_from_trace(events):
    """Distinct (node, object) first admissions -- never decremented.

    Deduplicated because a node can admit the same object twice after eviction
    and `seen` forgetting, and counting both would put reach above one.
    """
    reached = {}
    for event in events:
        if event["e"] == "arr":
            reached.setdefault(event["o"], set()).add(event["n"])
        elif event["e"] == "adm" and "via" not in event:
            reached.setdefault(event["o"], set()).add(event["n"])
    return reached


class TestDepartures(unittest.TestCase):
    def _folding_run(self, **kwargs):
        buffer = io.StringIO()
        adjacency = []
        for node in range(6):
            adjacency.append([peer for peer in range(6) if peer != node])
        overlay = Overlay.from_adjacency(adjacency, base_latency=20.0)
        defaults = dict(seed=1, slot_length=400.0, aggregation_interval=200.0,
                        capacity=bytes_per_ms(50), proposer_id=0,
                        costs=CostTable(verify_recv=2.0,
                                        prove_recursive=lambda deps: 20.0),
                        trace=buffer)
        defaults.update(kwargs)
        sim = NetworkSim(overlay, **defaults)
        for index in range(12):
            sim.schedule_birth(index * 30.0, KIND_PQ)
        sim.schedule_ticks(6)
        sim.schedule_slots(4)
        sim.run()
        return sim, parse_jsonl(buffer.getvalue())

    def test_every_departure_is_traced_with_its_reason(self):
        sim, events = self._folding_run()
        traced = 0
        reasons = set()
        for event in events:
            if event["e"] == "drop":
                traced += len(event["o"])
                reasons.add(event["why"])
        self.assertEqual(traced, sim.total_departures())
        self.assertTrue(reasons <= {"superseded", "pruned", "evicted"})
        self.assertTrue(reasons)

    def test_eviction_is_traced_too(self):
        # The default is an unbounded view, so eviction needs asking for -- and
        # without this the third reason would never be exercised.
        sim, events = self._folding_run(candidate_limit=3)
        reasons = {event["why"] for event in events if event["e"] == "drop"}
        self.assertIn("evicted", reasons)
        traced = sum(len(event["o"]) for event in events
                     if event["e"] == "drop")
        self.assertEqual(traced, sim.total_departures())

    def test_residency_reconstructs_from_the_trace_alone(self):
        sim, events = self._folding_run()
        rebuilt = residency_from_trace(events, len(sim.nodes))
        self.assertTrue(rebuilt)
        for object_id, fraction in rebuilt.items():
            obj = sim.arena.objects[object_id]
            self.assertEqual(fraction, sim.residency_fraction(obj), object_id)

    def test_residency_of_every_object_including_aggregates(self):
        """Line order is the contract, and it was wrong for wrappers.

        `_admit_wrapper` admitted the aggregate, then evicted under the candidate
        bound, then emitted the admission -- so the trace said `drop` before
        `adm` for one object at one instant. A reducer replaying line order saw a
        drop for a record it had not created, ignored it, and finished with the
        object resident while the simulator had thrown it away.

        The other reconstruction test excludes `via`-tagged admissions, since it
        is about transactions. This one includes them, which is the only way the
        ordering matters.
        """
        sim, events = self._folding_run(candidate_limit=2)
        holders = {}
        for event in events:
            if event["e"] == "arr":
                holders.setdefault(event["o"], set()).add(event["n"])
            elif event["e"] == "adm":
                holders.setdefault(event["o"], set()).add(event["n"])
            elif event["e"] == "drop":
                for object_id in event["o"]:
                    if object_id in holders:
                        holders[object_id].discard(event["n"])
        checked = 0
        for object_id, nodes in holders.items():
            obj = sim.arena.objects.get(object_id)
            if obj is None:
                continue
            self.assertEqual(len(nodes) / len(sim.nodes),
                             sim.residency_fraction(obj), object_id)
            checked += 1
        self.assertGreater(checked, 5)
        # The fixture has to actually contain wrapper admissions, or the
        # ordering it exists to check never arises.
        self.assertTrue([e for e in events
                         if e["e"] == "adm" and e.get("via") == "wrapper"])

    def test_reach_is_never_below_residency_and_somewhere_strictly_above(self):
        # "Equality only where nothing retired" would be false: a dropped object
        # can be re-admitted and restore equality. What holds is the inequality,
        # plus at least one object where it is strict.
        sim, events = self._folding_run()
        residency = residency_from_trace(events, len(sim.nodes))
        reach = reach_from_trace(events)
        strict = 0
        for object_id, nodes in reach.items():
            reach_fraction = len(nodes) / len(sim.nodes)
            self.assertGreaterEqual(reach_fraction + 1e-12,
                                    residency[object_id])
            if reach_fraction > residency[object_id]:
                strict += 1
        self.assertGreater(strict, 0)

class TestLifecycle(unittest.TestCase):
    """`seg` bounds a segment's observation; only `end` says no more is coming."""

    def _sim(self, **kwargs):
        buffer = io.StringIO()
        adjacency = [[1], [0]]
        overlay = Overlay.from_adjacency(adjacency, base_latency=20.0)
        defaults = dict(seed=1, capacity=bytes_per_ms(50), trace=buffer,
                        trace_counter_interval=500.0)
        defaults.update(kwargs)
        return NetworkSim(overlay, **defaults), buffer

    def _events(self, buffer):
        return parse_jsonl(buffer.getvalue())

    def test_each_run_records_its_own_segment_and_stop_reason(self):
        sim, buffer = self._sim()
        sim.schedule_birth(0.0, KIND_PQ)
        # Scheduled beyond the horizon on purpose: `until` as a stop reason means
        # work was left pending, which is the case that censors an observation.
        # A calendar that empties first reports `drained` even when a horizon was
        # given, because nothing was cut off.
        sim.schedule_birth(800.0, KIND_PQ)
        sim.run(until=500.0)
        sim.run(until=1200.0)
        segments = [e for e in self._events(buffer) if e["e"] == "seg"]
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["start"], 0.0)
        self.assertEqual(segments[0]["t"], 500.0)
        self.assertEqual(segments[0]["why"], "until")
        self.assertEqual(segments[1]["start"], 500.0)
        self.assertEqual(segments[1]["t"], 1200.0)

    def test_a_drained_calendar_is_distinguished_from_a_horizon(self):
        sim, buffer = self._sim()
        sim.schedule_birth(0.0, KIND_PQ)
        sim.run()                                   # no horizon: drains
        segments = [e for e in self._events(buffer) if e["e"] == "seg"]
        self.assertEqual(segments[-1]["why"], "drained")

    def test_a_drained_segment_can_be_followed_by_more_work(self):
        # Which is exactly why a segment boundary cannot mean "finished".
        sim, buffer = self._sim()
        sim.schedule_birth(0.0, KIND_PQ)
        sim.run()
        sim.schedule_birth(900.0, KIND_PQ)
        sim.run()
        segments = [e for e in self._events(buffer) if e["e"] == "seg"]
        self.assertEqual(len(segments), 2)
        self.assertEqual([s["why"] for s in segments], ["drained", "drained"])
        self.assertFalse([e for e in self._events(buffer) if e["e"] == "end"])

    def test_finishing_emits_exactly_one_terminal_record(self):
        sim, buffer = self._sim()
        sim.schedule_birth(0.0, KIND_PQ)
        sim.run()
        sim.finish_trace()
        ends = [e for e in self._events(buffer) if e["e"] == "end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["status"], "complete")

    def test_a_finished_trace_refuses_further_work(self):
        # Without this, `end` would not mean what the contract says.
        sim, _buffer = self._sim()
        sim.schedule_birth(0.0, KIND_PQ)
        sim.run()
        sim.finish_trace()
        for call in (lambda: sim.run(),
                     lambda: sim.schedule_birth(10.0, KIND_PQ),
                     lambda: sim.schedule_ticks(1),
                     lambda: sim.schedule_slots(1),
                     lambda: sim.finish_trace()):
            with self.assertRaises(ValueError):
                call()

    def test_finishing_seals_the_engine_and_not_just_the_wrapper(self):
        """`sim.engine` is public, and guarding only NetworkSim was not enough.

        With a birth still on the calendar, `sim.engine.run()` walked past the
        wrapper's guard, processed it, and appended counter records after the
        `end` that said nothing more was coming.
        """
        sim, buffer = self._sim()
        sim.schedule_birth(0.0, KIND_PQ)
        sim.run()
        sim.schedule_birth(900.0, KIND_PQ)      # left pending on purpose
        sim.finish_trace()
        after = buffer.getvalue()
        with self.assertRaises(ValueError):
            sim.engine.run()
        with self.assertRaises(ValueError):
            sim.engine.schedule_at(1000.0, "birth", None)
        self.assertEqual(buffer.getvalue(), after)
        # And `end` really is the last record.
        self.assertEqual(json.loads(after.strip().split("\n")[-1])["e"], "end")

    def test_a_status_can_record_an_unhappy_ending(self):
        sim, buffer = self._sim()
        sim.run()
        sim.finish_trace(status="cancelled")
        ends = [e for e in self._events(buffer) if e["e"] == "end"]
        self.assertEqual(ends[0]["status"], "cancelled")
        with self.assertRaises(ValueError):
            self._sim()[0].finish_trace(status="probably fine")

    def test_lifecycle_records_are_not_node_scoped(self):
        # A focused trace must still carry them: they describe the observation,
        # not a node's stream.
        sim, buffer = self._sim(trace_focus=[0])
        sim.schedule_birth(0.0, KIND_PQ)
        sim.run()
        sim.finish_trace()
        events = self._events(buffer)
        for kind in ("seg", "end"):
            records = [e for e in events if e["e"] == kind]
            self.assertTrue(records, kind)
            for record in records:
                self.assertNotIn("n", record)

class TestPerKindBytes(unittest.TestCase):
    """Criterion 8: the counters must equal the simulator's own accounting."""

    def _counters(self, events):
        latest = {}
        for event in events:
            if event["e"] == "cnt":
                latest[event["n"]] = event
        return latest

    def test_per_kind_totals_sum_to_the_aggregate_totals(self):
        sim, events = traced()
        for node_id, record in self._counters(events).items():
            self.assertEqual(sum(record["obk"].values()), record["ob"])
            self.assertEqual(sum(record["sbk"].values()), record["sb"])
            self.assertEqual(sum(record["ibk"].values()), record["ib"])

    def test_bytes_are_attributed_to_the_right_side_of_the_link(self):
        """Two nodes, one object, one direction. Everything else here compares
        the trace against the very dictionaries being serialised, which is
        tautological: crediting ingress to the sender, relabelling a payload kind
        consistently, or charging a wrong-but-consistent size would all survive
        it. This asserts sender, receiver, kind and bytes independently.
        """
        buffer = io.StringIO()
        overlay = two_node_overlay(20.0)
        sizes = SizeModel()
        sim = NetworkSim(overlay, seed=1, capacity=bytes_per_ms(50),
                         size_model=sizes, trace=buffer,
                         trace_counter_interval=100.0)
        sim.schedule_birth(0.0, KIND_PQ, node_id=0)
        sim.run(until=400.0)
        expected = sizes.tx_body + sizes.triple + sizes.sig_witness
        latest = {}
        for line in buffer.getvalue().strip().split("\n"):
            event = json.loads(line)
            if event["e"] == "cnt":
                latest[event["n"]] = event
        # Node 0 originated and sent; node 1 received and relayed to nobody,
        # because its only peer is where the object came from.
        self.assertEqual(latest[0]["obk"], {"object": expected})
        self.assertEqual(latest[0]["sbk"], {"object": expected})
        self.assertEqual(latest[0].get("ibk"), {})
        self.assertEqual(latest[1]["ibk"], {"object": expected})
        self.assertEqual(latest[1].get("obk"), {})
        self.assertEqual(latest[1].get("sbk"), {})

    def test_they_equal_the_simulator_accounting(self):
        sim, events = traced()
        for node_id, record in self._counters(events).items():
            self.assertEqual(record["obk"], sim.offered_kind[node_id])
            self.assertEqual(record["sbk"], sim.sent_kind[node_id])
            self.assertEqual(record["ibk"], sim.ingress_kind[node_id])
            self.assertEqual(record["idb"],
                             sim.ingress_duplicate_bytes[node_id])

    def test_sent_never_exceeds_offered(self):
        sim, events = traced()
        for record in self._counters(events).values():
            for kind, sent in record["sbk"].items():
                self.assertLessEqual(sent, record["obk"].get(kind, 0), kind)

    def test_ingress_equals_egress_once_the_network_has_drained(self):
        # Only *once drained*. `sent` means completed node egress, which is not
        # arrival: with a link cap a message can have left its sender and not yet
        # reached anyone, so at an intermediate boundary the two legitimately
        # differ. The fixture runs to completion, which is what makes this an
        # identity rather than an approximation.
        sim, events = traced()
        sent = {}
        ingress = {}
        for record in self._counters(events).values():
            for kind, value in record["sbk"].items():
                sent[kind] = sent.get(kind, 0) + value
            for kind, value in record["ibk"].items():
                ingress[kind] = ingress.get(kind, 0) + value
        self.assertEqual(sent, ingress)

    def test_more_than_one_kind_is_actually_exercised(self):
        # Otherwise the split above would be vacuously satisfied.
        sim, events = traced()
        kinds = set()
        for record in self._counters(events).values():
            kinds |= set(record["obk"])
        self.assertGreater(len(kinds), 1)

    def test_fetching_and_flooding_differ_by_kind(self):
        # The comparison the panel exists for: flooding pushes objects, fetching
        # trades them for announcements and requests.
        _flood, flood_events = traced(relay_policy="flood")
        _fetch, fetch_events = traced(relay_policy="announce_fetch")
        def kinds(events):
            seen = set()
            for record in self._counters(events).values():
                seen |= set(record["obk"])
            return seen
        self.assertIn("object", kinds(flood_events))
        self.assertIn("announce", kinds(fetch_events))
        self.assertNotIn("announce", kinds(flood_events))

class TestTopology(unittest.TestCase):
    """Criterion 9: hop distances must reconstruct exactly."""

    def test_edges_are_canonical_and_sorted(self):
        _sim, events = traced()
        edges = events[0]["topology"]
        self.assertEqual(edges, sorted(edges))
        for low, high in edges:
            self.assertLess(low, high)
        # Each undirected pair exactly once.
        self.assertEqual(len(edges), len({tuple(e) for e in edges}))

    def test_hop_distances_reconstruct_for_every_reference_node(self):
        sim, events = traced()
        neighbours = {}
        for low, high in events[0]["topology"]:
            neighbours.setdefault(low, set()).add(high)
            neighbours.setdefault(high, set()).add(low)
        for origin in range(len(sim.nodes)):
            distance = {origin: 0}
            frontier = [origin]
            while frontier:
                following = []
                for node in frontier:
                    for peer in neighbours.get(node, ()):
                        if peer not in distance:
                            distance[peer] = distance[node] + 1
                            following.append(peer)
                frontier = following
            expected = sim.hop_distance_from(origin)
            rebuilt = [distance.get(node) for node in range(len(sim.nodes))]
            self.assertEqual(rebuilt, expected, origin)

    def test_an_oversized_graph_says_omitted_rather_than_nothing(self):
        # A reader must be able to tell "too big to write" from "no edges".
        import starksim.trace as trace_module
        original = trace_module.TOPOLOGY_EDGE_LIMIT
        import starksim.netsim as netsim_module
        try:
            netsim_module.TOPOLOGY_EDGE_LIMIT = 2
            _sim, events = traced()
            self.assertEqual(events[0]["topology"], "omitted")
        finally:
            netsim_module.TOPOLOGY_EDGE_LIMIT = original

class TestSelfDescription(unittest.TestCase):
    """What the header has to say for a row to be attributable."""

    def test_the_follow_mode_is_recorded_in_all_three_cases(self):
        # An empty knowledge panel has three different explanations -- no rate
        # was set, a rate selected nothing, or the named indices were never born
        # -- and a reader cannot tell them apart without this.
        cases = [
            ({}, "none"),
            ({"trace_follow_rate": 0.5, "trace_follow_seed": "s"}, "bernoulli"),
            ({"trace_follow": [1, 3]}, "explicit"),
        ]
        for kwargs, expected in cases:
            buffer = io.StringIO()
            overlay = two_node_overlay(20.0)
            sim = NetworkSim(overlay, seed=1, capacity=bytes_per_ms(50),
                             trace=buffer, **kwargs)
            sim.schedule_birth(0.0, KIND_PQ)
            sim.run()
            cfg = json.loads(buffer.getvalue().split("\n")[0])
            self.assertEqual(cfg["trace_follow"]["mode"], expected)
        self.assertEqual(cfg["trace_follow"]["indices"], [1, 3])

    def test_wire_sizes_are_part_of_the_identity(self):
        # Two runs that put different numbers of bytes on the wire must not
        # claim the same identity, which is the whole point of recording one.
        overlay = two_node_overlay(20.0)
        small = NetworkSim(overlay, size_model=SizeModel(tx_body=512)).variant()
        large = NetworkSim(overlay, size_model=SizeModel(tx_body=4096)).variant()
        self.assertNotEqual(small, large)
        self.assertEqual(small["size_tx_body"], 512)

    def test_bounded_state_limits_are_part_of_the_identity(self):
        # A tighter bound makes aggregation look worse, so a row measured under
        # one is not comparable with a row measured under another.
        overlay = two_node_overlay(20.0)
        loose = NetworkSim(overlay).variant()
        tight = NetworkSim(overlay, candidate_limit=4, seen_limit=8).variant()
        self.assertNotEqual(loose, tight)
        self.assertEqual(loose["candidate_limit"], -1)     # unbounded
        self.assertEqual(tight["candidate_limit"], 4)

    def test_every_variant_key_has_a_csv_column(self):
        # Order is the contract, so a new key without a column silently drops out
        # of every sweep row.
        overlay = two_node_overlay(20.0)
        variant = NetworkSim(overlay).variant()
        for key in variant:
            self.assertIn(key, CSV_COLUMNS, key)

if __name__ == "__main__":
    unittest.main()


def traced_line(**kwargs):
    """Twelve births on a six-node line, every transaction followed.

    A line rather than the clique above, so "the peer is a neighbour" is a
    claim with content: on a clique every node is.
    """
    buffer = io.StringIO()
    defaults = dict(seed=1, slot_length=400.0, aggregation_interval=200.0,
                    capacity=bytes_per_ms(50), proposer_id=0,
                    costs=CostTable(verify_recv=2.0,
                                    prove_recursive=lambda deps: 20.0),
                    trace=buffer, trace_follow_rate=1.0)
    defaults.update(kwargs)
    sim = NetworkSim(line_overlay(6, base_latency=20.0), **defaults)
    for index in range(12):
        sim.schedule_birth(index * 30.0, KIND_PQ, fee=float(index % 3))
    sim.schedule_ticks(6)
    sim.schedule_slots(4)
    sim.run()
    return sim, parse_jsonl(buffer.getvalue())


class TestDuplicates(unittest.TestCase):
    """`dup`: a delivery that taught the receiver nothing, per followed tx.

    The spread tree is what a transaction's propagation achieved; this is
    what it cost. Recorded only for objects covering a followed transaction,
    naming those transactions, so it is bounded like `knw`.
    """

    def assert_well_formed(self, sim, events, kind):
        dups = [e for e in events if e["e"] == "dup"]
        of_kind = [e for e in dups if e["k"] == kind]
        self.assertTrue(of_kind, "no duplicate " + kind + " deliveries traced")
        for dup in dups:
            self.assertIn(dup["peer"], sim.overlay.adjacency[dup["n"]])
            self.assertTrue(dup["txs"])
            obj = sim.arena.objects[dup["o"]]
            for tx in dup["txs"]:
                self.assertIn(tx, sim.knowledge.followed)
                self.assertTrue(sim.arena.coverage(obj) >> tx & 1, dup)
            self.assertTrue(any(
                e["e"] == "snd" and e["n"] == dup["peer"] and e["to"] == dup["n"]
                and e["k"] == dup["k"] and e["t"] <= dup["t"] for e in events), dup)
        # Every traced duplicate is one the simulator counted; the count is
        # wider, since it also holds objects covering no followed transaction.
        self.assertLessEqual(len(dups), sim.duplicates)
        return dups

    # The clique for flooding and announcing: on a line there is no second
    # path for an object to arrive by, so nothing is ever a duplicate.
    def test_flooded_objects_arrive_again_and_say_so(self):
        sim, events = traced(trace_follow_rate=1.0)
        self.assert_well_formed(sim, events, "object")

    def test_repeated_announcements_are_duplicates_too(self):
        sim, events = traced(relay_policy="announce_fetch",
                             trace_follow_rate=1.0)
        self.assert_well_formed(sim, events, "announce")

    def test_a_wrapper_already_held_is_a_duplicate(self):
        sim, events = traced_line(relay_policy="wrapper_only")
        self.assert_well_formed(sim, events, "wrapper")

    def test_nothing_is_recorded_for_unfollowed_transactions(self):
        sim, events = traced_line(trace_follow_rate=0.0)
        self.assertGreater(sim.duplicates, 0)
        self.assertEqual([e for e in events if e["e"] == "dup"], [])

    def test_a_proof_for_witnesses_in_flight_is_not_a_duplicate(self):
        """Mode 0 is being verified when the recursive proof arrives.

        The second copy verifies nothing and brings no fresh body, but it
        upgrades what the claim's pending evidence is -- the receiver can
        resolve the proof by reference afterwards where it could not before
        -- so recording it as `dup` would call a delivery that taught
        something waste.
        """
        from starksim.payload import (SizeBreakdown, WireMessage, WrapperCore,
                                      WrapperPayload)
        stream = io.StringIO()
        sim = NetworkSim(two_node_overlay(10.0), seed=1, proposer_id=0,
                         costs=CostTable(verify_recv=50.0), trace=stream)
        leaf = sim.arena.new_leaf(KIND_PQ, 0.0)
        sim.knowledge.follow(leaf.leaf_index)
        aggregate = sim.arena.new_aggregate((leaf,), 1.0, 1, claimed=0b1)

        def wrapper(mode):
            core = WrapperCore(wrapper_id=mode + 1,
                               proof_object_id=aggregate.id, creator=1,
                               tick=0, mode=mode, tx_mask=0b1, dep_count=1)
            return WireMessage(sender=1, receiver=0,
                               payload=WrapperPayload(core=core, body_mask=0b1,
                                                      size=SizeBreakdown()))

        sim._on_deliver(sim.engine, wrapper(0))   # witnesses, now in flight
        self.assertFalse(sim.nodes[0].can_resolve_proof(aggregate.id))
        sim._on_deliver(sim.engine, wrapper(1))   # the proof, same claim
        self.assertTrue(sim.nodes[0].can_resolve_proof(aggregate.id))
        self.assertEqual(sim.duplicates, 0)
        self.assertEqual([e for e in parse_jsonl(stream.getvalue())
                          if e["e"] == "dup"], [])

    def test_a_duplicate_wrapper_names_only_what_it_claims(self):
        """The structure covers two transactions; the wrapper claims one.

        A duplicate's `txs` is what the delivery named, and a wrapper names
        its own claim: charging the copy to every transaction the aggregate
        behind it structurally covers would put spreading cost on a story
        the wrapper never carried.
        """
        from starksim.payload import (SizeBreakdown, WireMessage, WrapperCore,
                                      WrapperPayload)
        stream = io.StringIO()
        sim = NetworkSim(two_node_overlay(10.0), seed=1, proposer_id=0,
                         costs=CostTable(verify_recv=0.0), trace=stream)
        leaves = [sim.arena.new_leaf(KIND_PQ, 0.0) for _ in range(2)]
        for leaf in leaves:
            sim.knowledge.follow(leaf.leaf_index)
        aggregate = sim.arena.new_aggregate(leaves, 1.0, 1, claimed=0b11)

        def message():
            core = WrapperCore(wrapper_id=1, proof_object_id=aggregate.id,
                               creator=1, tick=0, mode=1, tx_mask=0b01,
                               dep_count=2)
            return WireMessage(sender=1, receiver=0,
                               payload=WrapperPayload(core=core,
                                                      body_mask=0b01,
                                                      size=SizeBreakdown()))

        sim._on_deliver(sim.engine, message())   # zero cost: admitted at once
        sim._on_deliver(sim.engine, message())   # a true duplicate
        [dup] = [e for e in parse_jsonl(stream.getvalue()) if e["e"] == "dup"]
        self.assertEqual(dup["txs"], [leaves[0].leaf_index])


class TestSpreadAttribution(unittest.TestCase):
    """`knw` says which object a class rests on and who delivered it.

    Together they let a reader draw how one transaction spread, in which form
    -- its own object, a wrapper, a block notice -- and along which edges. The
    peer is attributed only where what it delivered is the evidence, which is
    the rule the last test checks directly.
    """

    def test_a_peer_is_a_neighbour_that_sent_something_first(self):
        sim, events = traced_line()
        attributed = [e for e in events if e["e"] == "knw" and "peer" in e]
        self.assertTrue(attributed)
        for knw in attributed:
            self.assertNotEqual(knw["peer"], knw["n"])
            self.assertIn(knw["peer"], sim.overlay.adjacency[knw["n"]])
            self.assertTrue(any(
                e["e"] == "snd" and e["n"] == knw["peer"] and
                e["to"] == knw["n"] and e["t"] <= knw["t"] for e in events),
                knw)

    def test_the_evidence_covers_the_transaction(self):
        # Under flooding every node gets the object itself before any wrapper
        # could tell it anything, so the wrapper form needs wrapper_only.
        for policy in ("flood", "wrapper_only"):
            sim, events = traced_line(relay_policy=policy)
            evidenced = [e for e in events if e["e"] == "knw" and "o" in e]
            self.assertTrue(evidenced)
            forms = set()
            for knw in evidenced:
                obj = sim.arena.objects[knw["o"]]
                self.assertTrue(sim.arena.coverage(obj) >> knw["tx"] & 1, knw)
                forms.add("own" if obj.leaf_index == knw["tx"] else "wrapper")
            # Both forms occur, or the attribution could not tell them apart.
            self.assertEqual(forms, {"own"} if policy == "flood"
                             else {"own", "wrapper"})
            for knw in events:
                if knw["e"] == "knw" and \
                        knw["to"] in ("settled", "body_only", "none"):
                    self.assertNotIn("o", knw, knw)

    def test_settlement_is_attributed_only_when_a_notice_carried_it(self):
        _sim, instant = traced_line(chain_notify="instant")
        for knw in instant:
            if knw["e"] == "knw" and knw["to"] == "settled":
                self.assertNotIn("peer", knw, knw)
        _sim, overlay = traced_line(chain_notify="overlay")
        settled = [e for e in overlay if e["e"] == "knw" and e["to"] == "settled"]
        self.assertTrue(settled)
        for knw in settled:
            # The proposer settled it by building the block; everyone else was
            # told by a neighbour.
            self.assertEqual("peer" in knw, knw["n"] != 0, knw)

    def test_an_attribution_at_arrival_is_kept_through_admission(self):
        """Verification queues between arrival and admission.

        The arrival makes the object `verifying` and the finished job makes it
        held. Both are the same delivery, so both carry the same peer; a path
        that attributes only the arrival would leave the tree's held form
        unattributed.
        """
        _sim, events = traced_line()
        by_pair = {}
        for knw in events:
            if knw["e"] == "knw":
                by_pair.setdefault((knw["n"], knw["tx"]), []).append(knw)
        checked = 0
        for series in by_pair.values():
            for first, second in zip(series, series[1:]):
                if first["to"] == "verifying" and "peer" in first and \
                        second["to"].startswith("held") and \
                        second.get("o") == first.get("o"):
                    self.assertEqual(second.get("peer"), first["peer"], second)
                    checked += 1
        self.assertGreater(checked, 0)

    def test_each_followed_transaction_spreads_as_a_tree_from_its_entry(self):
        for kwargs in (dict(), dict(relay_policy="wrapper_only",
                                    chain_notify="overlay")):
            with self.subTest(**kwargs):
                self.assert_spreads_as_a_tree(traced_line(**kwargs)[1])

    def assert_spreads_as_a_tree(self, events):
        """The first transition into knowing, per node, is the tree.

        Only the first: a node that held the transaction, evicted it and was
        told again did not learn it then, and a tree that took the later
        transition would put the entry node at the end of an edge.
        """
        entry = {e["tx"]: e["n"] for e in events if e["e"] == "arr"}
        first = {}
        for knw in events:
            if knw["e"] != "knw" or knw["to"] == "none":
                continue
            key = (knw["tx"], knw["n"])
            if key in first:
                continue
            first[key] = knw.get("peer")
            if "peer" not in knw:
                continue
            # The entry knew at birth; nothing can have told it first.
            self.assertNotEqual(knw["n"], entry[knw["tx"]], knw)
            # In trace order, so "already in the tree" is well defined: the
            # peer is the entry or a node that itself came to know earlier.
            reached = {entry[knw["tx"]]} | {
                node for (tx, node) in first if tx == knw["tx"]}
            self.assertIn(knw["peer"], reached, knw)
        widest = max(sum(1 for (tx, _n), peer in first.items()
                         if tx == t and peer is not None) for t in entry)
        self.assertGreaterEqual(widest, 2)

    def test_a_body_riding_on_a_wrapper_is_attributed_to_its_sender(self):
        """Two wrappers that name the same proof and differ in what they carry.

        The first brings the proof; the second, by reference, brings only the
        body. The class the second transition reaches rests on the proof it
        found, so the `o` rule alone would credit a peer for evidence it did
        not send -- the body it did send is the cause, and the rule says so.
        And a wrapper whose proof the CPU queue refuses still leaves its
        bodies behind, so that transition has a peer too.
        """
        from starksim.arena import Arena
        arena = Arena()
        leaf = arena.new_leaf(KIND_PQ, 0.0)
        aggregate = arena.new_aggregate((leaf,), 0.0, creator=0)
        bit = 1 << leaf.leaf_index
        node = Node(0, (1, 2), InstantNotifier(ChainState()))
        stream = io.StringIO()
        knowledge = KnowledgeTracer([node], arena, lambda: 5.0,
                                    Tracer(stream=stream))
        knowledge.follow(leaf.leaf_index)
        node.view.held[aggregate.id] = 0.0
        knowledge.mark(0, bit, peer=1, obj=aggregate.id, bodies=0)
        knowledge.flush("deliver")
        node.view.bodies |= bit
        knowledge.mark(0, bit, peer=2, obj=aggregate.id, bodies=bit)
        knowledge.flush("deliver")
        held, with_body = [e for e in parse_jsonl(stream.getvalue())
                           if e["e"] == "knw"]
        self.assertEqual((held["to"], held["peer"]), ("held_no_body", 1))
        self.assertEqual((with_body["to"], with_body["o"], with_body["peer"]),
                         ("held_and_body", aggregate.id, 2))
        # Refused proof, kept body: body_only, and the peer that sent it.
        other = Node(0, (1,), InstantNotifier(ChainState()))
        stream = io.StringIO()
        knowledge = KnowledgeTracer([other], arena, lambda: 6.0,
                                    Tracer(stream=stream))
        knowledge.follow(leaf.leaf_index)
        other.view.bodies |= bit
        knowledge.mark(0, bit, peer=1, obj=aggregate.id, bodies=bit)
        knowledge.flush("deliver")
        [only] = [e for e in parse_jsonl(stream.getvalue()) if e["e"] == "knw"]
        self.assertEqual((only["to"], only.get("o"), only["peer"]),
                         ("body_only", None, 1))

    def test_a_refused_wrapper_leaves_bodies_the_trace_attributes(self):
        """The same rule through the delivery path rather than `mark` by hand.

        Node 0's one-job CPU queue is full, so the wrapper node 1 sends is
        refused; its proof is abandoned and its body kept, and the transition
        that body causes carries node 1. This is what protects the wiring
        that hands a message's body mask to the tracer: with that mask
        zeroed, the transition has no peer.
        """
        from starksim.engine import Job
        from starksim.payload import (SizeBreakdown, WireMessage, WrapperCore,
                                      WrapperPayload)
        stream = io.StringIO()
        sim = NetworkSim(two_node_overlay(10.0), seed=1, proposer_id=0,
                         costs=CostTable(verify_recv=50.0), cpu_queue_jobs=1,
                         trace=stream)
        leaves = [sim.arena.new_leaf(KIND_PQ, 0.0) for _ in range(2)]
        for leaf in leaves:
            sim.knowledge.follow(leaf.leaf_index)
        aggregate = sim.arena.new_aggregate(leaves, 1.0, 1, claimed=0b11)
        for _ in range(2):   # one running, one waiting: the bound is met
            sim.cpu[0].submit(Job(50.0, on_done=lambda job: None, kind="busy"))
        core = WrapperCore(wrapper_id=1, proof_object_id=aggregate.id,
                           creator=1, tick=0, mode=1, tx_mask=0b11, dep_count=2)
        sim._on_deliver(sim.engine, WireMessage(
            sender=1, receiver=0,
            payload=WrapperPayload(core=core, body_mask=0b01,
                                   size=SizeBreakdown())))
        sim.knowledge.flush("deliver")
        transitions = {e["tx"]: e for e in parse_jsonl(stream.getvalue())
                       if e["e"] == "knw"}
        self.assertEqual(sim.dropped_messages, 1)
        with_body = transitions[leaves[0].leaf_index]
        self.assertEqual((with_body["to"], with_body["peer"]), ("body_only", 1))
        self.assertNotIn("o", with_body)
        self.assertNotIn(leaves[1].leaf_index, transitions)

    def test_a_delivery_is_not_blamed_for_an_eviction(self):
        """The rule itself, on one node with the state built by hand.

        One event admits leaf A from peer 1 and, to make room, evicts the
        aggregate that was the node's only evidence for leaf B. Two
        transitions result; only A's was caused by the delivery.
        """
        from starksim.arena import Arena
        arena = Arena()
        a, b = arena.new_leaf(KIND_PQ, 0.0), arena.new_leaf(KIND_PQ, 0.0)
        aggregate = arena.new_aggregate((b,), 0.0, creator=0)
        node = Node(0, (1,), InstantNotifier(ChainState()))
        stream = io.StringIO()
        tracer = Tracer(stream=stream)
        knowledge = KnowledgeTracer([node], arena, lambda: 5.0, tracer)
        knowledge.follow(a.leaf_index)
        knowledge.follow(b.leaf_index)
        node.view.held[aggregate.id] = 0.0
        knowledge.mark(0)
        knowledge.flush("setup")
        node.view.held[a.id] = 5.0
        del node.view.held[aggregate.id]
        knowledge.mark(0, peer=1, obj=a.id)
        knowledge.flush("deliver")
        transitions = {e["tx"]: e for e in parse_jsonl(stream.getvalue())
                       if e["e"] == "knw" and e["t"] == 5.0}
        self.assertEqual(transitions[a.leaf_index]["to"], "held_no_body")
        self.assertEqual(transitions[a.leaf_index]["o"], a.id)
        self.assertEqual(transitions[a.leaf_index]["peer"], 1)
        self.assertEqual(transitions[b.leaf_index]["to"], "none")
        self.assertNotIn("o", transitions[b.leaf_index])
        self.assertNotIn("peer", transitions[b.leaf_index])
