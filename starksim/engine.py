#!/usr/bin/env python3
"""
Discrete-event engine for the networked mempool model.

Domain-agnostic on purpose: this module knows about time, an ordered event
calendar, and servers that queue work. It knows nothing about mempools,
transactions, proofs or overlays -- those live in the modules above it.

Time is abstract. `Engine.now` is a float advanced only by popping events; there
is no wall clock, no sleeping, and no relationship to how long the process runs.
The engine is also unit-free -- it never says what a duration means -- and the
domain layer above fixes the unit to milliseconds. Two consequences
that shape the code below: nothing stops several events sharing a timestamp, or
a duration being zero, which is why the (time, sequence) tie-break is load
bearing rather than a detail; and because timestamps are floats compared
exactly, the order in which quantities are accumulated is part of the contract.

Two pieces:

- `Calendar` -- the ordered event queue. Events pop in (time, sequence) order,
  so events sharing a timestamp run in the order they were scheduled.
  `HeapCalendar` is the reference implementation; the interface exists so a
  timing-wheel calendar can replace it later without touching any caller.

- `Server` -- one service unit with a FIFO queue of waiting work. A server owns
  its queue and keeps at most one completion event in the calendar. This is what
  makes queueing delay *emergent*: nothing in the code works out how long a job
  waits, it falls out of arrival rate against service rate.

The server contract, stated precisely because vagueness here is expensive:

- `current_job` is NOT in `waiting`. A server is busy iff `current_job` is not
  None, so a busy server can have an empty queue.
- Queue depth means *waiting* jobs. Jobs in system is waiting + busy.
- Utilisation is busy time integrated over an interval, never a sample of the
  queue length at interval boundaries.
- A completion applies its result (the job's callback) *before* the next job
  starts.
- Waiting work can be cancelled; work in service cannot be preempted.
- A zero-duration job completes at the current timestamp without reordering
  anything already scheduled for that timestamp.
"""

import heapq
import itertools
from collections import deque


# The engine handles one event kind itself: a server finishing its current job.
# Domain modules register their own kinds (deliveries, ticks, slot boundaries).
EV_SERVICE_DONE = "service_done"


class Calendar:
    """Ordered event queue interface.

    Implementations must pop events in (time, sequence) order, where sequence
    is a monotonically increasing insertion counter. That tie-break is part of
    the semantics, not an implementation accident: it is what makes an event
    scheduled earlier for time t run before one scheduled later for the same t.
    """

    def push(self, t, kind, payload):
        raise NotImplementedError

    def pop(self):
        """Return (t, seq, kind, payload) for the earliest event."""
        raise NotImplementedError

    def peek_time(self):
        """Timestamp of the earliest event, or None when empty."""
        raise NotImplementedError

    def __len__(self):
        raise NotImplementedError


class HeapCalendar(Calendar):
    """Binary-heap calendar. The reference implementation.

    With servers holding their own queues, the live event count stays close to
    (busy servers + timers + messages in flight), so the heap's O(log n) is not
    obviously the bottleneck. A timing wheel goes behind this same interface if
    a profile ever says otherwise.
    """

    __slots__ = ("_heap", "_counter")

    def __init__(self):
        self._heap = []
        self._counter = itertools.count()

    def push(self, t, kind, payload):
        seq = next(self._counter)
        heapq.heappush(self._heap, (t, seq, kind, payload))
        return seq

    def pop(self):
        return heapq.heappop(self._heap)

    def peek_time(self):
        if not self._heap:
            return None
        return self._heap[0][0]

    def __len__(self):
        return len(self._heap)


class Job:
    """One unit of work for a server.

    The specification -- duration, callback, tag, generation -- is treated as
    immutable once submitted; only the timestamps are filled in as the job moves
    through the queue. `generation` exists so a domain policy can cancel work it
    has decided is stale (see `Server.cancel_waiting`).
    """

    __slots__ = ("duration", "on_done", "on_start", "kind", "tag",
                 "generation", "submitted_at", "started_at")

    def __init__(self, duration, on_done=None, tag=None, generation=0,
                 kind=None, on_start=None):
        if duration < 0:
            raise ValueError("job duration must be >= 0")
        self.duration = float(duration)
        self.on_done = on_done
        # Called when service *begins*. Some things have to be booked then
        # rather than at completion: work committed to an input is spent whether
        # or not the job finishes, so a cancelled or still-running job at the
        # end of a run has still consumed it.
        self.on_start = on_start
        # What kind of computation this is, so a server can report service time
        # per kind rather than one undifferentiated total.
        self.kind = kind
        self.tag = tag
        self.generation = generation
        self.submitted_at = None
        self.started_at = None

    def wait(self):
        """Time spent queued before service started."""
        return self.started_at - self.submitted_at


