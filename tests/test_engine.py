#!/usr/bin/env python3
"""Calendar ordering, server contracts, queueing, and cancellation.

The hand-computed fixtures are the primary correctness tests -- they pin exact
start, finish and wait times job by job. The M/D/1 check is secondary: it
confirms that the queue behaves like an M/D/1 system in aggregate.
"""

import random
import unittest

from starksim.engine import EV_SERVICE_DONE, Engine, HeapCalendar, Job


class TestCalendarOrdering(unittest.TestCase):
    def test_pops_in_time_then_sequence_order(self):
        calendar = HeapCalendar()
        calendar.push(10.0, "b", "second-at-10")
        calendar.push(5.0, "a", "only-at-5")
        calendar.push(10.0, "c", "third-at-10")
        order = []
        while len(calendar):
            t, _seq, _kind, payload = calendar.pop()
            order.append((t, payload))
        self.assertEqual(order, [(5.0, "only-at-5"),
                                 (10.0, "second-at-10"),
                                 (10.0, "third-at-10")])

    def test_peek_time_and_len(self):
        calendar = HeapCalendar()
        self.assertIsNone(calendar.peek_time())
        self.assertEqual(len(calendar), 0)
        calendar.push(3.0, "k", None)
        calendar.push(1.0, "k", None)
        self.assertEqual(calendar.peek_time(), 1.0)
        self.assertEqual(len(calendar), 2)

    def test_scheduling_into_the_past_is_rejected(self):
        engine = Engine()
        engine.schedule_at(5.0, "noop")
        engine.on("noop", lambda e, p: None)
        engine.run()
        self.assertEqual(engine.now, 5.0)
        with self.assertRaises(ValueError):
            engine.schedule_at(4.0, "noop")

    def test_unregistered_kind_raises(self):
        engine = Engine()
        engine.schedule_at(1.0, "mystery")
        with self.assertRaises(KeyError):
            engine.run()


class TestHandComputedQueue(unittest.TestCase):
    """Exact expected timings, worked out by hand, one job at a time."""

    def _run_schedule(self, submissions, engine=None):
        # submissions: list of (submit_time, duration). Returns the jobs, each
        # carrying its own started_at, plus (server, engine).
        if engine is None:
            engine = Engine()
        server = engine.new_server("cpu")
        jobs = []
        finished = []

        def submit_handler(eng, job):
            server.submit(job)

        engine.on("submit", submit_handler)
        for submit_time, duration in submissions:
            job = Job(duration, on_done=finished.append)
            jobs.append(job)
            engine.schedule_at(submit_time, "submit", job)
        engine.run()
        return jobs, finished, server

    def test_constant_service_with_queueing_and_an_idle_gap(self):
        # Service time 10 throughout:
        #   j0 submitted  0 -> starts   0, done  10, wait  0
        #   j1 submitted  5 -> starts  10, done  20, wait  5   (j0 in service)
        #   j2 submitted  6 -> starts  20, done  30, wait 14   (j0, then j1)
        #   j3 submitted100 -> starts 100, done 110, wait  0   (server idle)
        jobs, finished, server = self._run_schedule(
            [(0.0, 10.0), (5.0, 10.0), (6.0, 10.0), (100.0, 10.0)])
        self.assertEqual([job.started_at for job in jobs],
                         [0.0, 10.0, 20.0, 100.0])
        self.assertEqual([job.wait() for job in jobs], [0.0, 5.0, 14.0, 0.0])
        self.assertEqual(finished, jobs)              # completion order is FIFO
        self.assertEqual(server.served, 4)
        self.assertEqual(server.max_waiting, 2)       # j1 and j2 both queued
        self.assertAlmostEqual(server.mean_wait(), 19.0 / 4)
        # Busy for four spans of 10 within a 110-long run.
        self.assertAlmostEqual(server.busy_time_at(110.0), 40.0)
        self.assertAlmostEqual(server.busy_time_at(110.0) / 110.0, 40.0 / 110.0)

    def test_variable_service_times(self):
        #   j0 submitted 0, D=10 -> starts  0, done 10, wait  0
        #   j1 submitted 1, D= 3 -> starts 10, done 13, wait  9
        #   j2 submitted 2, D= 7 -> starts 13, done 20, wait 11
        jobs, _finished, server = self._run_schedule(
            [(0.0, 10.0), (1.0, 3.0), (2.0, 7.0)])
        self.assertEqual([job.started_at for job in jobs], [0.0, 10.0, 13.0])
        self.assertEqual([job.wait() for job in jobs], [0.0, 9.0, 11.0])
        self.assertAlmostEqual(server.busy_time_at(20.0), 20.0)   # no idle time
        self.assertAlmostEqual(server.service_sum, 20.0)


