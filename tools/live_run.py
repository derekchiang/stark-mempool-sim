#!/usr/bin/env python3
"""
Run a simulation paced against the wall clock, for watching live in the viewer.

The engine deliberately knows nothing about wall time (`engine.py`): pacing
lives here, in the runner, which advances the simulation in slices and sleeps
whenever simulated time gets ahead of `wall elapsed x speed`. The trace is
flushed per line as it always is, so the viewer follows the file as it grows --
nothing on the viewer side is specific to this runner.

    python3 -m tools.live_run                     # real time, until Ctrl-C
    python3 -m tools.live_run --speed 0.25        # slow motion
    python3 -m tools.live_run --duration 120      # two minutes of sim time
    python3 serve.py                              # in another terminal

then open <http://127.0.0.1:8000/viewer.html>.

Scheduling is chunked: births, ticks and slots are scheduled one chunk ahead,
and the next chunk is added only when the clock reaches the previous horizon.
The chunk is a whole number of slots and of aggregation intervals, and top-up
happens exactly at the horizon, so the slot and tick grids stay aligned across
chunks -- `schedule_ticks` and `schedule_slots` chain from `engine.now`, and
calling them anywhere else would skew the grid by the difference.

Speed is simulated time over wall time: 1.0 is real time, 0.25 slow motion,
8.0 fast-forward. If the simulation cannot keep up with the requested speed it
simply lags and says so; it never skips.

Ctrl-C seals the trace as `cancelled`, which is the honest status for a run
stopped with work outstanding; a `--duration` run seals it as `complete`.
"""

import argparse
import signal
import sys
import time

from bench.common import (add_preset_argument, preset_settings,
                          random_regular_overlay)
from starksim.arena import KIND_PQ, KIND_PRIVACY
from starksim.costs import CostTable
from starksim.netsim import (CLOCKED_TRIGGERS, RELAY_POLICIES, WRAPPER_ECONOMIES,
                            NetworkSim)
from starksim.network import SizeModel, bytes_per_ms
from starksim.strategies import names

# One run() call per slice, and one `seg` record per run() call: slices much
# smaller than this would bloat the trace with segment boundaries, and much
# larger would make the viewer's live view visibly steppy.
SLICE_MS = 500.0
# How many emission rounds a node's send buffer holds by default. A wrapper
# emission puts one proof-sized copy in the queue *per peer*, so one round is
# `degree x recursive_proof` -- 2.1 MB at degree 8. A buffer smaller than that
# is not a congestion bound at all: every node overflows the moment it
# aggregates, whatever the load, and the drops report a network problem that is
# really a buffer sized below one unavoidable burst. This was the default's
# behaviour past degree 6, where 1000 nodes at degree 8 produced 54 000 drops in
# 24 s of a run with 2 tx/s offered. Two rounds so a node can hold the next
# emission while the last one is still draining.
EMISSION_ROUNDS_BUFFERED = 2
# How long to sleep when the simulation is ahead of the wall clock. Bounds the
# reaction time to the clock catching up; nothing else depends on it.
POLL_SECONDS = 0.1
REPORT_EVERY_SECONDS = 5.0


class LiveSchedule:
    """Keeps births, ticks and slots scheduled one chunk ahead of the clock.

    The horizon is the simulated time everything is scheduled up to. It only
    ever advances in whole chunks, and `extend` must be called when the engine
    clock stands exactly on it -- that is what keeps the slot grid aligned.
    """

    def __init__(self, sim, spacing, lifetime, chunk_slots):
        self.sim = sim
        self.spacing = spacing
        self.lifetime = lifetime
        self.chunk = chunk_slots * sim.slot_length
        ticks = self.chunk / sim.aggregation_interval
        if ticks != int(ticks):
            raise ValueError(
                f"a chunk of {self.chunk:.0f} ms is not a whole number of "
                f"aggregation intervals ({sim.aggregation_interval:.0f} ms); "
                "choose a slot length divisible by the interval")
        self.ticks_per_chunk = int(ticks)
        self.slots_per_chunk = chunk_slots
        self.horizon = 0.0
        self.births = 0

    def extend(self):
        """Schedule the next chunk. The engine clock must be at the horizon.

        A chunk covers `(horizon, next_horizon]` -- the *closed* end matters.
        A birth landing exactly on the next horizon must be scheduled in this
        chunk, before that horizon's slot fires: scheduled afterwards it would
        carry a later sequence number and lose the same-timestamp race it wins
        under up-front scheduling, so the boundary transaction could never
        make the block it precedes. Births go on the calendar before this
        chunk's slots for the same tie-break reason.
        """
        next_horizon = self.horizon + self.chunk
        while self.births * self.spacing <= next_horizon:
            when = self.births * self.spacing
            if when > self.horizon or when == 0.0:
                # Alternate kinds so both appear in the byte-per-kind series.
                if self.births % 3 == 0:
                    kind = KIND_PRIVACY
                else:
                    kind = KIND_PQ
                self.sim.schedule_birth(when, kind, lifetime=self.lifetime)
            self.births += 1
        # A clockless trigger folds when idle and holds enough; it has no
        # ticks to schedule, and the model refuses them rather than ignore
        # them. v1's configuration is one such.
        if self.sim.aggregation_trigger in CLOCKED_TRIGGERS:
            self.sim.schedule_ticks(self.ticks_per_chunk)
        self.sim.schedule_slots(self.slots_per_chunk)
        self.horizon = next_horizon