class Server:
    """A single service unit with its own FIFO queue.

    One outstanding completion event in the calendar at a time, never more: the
    queue lives here, not in the calendar.
    """

    __slots__ = ("name", "engine", "waiting", "current_job", "on_idle",
                 "busy_since", "busy_time", "served", "wait_sum",
                 "service_sum", "max_waiting", "cancelled",
                 "served_by_kind", "service_by_kind",
                 "queue_limit", "weigh", "queued_weight", "rank",
                 "dropped", "dropped_weight")

    def __init__(self, engine, name, queue_limit=None, weigh=None, rank=None):
        self.name = name
        self.engine = engine
        # A bound on what may *wait*, excluding the job in service, because the
        # thing in service has already left the buffer. `weigh` makes the bound
        # a weighted sum rather than a count -- bytes for a send buffer, jobs for
        # a work queue -- since those are the units each of them physically has.
        self.queue_limit = queue_limit
        self.weigh = weigh
        # `rank` makes service order a policy: None is FIFO; a callable makes
        # the server pick the waiting job with the lowest rank when it next
        # goes idle. Selection happens only at service start -- work in
        # progress is never preempted -- and ties stay FIFO, so a rank that
        # maps everything to one value *is* FIFO. Rank orders *service*, never
        # admission: a bounded queue still tail-drops an arriving job whatever
        # its rank, because push-out would need cancellation accounting and an
        # owner-notification path that does not exist. See [D20] in
        # `docs/history/networking-plan.md`.
        self.rank = rank
        self.queued_weight = 0
        self.dropped = 0
        self.dropped_weight = 0
        self.waiting = engine.new_queue()
        self.current_job = None
        # Set by an owner that has something to do when this server runs dry.
        self.on_idle = None
        # Busy-time accounting: `busy_time` is the closed total, `busy_since`
        # marks the start of the span in progress. Utilisation over [a, b] is
        # (busy_time_at(b) - busy_time_at(a)) / (b - a) -- an integral of the
        # busy indicator, which is why it is accumulated rather than sampled.
        self.busy_since = None
        self.busy_time = 0.0
        self.served = 0
        self.wait_sum = 0.0
        self.service_sum = 0.0
        self.max_waiting = 0
        self.cancelled = 0
        # Per-kind accounting, so "service times match the cost table" is
        # something a test can assert rather than something a script observes.
        self.served_by_kind = {}
        self.service_by_kind = {}

    # -- state ------------------------------------------------------------

    def is_busy(self):
        return self.current_job is not None

    def queue_depth(self):
        """Jobs waiting for service. Excludes the one in service."""
        return len(self.waiting)

    def jobs_in_system(self):
        if self.is_busy():
            return len(self.waiting) + 1
        return len(self.waiting)

    def busy_time_at(self, t):
        """Cumulative busy time up to t, including the span in progress.

        `t` must be exactly the current time. This is a running counter, not a
        query: utilisation over [a, b] comes from sampling it *as the clock
        passes* a and b and differencing the two readings.

        Neither direction is allowed to slide. A past instant cannot work
        because closed spans carry no record of when they closed. A future
        instant is worse than useless: it would assume the span in progress
        continues all the way to `t`, so a one-unit job started at zero would
        report a hundred units of busy time at `busy_time_at(100)`. Passing the
        current time makes the caller state which instant it believes it is
        sampling, and mismatches surface instead of quietly inventing load.
        """
        if t != self.engine.now:
            raise ValueError(
                f"busy_time_at({t}) but now={self.engine.now}; this counter "
                "must be sampled at the current time, as the clock passes each "
                "window boundary")
        total = self.busy_time
        if self.busy_since is not None:
            total += t - self.busy_since
        return total

    def mean_wait(self):
        if not self.served:
            return 0.0
        return self.wait_sum / self.served

    # -- submitting and serving -------------------------------------------

    def submit(self, job):
        """Queue a job. Returns False if the bound refused it.

        **Tail-drop**: the arriving job is the one refused, not the oldest
        waiting one. That is the choice a device driver makes and it is the
        conservative one to model -- head-drop would discard work already
        accounted for, and dropping by priority would need a priority the model
        does not have. An unbounded server never refuses anything and is the
        default.
        """
        weight = 1 if self.weigh is None else self.weigh(job)
        job.submitted_at = self.engine.now
        # Start immediately only if nothing is in service AND nothing is queued.
        # The second half matters during a completion callback: `current_job` is
        # already None there, so testing it alone would let work submitted by the
        # callback overtake jobs that were waiting before it.
        if self.current_job is None and not self.waiting:
            self._start(job)
            return True
        if (self.queue_limit is not None
                and self.queued_weight + weight > self.queue_limit):
            self.dropped += 1
            self.dropped_weight += weight
            return False
        self.waiting.append(job)
        self.queued_weight += weight
        if len(self.waiting) > self.max_waiting:
            self.max_waiting = len(self.waiting)
        return True

    def _start(self, job):
        now = self.engine.now
        job.started_at = now
        self.current_job = job
        self.busy_since = now
        if job.on_start is not None:
            job.on_start(job)
        # A zero-duration job lands at `now` with a fresh sequence number, so it
        # completes after everything already scheduled for this instant rather
        # than jumping ahead of it.
        self.engine.schedule_at(now + job.duration, EV_SERVICE_DONE, self)

    def _service_done(self):
        """Called by the engine when this server's completion event fires."""
        now = self.engine.now
        job = self.current_job
        self.current_job = None
        self.busy_time += now - self.busy_since
        self.busy_since = None
        self.served += 1
        self.wait_sum += job.wait()
        self.service_sum += job.duration
        if job.kind is not None:
            self.served_by_kind[job.kind] = (
                self.served_by_kind.get(job.kind, 0) + 1)
            self.service_by_kind[job.kind] = (
                self.service_by_kind.get(job.kind, 0.0) + job.duration)

        # Apply the result before starting the next job: the callback may
        # submit follow-up work, which must queue behind whatever is already
        # waiting rather than overtake it.
        if job.on_done is not None:
            job.on_done(job)
        if self.engine.sealed:
            # The completion sealed the run. Starting the next job would
            # mutate `current_job` and then fail to schedule its completion,
            # leaving a server that looks busy for ever; announcing idleness
            # would invite the same.
            return

        if self.current_job is None and self.waiting:
            following = self._take_next()
            self.queued_weight -= (1 if self.weigh is None
                                   else self.weigh(following))
            self._start(following)
        if self.current_job is None and not self.waiting and self.on_idle:
            # The server has run out of work. One place, because "this server
            # went idle" is a property of the server and not of whichever job
            # happened to be last: hanging the notice off individual callbacks
            # means every new kind of job, and every early return, is a way to
            # miss it.
            self.on_idle(self)

    def _take_next(self):
        """The next waiting job: FIFO, or the lowest rank when ranked.

        The scan keeps the *first* job at the minimum rank, so order within a
        rank class stays FIFO. Linear in queue length per service start, which
        is fine at the depths a bounded work queue reaches; a heap would buy
        nothing but ceremony here.
        """
        if self.rank is None:
            return self.waiting.popleft()
        best_index = 0
        best_rank = self.rank(self.waiting[0])
        for index in range(1, len(self.waiting)):
            job_rank = self.rank(self.waiting[index])
            if job_rank < best_rank:
                best_rank = job_rank
                best_index = index
        job = self.waiting[best_index]
        del self.waiting[best_index]
        return job

    # -- cancellation ------------------------------------------------------

    def cancel_waiting(self, predicate):
        """Drop waiting jobs matching `predicate`. Returns those dropped.

        Work already in service is never touched -- a proof being computed
        cannot be un-computed. A policy that needs to discard the result of an
        in-flight job checks the job's generation in its own callback.
        """
        if not self.waiting:
            return []
        kept = self.engine.new_queue()
        dropped = []
        for job in self.waiting:
            if predicate(job):
                dropped.append(job)
            else:
                kept.append(job)
        self.waiting = kept
        for job in dropped:
            self.queued_weight -= 1 if self.weigh is None else self.weigh(job)
        self.cancelled += len(dropped)
        return dropped

    # -- invariants --------------------------------------------------------

    def assert_invariants(self):
        # Busy is defined by current_job, not by queue contents.
        assert (self.busy_since is not None) == (self.current_job is not None)
        # The job in service is not also queued.
        if self.current_job is not None:
            for job in self.waiting:
                assert job is not self.current_job
        # An idle server never holds waiting work: it would have started it.
        if self.current_job is None:
            assert not self.waiting
        # The bound is on what waits, so the accounting must track the queue and
        # not drift from it -- a leak here would silently tighten or loosen it.
        if self.weigh is None:
            assert self.queued_weight == len(self.waiting)
        else:
            assert self.queued_weight == sum(
                self.weigh(job) for job in self.waiting)
        if self.queue_limit is not None:
            assert self.queued_weight <= self.queue_limit