class TestServerContract(unittest.TestCase):
    def test_busy_with_an_empty_queue(self):
        # The contract's central point: current_job is not in `waiting`, so a
        # busy server can have queue depth zero.
        engine = Engine()
        server = engine.new_server("cpu")
        server.submit(Job(5.0))
        self.assertTrue(server.is_busy())
        self.assertEqual(server.queue_depth(), 0)
        self.assertEqual(server.jobs_in_system(), 1)
        server.assert_invariants()

    def test_queue_depth_excludes_the_job_in_service(self):
        engine = Engine()
        server = engine.new_server("cpu")
        server.submit(Job(5.0))
        server.submit(Job(5.0))
        server.submit(Job(5.0))
        self.assertEqual(server.queue_depth(), 2)
        self.assertEqual(server.jobs_in_system(), 3)

    def test_idle_server_holds_no_waiting_work(self):
        engine = Engine()
        server = engine.new_server("cpu")
        for _ in range(3):
            server.submit(Job(5.0))
        engine.run()
        self.assertFalse(server.is_busy())
        self.assertEqual(server.queue_depth(), 0)
        server.assert_invariants()

    def test_callback_runs_before_the_next_job_starts(self):
        engine = Engine()
        server = engine.new_server("cpu")
        events = []

        def note_done(job):
            # At this instant the finished job is out of service and the next
            # one has not started yet.
            events.append(("done", job.tag, server.current_job))

        first = Job(10.0, on_done=note_done, tag="first")
        second = Job(10.0, on_done=note_done, tag="second")
        server.submit(first)
        server.submit(second)
        engine.run()
        self.assertEqual([e[0:2] for e in events],
                         [("done", "first"), ("done", "second")])
        self.assertEqual([e[2] for e in events], [None, None])

    def test_work_submitted_by_a_callback_queues_behind_waiting_work(self):
        # During a completion callback current_job is already None, so a naive
        # idle test would let the callback's own job overtake work that was
        # queued before it. FIFO must hold.
        engine = Engine()
        server = engine.new_server("cpu")
        order = []

        def on_first_done(job):
            order.append(job.tag)
            server.submit(Job(1.0, on_done=lambda j: order.append(j.tag),
                              tag="from-callback"))

        server.submit(Job(10.0, on_done=on_first_done, tag="first"))
        server.submit(Job(1.0, on_done=lambda j: order.append(j.tag),
                         tag="already-waiting"))
        engine.run()
        self.assertEqual(order, ["first", "already-waiting", "from-callback"])

    def test_zero_duration_job_completes_without_reordering(self):
        # A zero-duration job finishes at the current timestamp, but after any
        # event already scheduled for that timestamp.
        engine = Engine()
        server = engine.new_server("cpu")
        order = []
        engine.on("marker", lambda e, p: order.append(p))
        engine.schedule_at(0.0, "marker", "marker-scheduled-first")
        server.submit(Job(0.0, on_done=lambda j: order.append("zero-job")))
        engine.run()
        self.assertEqual(order, ["marker-scheduled-first", "zero-job"])
        self.assertEqual(engine.now, 0.0)
        self.assertAlmostEqual(server.busy_time_at(0.0), 0.0)

    def test_negative_duration_rejected(self):
        with self.assertRaises(ValueError):
            Job(-1.0)

    def test_utilisation_over_a_window_is_integrated_not_sampled(self):
        # Busy 0->10 and 20->30. Over [0, 40] that is 20/40, and a sample of
        # the queue length at t=40 (zero) would say nothing about it. The
        # counter is sampled as the clock passes each boundary, which is how a
        # metrics layer has to use it.
        engine = Engine()
        server = engine.new_server("cpu")
        engine.on("submit", lambda e, job: server.submit(job))
        engine.schedule_at(0.0, "submit", Job(10.0))
        engine.schedule_at(20.0, "submit", Job(10.0))
        engine.run(until=0.0)
        at_window_start = server.busy_time_at(0.0)
        engine.run(until=40.0)
        at_window_end = server.busy_time_at(40.0)
        busy_in_window = at_window_end - at_window_start
        self.assertAlmostEqual(busy_in_window, 20.0)
        self.assertAlmostEqual(busy_in_window / 40.0, 0.5)

    def test_busy_time_at_rejects_a_future_instant(self):
        # Extrapolating the in-progress span would report a one-unit job as a
        # hundred units of busy time.
        engine = Engine()
        server = engine.new_server("cpu")
        server.submit(Job(1.0))
        with self.assertRaises(ValueError):
            server.busy_time_at(100.0)

    def test_busy_time_at_rejects_a_past_instant(self):
        # Reading the counter retroactively would silently return the total so
        # far, which is not the answer to the question asked.
        engine = Engine()
        server = engine.new_server("cpu")
        server.submit(Job(10.0))
        engine.run()
        with self.assertRaises(ValueError):
            server.busy_time_at(0.0)