def run_live(sim, schedule, speed, duration_ms, out=sys.stdout,
             clock=time.monotonic, sleep=time.sleep):
    """Pace the simulation against the wall clock until done or interrupted.

    `clock` and `sleep` are injectable so a test can drive this without
    waiting; the defaults are the real thing. Returns the status the trace
    was sealed with.
    """
    schedule.extend()
    started = clock()
    last_report = started
    status = "complete"
    try:
        while True:
            wall_ms = (clock() - started) * 1000.0 * speed
            target = min(wall_ms, schedule.horizon)
            if duration_ms is not None:
                target = min(target, duration_ms)
            behind = target - sim.engine.now
            if behind >= SLICE_MS or target == schedule.horizon \
                    or target == duration_ms:
                if target > sim.engine.now:
                    sim.run(until=target)
            if duration_ms is not None and sim.engine.now >= duration_ms:
                break
            if sim.engine.now >= schedule.horizon:
                schedule.extend()
                continue
            now = clock()
            if now - last_report >= REPORT_EVERY_SECONDS:
                last_report = now
                report(sim, wall_ms, out)
            if behind < SLICE_MS:
                sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        status = "cancelled"
    sim.finish_trace(status)
    return status


def report(sim, wall_ms, out):
    # The lag between where the wall clock says the simulation should be and
    # where it is. Persistent growth means the machine cannot sustain this
    # speed; the run stays correct and merely falls behind the wall.
    lag = wall_ms - sim.engine.now
    if lag > 2 * SLICE_MS:
        pace = f"behind by {lag / 1000.0:.1f}s"
    else:
        pace = "keeping up"
    print(f"sim {sim.engine.now / 1000.0:8.1f}s  "
          f"slots {sim.slots_run:4d}  included {sim.chain.included_count():5d}  "
          f"drops {sim.dropped_messages:6d}  {pace}",
          file=out, flush=True)


def default_egress_bytes(degree, size_model):
    """A send buffer that holds `EMISSION_ROUNDS_BUFFERED` wrapper rounds.

    Derived rather than constant because the burst it has to absorb is set by
    the topology: doubling the degree doubles what one tick puts in the queue.
    """
    return EMISSION_ROUNDS_BUFFERED * degree * size_model.recursive_proof


DEFAULT_UPLINK_MBPS = 25.0