class Engine:
    """Event loop over a calendar, with a handler per event kind."""

    __slots__ = ("calendar", "now", "handlers", "events_processed", "servers",
                 "observers", "observe_interval", "_observed_through",
                 "_observed_at", "post_event_hooks", "stop_reason", "sealed")

    def __init__(self, calendar=None):
        if calendar is None:
            calendar = HeapCalendar()
        self.calendar = calendar
        self.now = 0.0
        self.handlers = {EV_SERVICE_DONE: _service_done_handler}
        self.events_processed = 0
        self.servers = []
        # Observation is deliberately *not* a calendar event. Events sharing a
        # timestamp run in scheduling order, so a periodic observation event
        # would take a sequence number, interleave with domain events at the
        # same instant, and shift their relative order -- and it would count
        # towards `events_processed`, changing what a `max_events` run does.
        # Observers are therefore a separate mechanism that reads state and
        # cannot influence it.
        # Fired after a handler and every synchronous callback it made have
        # returned. That boundary is the only place a consistent state exists:
        # inside a handler the model passes through arrangements no other actor
        # could observe, and reporting them would invent transitions.
        # Why the last `run()` returned. A caller cannot infer it: a drained
        # calendar and a horizon reached look identical from outside, and they
        # mean different things for how far observation extends.
        # Once sealed, neither scheduling nor running is permitted. Guarding
        # only the domain wrapper was not enough: the engine is public, so
        # `sim.engine.run()` walked straight past it and appended events after
        # the trace had declared itself terminal.
        self.sealed = False
        self.stop_reason = None
        self.post_event_hooks = []
        self.observers = []
        self.observe_interval = None
        self._observed_through = None
        self._observed_at = None

    def after_event(self, callback):
        """Call `callback(kind)` once per event, after the handler returns."""
        self.post_event_hooks.append(callback)

    def observe_every(self, interval, callback):
        """Call `callback(t)` at each multiple of `interval` in simulated time.

        Fired *after* every domain event at or before that instant, so what an
        observer sees at time `b` is the state the model reached at `b` and not
        a moment part-way through it. Boundaries are global multiples of
        `interval`, not offsets from the start of a `run()` call, so two runs of
        the same model observed with the same interval land on the same grid and
        their series can be compared point for point.
        """
        if interval <= 0:
            raise ValueError("observation interval must be positive")
        if self.observe_interval is not None and interval != self.observe_interval:
            raise ValueError("one observation interval per engine")
        self.observe_interval = interval
        self.observers.append(callback)

    def observe_now(self, label=None):
        """Fire every observer at the current instant, off the grid.

        For segment starts and ends, which are not multiples of the interval but
        are the two boundaries a reader needs in order to reconstruct the first
        and last intervals of a run.
        """
        self._emit_observation(self.now, label)

    def _emit_observation(self, t, label):
        """One observation, never two at the same instant.

        Two records sharing a timestamp would divide by zero in the obvious
        reconstruction -- utilisation is a difference over elapsed time -- so a
        grid boundary that coincides with a segment boundary is dropped rather
        than duplicated. The segment label is the more informative of the two.
        """
        if self._observed_at is not None and t == self._observed_at:
            return
        self._observed_at = t
        for observer in self.observers:
            observer(t, label)

    def _observe_through(self, horizon):
        """Fire observers for every grid boundary strictly below `horizon`.

        Called with the *next* event's timestamp, so every boundary it fires has
        already had all its domain events processed: the calendar is ordered, so
        nothing at or before the boundary can still be pending.

        The clock advances to each boundary before observing, because a counter
        is read as the clock passes it -- `Server.busy_time_at` refuses any
        other time on purpose. That advance is legitimate: the boundary lies
        between the last event processed and the next one, so time has passed
        and no event can later claim an earlier timestamp.
        """
        if self.observe_interval is None or not self.observers:
            return
        interval = self.observe_interval
        if self._observed_through is None:
            index = 0
        else:
            index = int(self._observed_through / interval) + 1
        while True:
            boundary = index * interval
            if boundary >= horizon:
                return
            if boundary >= self.now:
                self.now = boundary
                self._emit_observation(boundary, None)
            self._observed_through = boundary
            index += 1

    def new_queue(self):
        """FIFO queue for a server. Here so the container is chosen once."""
        return deque()

    def new_server(self, name, queue_limit=None, weigh=None, rank=None):
        server = Server(self, name, queue_limit, weigh, rank)
        self.servers.append(server)
        return server

    def on(self, kind, handler):
        """Register the handler for an event kind: handler(engine, payload)."""
        self.handlers[kind] = handler

    def seal(self):
        """Refuse all further scheduling and running, permanently."""
        self.sealed = True

    def _require_unsealed(self, what):
        if self.sealed:
            raise ValueError(
                f"cannot {what}: this engine is sealed. Anything appended now "
                "would come after the record that says nothing more is coming")

    def schedule_at(self, t, kind, payload=None):
        self._require_unsealed("schedule")
        if t < self.now:
            raise ValueError(f"cannot schedule into the past: {t} < {self.now}")
        return self.calendar.push(t, kind, payload)

    def schedule(self, delay, kind, payload=None):
        return self.schedule_at(self.now + delay, kind, payload)

    def run(self, until=None, max_events=None):
        """Process events in order until the calendar drains or a limit hits.

        `until` is inclusive: events at exactly `until` run. Returns the number
        of events processed by this call.
        """
        self._require_unsealed("run")
        processed = 0
        stopped_on_limit = False
        self.stop_reason = "drained"
        self.observe_now("segment_start")
        while len(self.calendar):
            if max_events is not None and processed >= max_events:
                stopped_on_limit = True
                self.stop_reason = "max_events"
                break
            next_t = self.calendar.peek_time()
            if until is not None and next_t > until:
                self.stop_reason = "until"
                break
            self._observe_through(next_t)
            t, _seq, kind, payload = self.calendar.pop()
            self.now = t
            handler = self.handlers.get(kind)
            if handler is None:
                raise KeyError(f"no handler registered for event kind {kind!r}")
            handler(self, payload)
            processed += 1
            self.events_processed += 1
            if self.sealed:
                # A handler declared the run over. Dispatching what the
                # calendar still holds would put events after the terminal
                # record, which is exactly what sealing promises not to do --
                # and post-event hooks after it would observe a model no
                # reader can reconstruct.
                break
            for hook in self.post_event_hooks:
                hook(kind)
        # Advance to the horizon only when there is genuinely nothing left
        # before it -- the calendar drained, or the next event is beyond it. If
        # we stopped early on `max_events` there are still events *inside* the
        # window, and jumping the clock forward would make the next pop assign
        # an earlier timestamp: time would run backwards.
        if until is not None and not stopped_on_limit and self.now < until:
            # Observe the quiet tail *before* jumping the clock. Advancing to
            # the horizon first and observing after refuses every boundary below
            # it -- they are then in the past -- so a run whose calendar drains
            # early lost every grid boundary between the last event and `until`.
            self._observe_through(until)
            self.now = until
        # Grid boundaries up to and including the point the clock reached, then
        # the segment's own end. The explicit endpoint is required to
        # reconstruct the final partial interval.
        # Strictly below the endpoint, then the endpoint itself carries the
        # segment label. Firing the grid inclusively first would emit the
        # endpoint unlabelled and the labelled record would then be dropped as a
        # duplicate -- losing the more informative of the two.
        if not stopped_on_limit:
            self._observe_through(self.now)
        self.observe_now("segment_end")
        return processed

    def assert_invariants(self):
        for server in self.servers:
            server.assert_invariants()


def _service_done_handler(engine, server):
    server._service_done()