class TestRunHorizon(unittest.TestCase):
    def test_max_events_does_not_jump_the_clock_past_pending_events(self):
        # Stopping on the event limit leaves events inside the window. Advancing
        # `now` to the horizon anyway would make the next pop move time
        # backwards.
        engine = Engine()
        seen = []
        engine.on("tick", lambda e, p: seen.append((e.now, p)))
        engine.schedule_at(1.0, "tick", "first")
        engine.schedule_at(2.0, "tick", "second")
        engine.run(until=10.0, max_events=1)
        self.assertEqual(engine.now, 1.0)          # not 10.0
        engine.run(until=10.0)
        self.assertEqual([t for t, _ in seen], [1.0, 2.0])
        self.assertEqual(engine.now, 10.0)         # horizon, now legitimately

    def test_horizon_reached_advances_the_clock(self):
        # Nothing left before the horizon: advancing is correct, so a window
        # closing at `until` measures the full window.
        engine = Engine()
        engine.on("tick", lambda e, p: None)
        engine.schedule_at(1.0, "tick")
        engine.run(until=50.0)
        self.assertEqual(engine.now, 50.0)

    def test_drained_calendar_without_horizon_stays_at_last_event(self):
        engine = Engine()
        engine.on("tick", lambda e, p: None)
        engine.schedule_at(7.0, "tick")
        engine.run()
        self.assertEqual(engine.now, 7.0)

    def test_time_never_runs_backwards_across_stepped_runs(self):
        engine = Engine()
        stamps = []
        engine.on("tick", lambda e, p: stamps.append(e.now))
        for index in range(20):
            engine.schedule_at(float(index), "tick")
        while engine.run(until=19.0, max_events=1):
            pass
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(stamps), 20)


class TestCancellation(unittest.TestCase):
    def test_cancels_waiting_and_spares_work_in_service(self):
        engine = Engine()
        server = engine.new_server("cpu")
        done = []
        in_service = Job(10.0, on_done=lambda j: done.append(j.tag),
                         tag="in-service", generation=1)
        stale_a = Job(10.0, on_done=lambda j: done.append(j.tag),
                      tag="stale-a", generation=1)
        fresh = Job(10.0, on_done=lambda j: done.append(j.tag),
                    tag="fresh", generation=2)
        stale_b = Job(10.0, on_done=lambda j: done.append(j.tag),
                      tag="stale-b", generation=1)
        for job in (in_service, stale_a, fresh, stale_b):
            server.submit(job)

        dropped = server.cancel_waiting(lambda job: job.generation == 1)

        # The generation-1 job already in service is untouched: work in service
        # is not preemptible.
        self.assertEqual([job.tag for job in dropped], ["stale-a", "stale-b"])
        self.assertIs(server.current_job, in_service)
        self.assertEqual(server.queue_depth(), 1)
        self.assertEqual(server.cancelled, 2)
        engine.run()
        self.assertEqual(done, ["in-service", "fresh"])
        self.assertEqual(server.served, 2)

    def test_cancel_preserves_order_of_survivors(self):
        engine = Engine()
        server = engine.new_server("cpu")
        done = []
        server.submit(Job(1.0, tag="head"))
        for index in range(6):
            generation = index % 2
            server.submit(Job(1.0, on_done=lambda j: done.append(j.tag),
                              tag=f"job{index}", generation=generation))
        server.cancel_waiting(lambda job: job.generation == 0)
        engine.run()
        self.assertEqual(done, ["job1", "job3", "job5"])

    def test_cancel_on_empty_queue_is_a_noop(self):
        engine = Engine()
        server = engine.new_server("cpu")
        self.assertEqual(server.cancel_waiting(lambda job: True), [])
        server.submit(Job(1.0))
        self.assertEqual(server.cancel_waiting(lambda job: True), [])
        self.assertTrue(server.is_busy())


