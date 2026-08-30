#!/usr/bin/env python3
"""
The event trace: what happened, written so it can be replayed and re-analysed.

Two things shape this. First, the acceptance criterion: per-node propagation and
utilisation series must be reconstructable **from the trace alone**, so the trace
carries admission events and periodic cumulative counters rather than
pre-computed rates -- a rate cannot be re-windowed after the fact, a counter can.

Second, volume. A run at ten thousand nodes offers on the order of `N x degree`
messages per tick, so an unsampled trace is hundreds of gigabytes. Sampling is
therefore designed in rather than bolted on, and every sampling decision is
recorded in the header so a reader knows what it is looking at: a trace that
quietly dropped nine tenths of its events would otherwise read as a complete
picture of a tenth of the traffic.

Written as JSONL, one object per line, flushed per line so a running simulation
can be followed.
"""

import json


# Above this many directed edges the topology is omitted from the header rather
# than written: at N*d = 200_000 the edge list is already over a megabyte of
# JSON, and a reader who needs hop distances at that scale needs a different
# mechanism, not a bigger header.
TOPOLOGY_EDGE_LIMIT = 200_000

# Bumped whenever an event's fields change meaning or disappear. A trace with no
# `trace_schema` predates versioning, which includes the one that carried the
# block proposer as `blk.n` -- a reader cannot feature-detect its way out of that,
# because the field it would look for is the one that moved.
SCHEMA_VERSION = 3


# Events that describe a single message. These are the ones that scale with
# N x degree, so they are what sampling applies to.
MESSAGE_EVENTS = frozenset(("snd",))


class Tracer:
    """Writes trace events, honouring a focus and a sampling rate."""

    __slots__ = ("stream", "focus", "sample", "written", "suppressed",
                 "counter_interval", "topology", "follow", "_counter")

    def __init__(self, stream=None, focus=None, sample=1,
                 counter_interval=None, topology=None, follow=None):
        if sample < 1:
            raise ValueError("sample must be >= 1")
        self.stream = stream
        # A node subset to trace. None means every node; a focus keeps the
        # trace small enough to read while staying exact for what it covers,
        # which is what makes it usable as a debugging tool rather than a
        # firehose.
        if focus is None:
            self.focus = None
        else:
            self.focus = frozenset(focus)
        self.sample = sample
        # How often `cnt` records are emitted. Recorded here rather than in the
        # variant identity for the same reason sampling and focus are: it
        # describes the observation, not the run, and two runs differing only in
        # it are the same run watched at different resolutions.
        self.counter_interval = counter_interval
        # The overlay's edges, or None when there are too many to be worth
        # writing. Kept out of the variant identity: a graph is what the run was
        # on, but a megabyte of it does not belong in a CSV column.
        self.topology = topology
        # How followed transactions were chosen. Without it a reader cannot tell
        # "no follow rate was set" from "the rate selected nothing" from
        # "the requested indices were never born" -- three different statements
        # that all look like an empty panel.
        self.follow = follow
        self.written = 0
        self.suppressed = 0
        self._counter = 0

    def enabled(self):
        return self.stream is not None

    def covers(self, node_id):
        if self.focus is None:
            return True
        return node_id in self.focus

    def emit(self, event, node_id=None, **fields):
        """Record one event, unless focus or sampling excludes it."""
        if self.stream is None:
            return False
        if node_id is not None and not self.covers(node_id):
            self.suppressed += 1
            return False
        if event in MESSAGE_EVENTS and self.sample > 1:
            self._counter += 1
            if self._counter % self.sample:
                self.suppressed += 1
                return False
        record = {"e": event}
        if node_id is not None:
            record["n"] = node_id
        record.update(fields)
        self.stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        # A running simulation must be followable through a block-buffered
        # file, so each complete record is flushed immediately.
        self.stream.flush()
        self.written += 1
        return True

    def header(self, variant):
        """The run's identity, plus what this trace does and does not contain.

        Sampling and focus go in the header because a reader cannot infer them:
        a sampled trace and a quiet network look identical from the inside.
        """
        if self.stream is None:
            return
        record = {"e": "cfg"}
        record.update(variant)
        record["trace_schema"] = SCHEMA_VERSION
        record["trace_sample"] = self.sample
        record["trace_counter_interval"] = self.counter_interval
        # "omitted" rather than an empty list, so a reader can tell a graph that
        # was too big to write from one that genuinely has no edges.
        if self.topology is None:
            record["topology"] = "omitted"
        else:
            record["topology"] = self.topology
        record["trace_follow"] = self.follow or {"mode": "none"}
        if self.focus is None:
            record["trace_focus"] = "all"
        else:
            record["trace_focus"] = sorted(self.focus)
        self.stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        # The header is the run identity. A live follower must see it before
        # the first event, including while a simulation is idle at startup.
        self.stream.flush()
        self.written += 1