def resolved_settings(args, size_model):
    """The simulation settings and the header notes, in layers.

    The runner's defaults underneath, the preset over them, the explicit
    flags on top: a flag given on the command line wins, a flag left out
    takes the preset's value or the runner's own default. Every flag that
    names a setting a preset can also set must be reapplied here, after the
    preset -- capacity and the egress bound included, which `v1_equivalent`
    unbounds. The notes are derived from the settings that win, so the
    header cannot describe a bound the simulation does not enforce.
    """
    derived_egress = default_egress_bytes(args.degree, size_model)
    settings = dict(
        seed=args.seed, size_model=size_model,
        capacity=bytes_per_ms(DEFAULT_UPLINK_MBPS),
        costs=CostTable(verify_recv=2.0,
                        prove_recursive=lambda deps: 20.0),
        candidate_limit=args.candidate_limit or None,
        egress_queue_bytes=derived_egress,
        relay_policy="flood", wrapper_economy="full",
        supersede_policy="newest_if_covered",
        proposer_mode="rotating",
        trace_follow_rate=args.follow_rate,
        trace_counter_interval=args.counter_interval)
    settings.update(preset_settings(args))
    if args.uplink_mbps is not None:
        settings["capacity"] = bytes_per_ms(args.uplink_mbps)
    if args.egress_bytes is not None:
        settings["egress_queue_bytes"] = args.egress_bytes or None
    for flag in ("relay_policy", "wrapper_economy", "supersede_policy"):
        if getattr(args, flag) is not None:
            settings[flag] = getattr(args, flag)
    capacity = settings["capacity"]
    uplink_note = ("unbounded" if capacity is None
                   else f"{capacity / 125.0:g} Mbit/s")
    egress = settings["egress_queue_bytes"]
    if egress is None:
        egress_note = "unbounded"
    elif args.egress_bytes is None and egress == derived_egress:
        egress_note = f"{egress / 1e6:.1f} MB (2 rounds)"
    else:
        egress_note = f"{egress / 1e6:.1f} MB"
    return settings, uplink_note, egress_note


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speed", type=float, default=1.0,
                        help="simulated time over wall time; 1.0 is real time")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="simulated seconds to run; 0 runs until Ctrl-C")
    parser.add_argument("--nodes", type=int, default=40)
    parser.add_argument("--degree", type=int, default=6)
    parser.add_argument("--uplink-mbps", type=float, default=None,
                        help="uplink capacity; default 25, or the preset's")
    parser.add_argument("--latency", type=float, default=100.0)
    parser.add_argument("--tps", type=float, default=2.0,
                        help="transactions per second born; more is heavier load")
    parser.add_argument("--lifetime", type=float, default=30_000.0,
                        help="ms before a transaction expires; 0 for never")
    parser.add_argument("--egress-bytes", type=int, default=None,
                        help="send-buffer bound per node; 0 for unbounded, "
                             "omitted to hold two wrapper emission rounds")
    parser.add_argument("--candidate-limit", type=int, default=8,
                        help="held objects per node; 0 for unbounded")
    add_preset_argument(parser)
    parser.add_argument("--relay-policy", default=None,
                        choices=RELAY_POLICIES,
                        help="how raw objects move; wrapper_only never "
                             "ships a witness, so claims spread one "
                             "aggregation tick per hop")
    parser.add_argument("--wrapper-economy", default=None,
                        choices=WRAPPER_ECONOMIES,
                        help="how much a node spends on wrappers it "
                             "need not send")
    parser.add_argument("--supersede-policy", default=None,
                        choices=names("supersede"),
                        help="default newest_if_covered, or the preset's")
    parser.add_argument("--follow-rate", type=float, default=0.15,
                        help="fraction of transactions traced in full")
    parser.add_argument("--counter-interval", type=float, default=500.0,
                        help="ms between per-node counter samples. One `cnt` "
                             "record per node per sample, so the trace grows "
                             "with nodes/interval: at 1000 nodes and 500 ms "
                             "that is 2 000 records a second. Raising it "
                             "shrinks the trace proportionally and coarsens "
                             "the viewer's utilisation and queue series")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default="trace.jsonl")
    args = parser.parse_args()
    if args.speed <= 0:
        parser.error("--speed must be positive")
    if args.counter_interval <= 0:
        # The engine refuses a non-positive observation interval, and the
        # viewer's fullness and utilisation panels are built from these
        # samples. Refuse here, where the message can say so.
        parser.error("--counter-interval must be positive; the viewer's "
                     "utilisation and queue panels are built from it")

    # A plain `kill` must seal the trace the same way Ctrl-C does. Backgrounded
    # non-interactive shells set SIGINT to ignored, so for a runner meant to
    # sit for hours SIGTERM is the stop signal people will actually reach for
    # -- and unhandled it would end the process between two lines, leaving a
    # trace that reads as killed rather than cancelled.
    def on_terminate(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_terminate)

    overlay = random_regular_overlay(args.nodes, args.degree, args.latency,
                                     args.seed)
    lifetime = args.lifetime if args.lifetime > 0 else None
    duration_ms = args.duration * 1000.0 if args.duration > 0 else None
    # One size model, shared by the buffer derivation and the simulation, so
    # the bound provably refers to the payload sizes actually being queued.
    size_model = SizeModel()
    settings, uplink_note, egress_note = resolved_settings(args, size_model)

    with open(args.out, "w") as stream:
        settings["trace"] = stream
        sim = NetworkSim(overlay, **settings)
        schedule = LiveSchedule(sim, spacing=1000.0 / args.tps,
                                lifetime=lifetime, chunk_slots=4)
        print(f"nodes={args.nodes} degree={args.degree} "
              f"uplink={uplink_note} "
              f"speed={args.speed}x "
              f"tps={args.tps:g} "
              f"relay={settings['relay_policy']} "
              f"economy={settings['wrapper_economy']} "
              f"egress={egress_note} "
              f"counters={args.counter_interval:.0f} ms -> {args.out}")
        print("serve it:  python3 serve.py    then open "
              "http://127.0.0.1:8000/viewer.html")
        status = run_live(sim, schedule, args.speed, duration_ms)
    print(f"sealed: {status} at sim {sim.engine.now / 1000.0:.1f}s, "
          f"{sim.slots_run} slots, "
          f"{sim.chain.included_count()} included")
    return 0


if __name__ == "__main__":
    sys.exit(main())