class TestInvariantsUnderRandomLoad(unittest.TestCase):
    def test_invariants_hold_at_every_event(self):
        engine = Engine()
        server = engine.new_server("cpu")
        rng = random.Random("engine-invariants")

        def submit_handler(eng, _payload):
            server.submit(Job(rng.uniform(0.0, 3.0)))
            eng.assert_invariants()

        engine.on("submit", submit_handler)
        t = 0.0
        for _ in range(500):
            t += rng.expovariate(0.5)
            engine.schedule_at(t, "submit")
        # Step one event at a time so the invariants are checked between all of
        # them, not only after submissions.
        while engine.run(max_events=1):
            engine.assert_invariants()
        self.assertEqual(server.served, 500)
        self.assertFalse(server.is_busy())


class TestMD1Convergence(unittest.TestCase):
    """M/D/1: Poisson arrivals, deterministic service, one server.

    Parameters declared up front, not tuned to fit:

        service time D  = 1.0
        offered loads   = 0.5 and 0.8   (lambda = rho / D)
        jobs per run    = 200_000
        warm-up         = first 20_000 jobs discarded (10%)
        tolerance       = 5% relative
        seed            = fixed per load

    The mean wait in queue for M/D/1 is the Pollaczek-Khinchine result
    specialised to zero service variance:

        Wq = rho * D / (2 * (1 - rho))

    which is half the M/M/1 wait -- deterministic service has no service-time
    variability to inflate the queue. At rho = 0.8 that is 2.0, at rho = 0.5
    it is 0.5.
    """

    SERVICE_TIME = 1.0
    JOBS = 200_000
    WARMUP = 20_000
    TOLERANCE = 0.05

    def _simulate(self, rho, seed):
        arrival_rate = rho / self.SERVICE_TIME
        engine = Engine()
        server = engine.new_server("cpu")
        rng = random.Random(seed)
        waits = []

        def submit_handler(eng, index):
            job = Job(self.SERVICE_TIME, on_done=None)
            server.submit(job)
            waits.append(job)
            if index + 1 < self.JOBS:
                eng.schedule(rng.expovariate(arrival_rate), "arrive", index + 1)

        engine.on("arrive", submit_handler)
        engine.schedule_at(0.0, "arrive", 0)
        engine.run()
        # Discard the warm-up transient: the queue starts empty, so early jobs
        # wait less than the steady-state mean.
        measured = [job.wait() for job in waits[self.WARMUP:]]
        return sum(measured) / len(measured), server

    def _simulate_with_clock(self, rho, seed):
        mean_wait, server = self._simulate(rho, seed)
        return mean_wait, server, server.engine.now

    def test_mean_wait_matches_theory(self):
        for rho, seed in ((0.5, "md1-rho-050"), (0.8, "md1-rho-080")):
            with self.subTest(rho=rho):
                observed, server = self._simulate(rho, seed)
                expected = rho * self.SERVICE_TIME / (2.0 * (1.0 - rho))
                relative_error = abs(observed - expected) / expected
                self.assertLess(
                    relative_error, self.TOLERANCE,
                    f"rho={rho}: mean wait {observed:.4f} vs theory "
                    f"{expected:.4f} ({100 * relative_error:.2f}% off)")
                self.assertEqual(server.served, self.JOBS)

    def test_utilisation_matches_offered_load(self):
        # Utilisation is busy time over elapsed time. For a stable queue it must
        # converge on rho whatever the queue is doing: the server is busy
        # exactly as often as work arrives for it.
        rho = 0.8
        _observed_mean, server, elapsed = self._simulate_with_clock(
            rho, "md1-rho-080")
        utilisation = server.busy_time / elapsed
        self.assertLess(abs(utilisation - rho) / rho, self.TOLERANCE,
                        f"utilisation {utilisation:.4f} vs offered {rho}")


if __name__ == "__main__":
    unittest.main()


class TestSealingMidRun(unittest.TestCase):
    """Sealing is a promise that nothing follows the terminal record."""

    def test_a_handler_that_seals_stops_the_run(self):
        seen = []
        engine = Engine()
        engine.on("x", lambda e, payload: (seen.append(payload),
                                           e.seal() if payload == 1 else None))
        engine.schedule_at(1.0, "x", 1)
        engine.schedule_at(2.0, "x", 2)
        engine.run()
        self.assertEqual(seen, [1])

    def test_sealing_inside_a_completion_leaves_no_phantom_job(self):
        # The completion callback seals; the server must not then start waiting
        # work, whose scheduling the sealed engine refuses *after* current_job
        # has been set -- a server busy for ever with no completion to come.
        engine = Engine()
        server = engine.new_server("cpu")
        server.on_idle = lambda owner: owner.submit(Job(1.0))
        server.submit(Job(1.0, on_done=lambda job: engine.seal()))
        engine.run()
        self.assertIsNone(server.current_job)
        self.assertFalse(server.waiting)
