import http.client
import json
import math
import os
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SOURCE_DIR not in sys.path:
    sys.path.insert(0, SOURCE_DIR)

try:
    import psycopg  # noqa: F401
except ModuleNotFoundError:
    psycopg_stub = types.ModuleType("psycopg")
    psycopg_stub.OperationalError = RuntimeError
    rows_stub = types.ModuleType("psycopg.rows")
    rows_stub.dict_row = object()
    sys.modules["psycopg"] = psycopg_stub
    sys.modules["psycopg.rows"] = rows_stub

import worker_health  # noqa: E402
import worker_prod  # noqa: E402
from worker_health import WorkerHealth  # noqa: E402

SECRET = "postgres://user:TOPSECRET@db.internal/prod"
SNAPSHOT_KEYS = {"ready", "reason", "initialized", "heartbeat_age_seconds"}
ALL_REASONS = {
    "STARTING", "RECOVERY_FAILED", "LOOP_EXCEPTION", "LOOP_NOT_ALIVE",
    "STALE_HEARTBEAT", "DB_UNAVAILABLE", "READY",
}


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeProc:
    """Stands in for subprocess.Popen; tracks launch/kill/reap."""

    def __init__(self, tracker, returncode=0, hang=False, work=0.0, clock=None,
                 advance=0.0):
        self.tracker = tracker
        self._rc = returncode
        self.hang = hang
        self.work = work
        self.clock = clock
        self.advance = advance
        self.killed = False
        self.reaped = False
        with tracker.lock:
            tracker.active += 1
            tracker.max_active = max(tracker.max_active, tracker.active)
            tracker.events.append("launch")

    def _reap(self):
        if not self.reaped:
            self.reaped = True
            with self.tracker.lock:
                self.tracker.active -= 1
                self.tracker.events.append("reap")

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            if timeout is None:
                raise AssertionError("unbounded wait on a live hung probe")
            raise subprocess.TimeoutExpired("probe", timeout)
        if self.work:
            time.sleep(self.work)
        if self.clock is not None:
            self.clock.advance(self.advance)
        self._reap()
        return -9 if self.killed else self._rc

    def kill(self):
        self.killed = True
        with self.tracker.lock:
            self.tracker.events.append("kill")


class Tracker:
    def __init__(self, factory=None):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.events = []
        self.launch_times = []
        self.procs = []
        self.factory = factory or (lambda n, t: FakeProc(t))

    def launch(self):
        with self.lock:
            n = len(self.procs)
            self.launch_times.append(time.monotonic())
        proc = self.factory(n, self)
        with self.lock:
            self.procs.append(proc)
        return proc


def wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def http_get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        return resp.status, raw, json.loads(raw)
    finally:
        conn.close()


class ServerCase(unittest.TestCase):
    def serve(self, snapshot_fn):
        server = worker_prod.start_health_server(snapshot_fn, port=0, host="127.0.0.1")
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        return server.server_address[1]


class WorkerHealthContractTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.health = WorkerHealth(clock=self.clock)

    def snap(self, loop_alive=True, db_ok=True, limit=900):
        return self.health.snapshot(
            loop_alive=loop_alive, db_ok=db_ok, max_silence_seconds=limit)

    def test_A_startup_without_tick_is_not_ready(self):
        self.health.mark_started()
        s = self.snap()
        self.assertEqual(s, {"ready": False, "reason": "STARTING",
                             "initialized": False, "heartbeat_age_seconds": None})

    def test_B_completed_iteration_alive_loop_fresh_db_is_ready(self):
        self.health.mark_started()
        self.health.mark_tick()
        self.clock.advance(3)
        s = self.snap()
        self.assertEqual(set(s), SNAPSHOT_KEYS)
        self.assertTrue(s["ready"])
        self.assertEqual(s["reason"], "READY")
        self.assertTrue(s["initialized"])
        self.assertEqual(s["heartbeat_age_seconds"], 3.0)

    def test_E_failures_are_not_ready_and_normalized(self):
        self.health.mark_started()
        self.health.mark_tick()
        for code, expected in (("RECOVERY_FAILED", "RECOVERY_FAILED"),
                               ("LOOP_EXCEPTION", "LOOP_EXCEPTION"),
                               ("boom " + SECRET, "LOOP_EXCEPTION"),
                               (None, "LOOP_EXCEPTION")):
            self.health.mark_failed(code)
            s = self.snap()
            self.assertFalse(s["ready"])
            self.assertEqual(s["reason"], expected)
            self.assertNotIn("TOPSECRET", json.dumps(s))

    def test_E_failure_before_first_tick_stays_starting(self):
        self.health.mark_started()
        self.health.mark_failed("RECOVERY_FAILED")
        s = self.snap()
        self.assertFalse(s["ready"])
        self.assertEqual(s["reason"], "STARTING")

    def test_Z_loop_not_alive_stale_and_threshold_boundary(self):
        self.health.mark_started()
        self.health.mark_tick()
        self.assertEqual(self.snap(loop_alive=False)["reason"], "LOOP_NOT_ALIVE")
        self.clock.advance(900)
        s = self.snap()
        self.assertTrue(s["ready"], "exactly at threshold is still fresh")
        self.assertEqual(s["heartbeat_age_seconds"], 900.0)
        self.clock.advance(0.001)
        s = self.snap()
        self.assertFalse(s["ready"])
        self.assertEqual(s["reason"], "STALE_HEARTBEAT")
        self.assertEqual(self.snap(db_ok=False)["reason"], "STALE_HEARTBEAT")

    def test_Z_work_shorter_than_threshold_stays_ready(self):
        self.health.mark_started()
        self.health.mark_tick()
        self.clock.advance(899)
        self.assertTrue(self.snap()["ready"])

    def test_precedence_failure_over_loop_over_stale_over_db(self):
        self.health.mark_started()
        self.health.mark_tick()
        self.clock.advance(5000)
        self.assertEqual(self.snap(loop_alive=False, db_ok=False)["reason"],
                         "LOOP_NOT_ALIVE")
        self.health.mark_failed("LOOP_EXCEPTION")
        self.assertEqual(self.snap(loop_alive=False, db_ok=False)["reason"],
                         "LOOP_EXCEPTION")
        self.health.mark_tick()
        self.assertEqual(self.snap(db_ok=False)["reason"], "DB_UNAVAILABLE")

    def test_K_tick_after_failure_recovers_but_needs_loop_and_db(self):
        self.health.mark_started()
        self.health.mark_tick()
        self.health.mark_failed("LOOP_EXCEPTION")
        self.assertFalse(self.snap()["ready"])
        self.health.mark_tick()
        self.assertTrue(self.snap()["ready"])
        self.assertEqual(self.snap(loop_alive=False)["reason"], "LOOP_NOT_ALIVE")
        self.assertEqual(self.snap(db_ok=False)["reason"], "DB_UNAVAILABLE")

    def test_mark_started_resets_everything(self):
        self.health.mark_tick()
        self.health.mark_failed("LOOP_EXCEPTION")
        self.health.mark_started()
        s = self.snap()
        self.assertEqual(s["reason"], "STARTING")
        self.assertIsNone(s["heartbeat_age_seconds"])

    def test_snapshot_does_not_mutate_or_do_io(self):
        self.health.mark_started()
        self.health.mark_tick()
        with patch("subprocess.Popen", side_effect=AssertionError("io")), \
                patch("socket.socket.connect", side_effect=AssertionError("io")):
            first = self.snap()
            second = self.snap()
        self.assertEqual(first, second)

    def test_M_invalid_threshold_rejected(self):
        for bad in (True, False, 0, 0.0, -1, -0.5, math.nan, math.inf, -math.inf,
                    "5", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.snap(limit=bad)

    def test_L_concurrent_snapshot_and_updates_are_consistent(self):
        health = WorkerHealth()
        stop = threading.Event()
        problems = []

        def writer():
            i = 0
            while not stop.is_set():
                i += 1
                (health.mark_started, health.mark_tick,
                 lambda: health.mark_failed("LOOP_EXCEPTION"),
                 health.mark_tick)[i % 4]()

        def reader():
            while not stop.is_set():
                s = health.snapshot(loop_alive=True, db_ok=True,
                                    max_silence_seconds=900)
                if set(s) != SNAPSHOT_KEYS or s["reason"] not in ALL_REASONS:
                    problems.append(("shape", s))
                if s["ready"] != (s["reason"] == "READY"):
                    problems.append(("ready", s))
                if s["initialized"] != (s["heartbeat_age_seconds"] is not None):
                    problems.append(("init/age", s))
                if s["heartbeat_age_seconds"] is not None and \
                        s["heartbeat_age_seconds"] < 0:
                    problems.append(("age", s))
                if (s["reason"] == "STARTING") == s["initialized"]:
                    problems.append(("starting", s))

        threads = [threading.Thread(target=writer) for _ in range(2)] + \
                  [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.4)
        stop.set()
        for t in threads:
            t.join(5)
        self.assertEqual(problems[:3], [])


class ConfigTest(unittest.TestCase):
    def test_default_and_valid(self):
        self.assertEqual(worker_prod.load_ready_max_silence({}), 900.0)
        self.assertEqual(
            worker_prod.load_ready_max_silence(
                {"WORKER_READY_MAX_SILENCE_SECONDS": "30.5"}), 30.5)

    def test_M_invalid_env_is_explicit_safe_error(self):
        for bad in ("abc", "nan", "inf", "-inf", "0", "-5", "", "TOPSECRET"):
            with self.assertRaises(worker_prod.ConfigError) as ctx:
                worker_prod.load_ready_max_silence(
                    {"WORKER_READY_MAX_SILENCE_SECONDS": bad})
            self.assertNotIn("TOPSECRET", str(ctx.exception))
        self.assertTrue(issubclass(worker_prod.ConfigError, ValueError))

    def test_invalid_env_stops_before_anything_starts(self):
        with patch.dict(os.environ, {"WORKER_READY_MAX_SILENCE_SECONDS": "nan"}), \
                patch.object(worker_prod, "start_health_server") as server, \
                patch.object(worker_prod, "DbProbeMonitor") as monitor:
            with self.assertRaises(worker_prod.ConfigError):
                worker_prod.serve_forever(max_iterations=1)
        server.assert_not_called()
        monitor.assert_not_called()

    def test_default_launcher_is_shell_free_with_discarded_output(self):
        with patch.object(worker_prod.subprocess, "Popen") as popen:
            worker_prod.launch_db_probe()
        args, kwargs = popen.call_args
        self.assertEqual(args[0][0], sys.executable)
        self.assertIsInstance(args[0], list)
        self.assertFalse(kwargs.get("shell"))
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["cwd"], SOURCE_DIR)


class DbProbeMonitorTest(unittest.TestCase):
    def make(self, factory, clock=None, **kw):
        tracker = Tracker(factory)
        monitor = worker_prod.DbProbeMonitor(
            launch=tracker.launch, clock=clock or FakeClock(), **kw)
        return monitor, tracker

    def test_G_no_result_means_not_ok(self):
        monitor, tracker = self.make(None)
        self.assertFalse(monitor.db_ok())
        self.assertEqual(tracker.procs, [])

    def test_G_age_boundary_is_15_seconds_from_completion(self):
        clock = FakeClock(100.0)
        monitor, _ = self.make(
            lambda n, t: FakeProc(t, clock=clock, advance=4.0), clock=clock)
        self.assertTrue(monitor.probe_once())
        self.assertEqual(clock.now, 104.0)
        clock.now = 119.0
        self.assertTrue(monitor.db_ok())
        clock.now = 119.001
        self.assertFalse(monitor.db_ok())

    def test_V_failure_right_after_success_invalidates_cache(self):
        clock = FakeClock()
        monitor, _ = self.make(
            lambda n, t: FakeProc(t, returncode=0 if n == 0 else 1), clock=clock)
        self.assertTrue(monitor.probe_once())
        self.assertTrue(monitor.db_ok())
        self.assertFalse(monitor.probe_once())
        self.assertFalse(monitor.db_ok())
        self.assertEqual(monitor.last_code, "DB_PROBE_FAILED")

    def test_V_launch_error_invalidates_cache(self):
        clock = FakeClock()
        calls = []

        def launch():
            calls.append(1)
            if len(calls) == 1:
                return FakeProc(Tracker())
            raise OSError(SECRET)

        monitor = worker_prod.DbProbeMonitor(launch=launch, clock=clock)
        self.assertTrue(monitor.probe_once())
        self.assertFalse(monitor.probe_once())
        self.assertFalse(monitor.db_ok())
        self.assertNotIn("TOPSECRET", monitor.last_code)

    def test_D_timeout_kills_reaps_and_invalidates(self):
        monitor, tracker = self.make(
            lambda n, t: FakeProc(t, returncode=0 if n == 0 else 0, hang=n == 1))
        self.assertTrue(monitor.probe_once())
        self.assertFalse(monitor.probe_once())
        self.assertFalse(monitor.db_ok())
        self.assertEqual(tracker.events, ["launch", "reap", "launch", "kill", "reap"])
        self.assertTrue(tracker.procs[1].killed and tracker.procs[1].reaped)
        self.assertEqual(monitor.last_code, "DB_PROBE_TIMEOUT")

    def test_D_deadline_passed_to_wait_is_5_seconds(self):
        seen = []
        tracker = Tracker()

        class Proc(FakeProc):
            def wait(self, timeout=None):
                seen.append(timeout)
                return super().wait(timeout)

        tracker.factory = lambda n, t: Proc(t)
        monitor = worker_prod.DbProbeMonitor(
            launch=tracker.launch, clock=FakeClock(), deadline_clock=FakeClock())
        monitor.probe_once()
        self.assertEqual(seen, [5.0])

    def _slow_launch_monitor(self, launch_seconds, hang):
        deadline_clock = FakeClock(50.0)
        ttl_clock = FakeClock()
        seen = []
        tracker = Tracker()

        class Proc(FakeProc):
            def wait(self, timeout=None):
                seen.append(timeout)
                return super().wait(timeout)

        def factory(n, t):
            if n == 1:
                deadline_clock.advance(launch_seconds)
            return Proc(t, hang=hang and n == 1)

        tracker.factory = factory
        monitor = worker_prod.DbProbeMonitor(
            launch=tracker.launch, clock=ttl_clock, deadline_clock=deadline_clock)
        return monitor, tracker, seen

    def test_D_launch_time_counts_against_5_second_deadline(self):
        monitor, tracker, seen = self._slow_launch_monitor(3.5, hang=True)
        self.assertTrue(monitor.probe_once())
        self.assertTrue(monitor.db_ok())
        self.assertFalse(monitor.probe_once())
        self.assertEqual(seen[0], 5.0)
        self.assertEqual(seen[1], 1.5, "wait gets only the remaining time")
        self.assertFalse(monitor.db_ok(), "timeout invalidates earlier success")
        self.assertEqual(monitor.last_code, "DB_PROBE_TIMEOUT")
        self.assertEqual(tracker.events[-2:], ["kill", "reap"])
        self.assertEqual(seen[2:], [None], "only the post-kill reap is unbounded")

    def test_D_launch_consuming_whole_deadline_is_timeout_without_wait(self):
        for launch_seconds in (5.0, 7.0):
            monitor, tracker, seen = self._slow_launch_monitor(
                launch_seconds, hang=False)
            self.assertTrue(monitor.probe_once())
            self.assertFalse(monitor.probe_once())
            self.assertFalse(monitor.db_ok())
            self.assertEqual(monitor.last_code, "DB_PROBE_TIMEOUT")
            self.assertEqual(seen[0], 5.0)
            self.assertEqual(seen[1:], [None], "no bounded wait; kill then reap")
            self.assertEqual(tracker.events[-2:], ["kill", "reap"])
            self.assertEqual(tracker.active, 0)

    def test_D_real_hung_child_is_killed_and_reaped(self):
        children = []

        def launch():
            child = subprocess.Popen(
                [sys.executable, "-c",
                 "import sys,time; print('LEAK_OUT'); "
                 "sys.stderr.write('LEAK_ERR'); time.sleep(60)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            children.append(child)
            return child

        monitor = worker_prod.DbProbeMonitor(launch=launch, timeout=1.0)
        started = time.monotonic()
        self.assertFalse(monitor.probe_once())
        self.assertLess(time.monotonic() - started, 20)
        self.assertIsNotNone(children[0].poll(), "child must be reaped")
        self.assertFalse(monitor.db_ok())

    def test_D_real_child_exit_zero_is_success_nonzero_is_failure(self):
        codes = iter(["0", "3"])

        def launch():
            return subprocess.Popen(
                [sys.executable, "-c", f"import sys; sys.exit({next(codes)})"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        monitor = worker_prod.DbProbeMonitor(launch=launch, timeout=20)
        self.assertTrue(monitor.probe_once())
        self.assertTrue(monitor.db_ok())
        self.assertFalse(monitor.probe_once())
        self.assertFalse(monitor.db_ok())

    def test_D_never_overlaps_and_next_attempt_follows_cleanup(self):
        monitor, tracker = self.make(
            lambda n, t: FakeProc(t, hang=n % 2 == 0, work=0.01),
            period=0.02, timeout=0.05)
        monitor.start()
        self.assertTrue(wait_until(lambda: len(tracker.procs) >= 5))
        monitor.stop()
        self.assertEqual(tracker.max_active, 1)
        events = tracker.events
        for i, name in enumerate(events):
            if name == "launch" and i:
                self.assertEqual(events[i - 1], "reap")
        self.assertIn("kill", events)

    def test_first_probe_at_start_and_period_counts_from_attempt_start(self):
        monitor, tracker = self.make(
            lambda n, t: FakeProc(t, work=0.1), period=0.3, timeout=5)
        t0 = time.monotonic()
        monitor.start()
        self.assertTrue(wait_until(lambda: len(tracker.launch_times) >= 4))
        monitor.stop()
        self.assertLess(tracker.launch_times[0] - t0, 0.2)
        gaps = [b - a for a, b in zip(tracker.launch_times, tracker.launch_times[1:])]
        for gap in gaps[:3]:
            self.assertGreaterEqual(gap, 0.27)
            self.assertLess(gap, 0.38, "period is from attempt start, not its end")

    def test_stop_kills_inflight_probe_and_joins(self):
        monitor, tracker = self.make(
            lambda n, t: FakeProc(t, hang=True), timeout=30, period=10)
        monitor.start()
        self.assertTrue(wait_until(lambda: len(tracker.procs) == 1))
        monitor.stop(join_timeout=5)
        self.assertFalse(monitor._thread.is_alive())
        self.assertTrue(tracker.procs[0].killed and tracker.procs[0].reaped)
        self.assertEqual(tracker.active, 0)
        self.assertFalse(monitor.db_ok())


class BlockedLaunchTest(unittest.TestCase):
    """Real-thread tests: _launch() blocks on a threading.Event."""

    def make(self, blocked_call, late_error=False, **kw):
        tracker = Tracker(lambda n, t: FakeProc(t, hang=n == blocked_call))
        release = threading.Event()
        entered = threading.Event()
        calls = []

        def tracked():
            n = len(calls)
            calls.append(n)
            if n == blocked_call:
                entered.set()
                self.assertTrue(release.wait(10))
                if late_error:
                    raise OSError(SECRET)
            proc = tracker.factory(n, tracker)
            with tracker.lock:
                tracker.procs.append(proc)
            return proc

        monitor = worker_prod.DbProbeMonitor(launch=tracked, **kw)
        return monitor, tracker, release, entered, calls

    def test_blocked_launch_times_out_before_return_and_cleans_late_process(self):
        monitor, tracker, release, entered, calls = self.make(
            1, period=0.2, timeout=0.05)
        self.addCleanup(release.set)
        self.addCleanup(monitor.stop, 5)
        monitor.start()
        self.assertTrue(wait_until(monitor.db_ok), "first attempt cached success")
        self.assertTrue(entered.wait(5), "second attempt is blocked in launch")
        attempt = monitor._attempt
        time.sleep(0.15)  # > timeout; launch is still blocked

        self.assertFalse(release.is_set())
        self.assertTrue(attempt.thread.is_alive(), "launch has not returned")
        self.assertFalse(attempt.launch_done.is_set())
        self.assertFalse(monitor.db_ok(), "cache invalidated at the deadline")
        self.assertEqual(monitor.last_code, "DB_PROBE_TIMEOUT")
        self.assertTrue(monitor._thread.is_alive(), "monitor thread not stuck")

        time.sleep(0.3)  # spans a full period: no new attempt while pending
        self.assertEqual(len(calls), 2, "no new launch before cleanup")

        release.set()
        self.assertTrue(wait_until(lambda: tracker.procs[1:] and tracker.procs[1].reaped))
        late = tracker.procs[1]
        self.assertTrue(late.killed)
        with tracker.lock:
            events = list(tracker.events)
        self.assertEqual(events[:5], ["launch", "reap", "launch", "kill", "reap"])
        attempt.thread.join(5)
        self.assertFalse(attempt.thread.is_alive())

        self.assertTrue(wait_until(lambda: len(calls) >= 3), "next attempt after cleanup")
        self.assertTrue(wait_until(monitor.db_ok))
        monitor.stop(5)
        self.assertEqual(tracker.max_active, 1)
        self.assertEqual(tracker.active, 0)

    def test_probe_once_returns_at_deadline_while_launch_blocked(self):
        monitor, tracker, release, entered, calls = self.make(0, timeout=0.05)
        self.addCleanup(release.set)
        started = time.monotonic()
        self.assertFalse(monitor.probe_once())
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(monitor.last_code, "DB_PROBE_TIMEOUT")
        self.assertFalse(monitor.probe_once(), "pending cleanup blocks new attempt")
        self.assertEqual(len(calls), 1)
        release.set()
        monitor._attempt.thread.join(5)
        self.assertTrue(tracker.procs[0].killed and tracker.procs[0].reaped)
        self.assertEqual(tracker.active, 0)
        self.assertEqual(monitor.last_code, "DB_PROBE_TIMEOUT")

    def test_stop_during_blocked_launch_cleans_late_process(self):
        monitor, tracker, release, entered, calls = self.make(
            0, period=10, timeout=30)
        self.addCleanup(release.set)
        monitor.start()
        self.assertTrue(entered.wait(5))
        attempt = monitor._attempt
        monitor.stop(join_timeout=0.3)
        self.assertFalse(monitor._thread.is_alive(), "monitor ended despite launch")
        self.assertTrue(attempt.thread.is_alive(), "launch still blocked")
        self.assertFalse(attempt.launch_done.is_set())
        self.assertFalse(monitor.db_ok())
        self.assertFalse(monitor.probe_once(), "no attempts after stop")
        self.assertEqual(len(calls), 1)

        release.set()
        attempt.thread.join(5)
        self.assertFalse(attempt.thread.is_alive())
        self.assertTrue(tracker.procs[0].killed and tracker.procs[0].reaped)
        self.assertEqual(tracker.active, 0)
        self.assertEqual(len(calls), 1)

    def test_late_launch_exception_finishes_attempt_without_leak(self):
        monitor, tracker, release, entered, calls = self.make(
            0, late_error=True, timeout=0.05)
        self.addCleanup(release.set)
        self.assertFalse(monitor.probe_once())
        attempt = monitor._attempt
        self.assertTrue(attempt.thread.is_alive())
        self.assertEqual(monitor.last_code, "DB_PROBE_TIMEOUT")
        release.set()
        attempt.thread.join(5)
        self.assertFalse(attempt.thread.is_alive())
        self.assertEqual(monitor.last_code, "DB_PROBE_TIMEOUT")
        self.assertNotIn("TOPSECRET", monitor.last_code)
        self.assertEqual(tracker.active, 0)
        self.assertFalse(monitor._cleanup_pending())


class HttpTest(ServerCase):
    def setUp(self):
        self.clock = FakeClock()
        self.health = WorkerHealth(clock=self.clock)
        self.state = {"alive": True, "db": True}
        self.port = self.serve(lambda: self.health.snapshot(
            loop_alive=self.state["alive"], db_ok=self.state["db"],
            max_silence_seconds=900))

    def test_A_startup_ready_is_503_but_health_is_200(self):
        self.health.mark_started()
        status, _, body = http_get(self.port, "/ready")
        self.assertEqual(status, 503)
        self.assertEqual(body, {"ready": False, "reason": "STARTING",
                                "initialized": False, "heartbeat_age_seconds": None})
        status, _, body = http_get(self.port, "/health")
        self.assertEqual((status, body), (200, {"status": "ALIVE"}))

    def test_B_ready_200_after_first_iteration(self):
        self.health.mark_started()
        self.health.mark_tick()
        status, _, body = http_get(self.port, "/ready")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), SNAPSHOT_KEYS)
        self.assertTrue(body["ready"])
        self.assertEqual(body["reason"], "READY")

    def test_V_db_failure_turns_ready_503_immediately(self):
        self.health.mark_started()
        self.health.mark_tick()
        self.assertEqual(http_get(self.port, "/ready")[0], 200)
        self.state["db"] = False
        status, _, body = http_get(self.port, "/ready")
        self.assertEqual((status, body["reason"]), (503, "DB_UNAVAILABLE"))

    def test_M_root_health_unknown_path_and_safe_bodies(self):
        self.health.mark_started()
        self.health.mark_tick()
        self.health.mark_failed("LOOP_EXCEPTION")
        with patch.dict(os.environ, {"DATABASE_URL": SECRET, "MAKE_API_KEY": "k3y"}):
            for path in ("/", "/health", "/ready", "/ready?x=1", "/nope", "/health/x"):
                status, raw, body = http_get(self.port, path)
                for forbidden in ("TOPSECRET", "postgres", "k3y", "Traceback",
                                  "DATABASE_URL"):
                    self.assertNotIn(forbidden, raw)
                if path in ("/", "/health"):
                    self.assertEqual((status, body), (200, {"status": "ALIVE"}))
                elif path.startswith("/ready"):
                    self.assertEqual(status, 503)
                else:
                    self.assertEqual(status, 404)
                    self.assertNotIn("READY", raw)

    def test_snapshot_exception_is_503_and_safe(self):
        port = self.serve(lambda: (_ for _ in ()).throw(RuntimeError(SECRET)))
        status, raw, body = http_get(port, "/ready")
        self.assertEqual(status, 503)
        self.assertFalse(body["ready"])
        self.assertNotIn("TOPSECRET", raw)

    def test_http_reads_cache_and_never_launches_probe(self):
        tracker = Tracker()
        monitor = worker_prod.DbProbeMonitor(launch=tracker.launch, clock=self.clock)
        self.health.mark_started()
        self.health.mark_tick()
        port = self.serve(lambda: self.health.snapshot(
            loop_alive=True, db_ok=monitor.db_ok(), max_silence_seconds=900))
        for _ in range(3):
            status, _, body = http_get(port, "/ready")
            self.assertEqual((status, body["reason"]), (503, "DB_UNAVAILABLE"))
        self.assertEqual(tracker.procs, [])


class LoopTest(unittest.TestCase):
    """Real run_worker_loop; only task_store/worker are replaced."""

    def run_loop(self, run_once, recover=None, max_iterations=10, health=None):
        health = health or WorkerHealth(clock=FakeClock())
        health.mark_started()
        alive = threading.Event()
        sleeps = []
        with patch.object(worker_prod.task_store, "recover_stale_tasks",
                          side_effect=recover or (lambda s: [])), \
                patch.object(worker_prod.worker, "run_once", side_effect=run_once), \
                patch("builtins.print"):
            try:
                worker_prod.run_worker_loop(
                    health, alive, max_iterations=max_iterations,
                    sleep=sleeps.append)
                error = None
            except Exception as exc:  # noqa: BLE001
                error = exc
        return health, alive, sleeps, error

    def snap(self, health, alive=True, db=True):
        return health.snapshot(loop_alive=alive, db_ok=db, max_silence_seconds=900)

    def test_E_run_once_exception_is_not_ready_and_propagates(self):
        calls = []

        def run_once():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError(SECRET)
            return {"status": "IDLE"}

        health, alive, _, error = self.run_loop(run_once)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(len(calls), 2, "no new retry after the exception")
        self.assertFalse(alive.is_set())
        s = self.snap(health)
        self.assertEqual((s["ready"], s["reason"]), (False, "LOOP_EXCEPTION"))
        self.assertNotIn("TOPSECRET", json.dumps(s))

    def test_E_recovery_exception_is_not_ready_and_skips_run_once(self):
        recover_calls = []

        def recover(_seconds):
            recover_calls.append(1)
            if len(recover_calls) == 2:
                raise RuntimeError(SECRET)
            return []

        run_calls = []
        health, alive, _, error = self.run_loop(
            lambda: run_calls.append(1) or {"status": "DONE", "task": {}},
            recover=recover)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(len(run_calls), 1)
        self.assertFalse(alive.is_set())
        self.assertEqual(self.snap(health)["reason"], "RECOVERY_FAILED")

    def test_E_exception_in_first_iteration_is_still_starting(self):
        health, _, _, error = self.run_loop(
            lambda: (_ for _ in ()).throw(RuntimeError("x")))
        self.assertIsNotNone(error)
        s = self.snap(health)
        self.assertFalse(s["ready"])
        self.assertEqual(s["reason"], "STARTING")

    def test_J_normal_statuses_count_as_progress(self):
        results = [
            {"status": "ERROR", "task": {"task_id": "t1", "status": "PENDING"},
             "alert_queued": False},
            {"status": "IDLE"},
            {"status": "DONE", "task": {"task_id": "t2", "status": "SUCCEEDED"}},
            {"status": "DEFERRED", "task": {"task_id": "t3", "status": "PENDING"}},
        ]
        it = iter(results)
        health, alive, sleeps, error = self.run_loop(
            lambda: next(it), max_iterations=4)
        self.assertIsNone(error)
        s = self.snap(health, alive=True)
        self.assertTrue(s["ready"], s)
        self.assertEqual(sleeps, [worker_prod.POLL_SECONDS], "sleep only on IDLE")
        self.assertFalse(alive.is_set(), "loop flag cleared on normal end")
        self.assertEqual(self.snap(health, alive=alive.is_set())["reason"],
                         "LOOP_NOT_ALIVE")

    def test_J_error_status_keeps_completion_log(self):
        health = WorkerHealth(clock=FakeClock())
        health.mark_started()
        with patch.object(worker_prod.task_store, "recover_stale_tasks",
                          return_value=[{"task_id": 7}]), \
                patch.object(worker_prod.worker, "run_once", return_value={
                    "status": "ERROR", "task": {"task_id": "t1", "status": "FAILED",
                                                "attempts": 8},
                    "alert_queued": True}), \
                patch("builtins.print") as printed:
            worker_prod.run_worker_loop(health, threading.Event(), max_iterations=1,
                                        sleep=lambda s: None)
        events = [json.loads(c.args[0])["event"] for c in printed.call_args_list]
        self.assertEqual(events, ["tasks_recovered", "task_finished"])

    def test_I_first_long_job_stays_starting_and_start_does_not_tick(self):
        clock = FakeClock()
        health = WorkerHealth(clock=clock)
        entered, release = threading.Event(), threading.Event()

        def run_once():
            entered.set()
            self.assertTrue(release.wait(10))
            return {"status": "DONE", "task": {}}

        alive = threading.Event()
        health.mark_started()
        with patch.object(worker_prod.task_store, "recover_stale_tasks",
                          return_value=[]), \
                patch.object(worker_prod.worker, "run_once", side_effect=run_once), \
                patch("builtins.print"):
            thread = threading.Thread(target=lambda: worker_prod.run_worker_loop(
                health, alive, max_iterations=1, sleep=lambda s: None))
            thread.start()
            self.assertTrue(entered.wait(5))
            for jump in (0, 100, 5000):
                clock.advance(jump)
                s = self.snap(health, alive=alive.is_set())
                self.assertEqual((s["ready"], s["reason"], s["heartbeat_age_seconds"]),
                                 (False, "STARTING", None))
            release.set()
            thread.join(5)
        self.assertEqual(self.snap(health, alive=True)["reason"], "READY")

    def test_Z_long_second_job_ready_until_threshold_then_stale(self):
        clock = FakeClock()
        health = WorkerHealth(clock=clock)
        entered, release = threading.Event(), threading.Event()
        calls = []

        def run_once():
            calls.append(1)
            if len(calls) == 2:
                entered.set()
                self.assertTrue(release.wait(10))
            return {"status": "DONE", "task": {}}

        alive = threading.Event()
        health.mark_started()
        with patch.object(worker_prod.task_store, "recover_stale_tasks",
                          return_value=[]), \
                patch.object(worker_prod.worker, "run_once", side_effect=run_once), \
                patch("builtins.print"):
            thread = threading.Thread(target=lambda: worker_prod.run_worker_loop(
                health, alive, max_iterations=2, sleep=lambda s: None))
            thread.start()
            self.assertTrue(entered.wait(5))
            clock.advance(899)
            self.assertTrue(self.snap(health, alive=alive.is_set())["ready"])
            clock.advance(1)
            self.assertTrue(self.snap(health, alive=alive.is_set())["ready"])
            clock.advance(0.5)
            s = self.snap(health, alive=alive.is_set())
            self.assertEqual((s["ready"], s["reason"]), (False, "STALE_HEARTBEAT"))
            release.set()
            thread.join(5)
        self.assertTrue(self.snap(health, alive=True)["ready"])


class ServeForeverLifecycleTest(unittest.TestCase):
    def run_serve(self, run_once, max_iterations, tracker=None, period=0.05):
        tracker = tracker or Tracker()
        created, servers = [], []
        real_start = worker_prod.start_health_server

        class Rec(worker_prod.DbProbeMonitor):
            def __init__(self, **kw):
                super().__init__(**{**kw, "period": period, "timeout": 2})
                created.append(self)

        def start(snapshot_fn, port=None, host="0.0.0.0"):
            server = real_start(snapshot_fn, port=0, host="127.0.0.1")
            servers.append(server)
            return server

        with patch.object(worker_prod, "DbProbeMonitor", Rec), \
                patch.object(worker_prod, "start_health_server", start), \
                patch.object(worker_prod.task_store, "recover_stale_tasks",
                             return_value=[]), \
                patch.object(worker_prod.worker, "run_once", side_effect=run_once), \
                patch("builtins.print"):
            try:
                worker_prod.serve_forever(
                    max_iterations=max_iterations, launch=tracker.launch,
                    sleep=lambda s: None, max_silence=900)
                error = None
            except Exception as exc:  # noqa: BLE001
                error = exc
        return tracker, created[0], servers[0], error

    def test_N_end_to_end_ready_flow_and_cleanup(self):
        observed = {}
        holder = {}
        calls = []

        def run_once():
            calls.append(1)
            monitor, server = holder["monitor"], holder["server"]
            port = server.server_address[1]
            self.assertTrue(wait_until(monitor.db_ok))
            if len(calls) == 1:
                observed["first"] = http_get(port, "/ready")
                observed["health"] = http_get(port, "/health")
            elif len(calls) == 2:
                observed["second"] = http_get(port, "/ready")
            return {"status": "DONE", "task": {}}

        tracker = Tracker()
        real_probe = worker_prod.DbProbeMonitor.start

        def start_and_capture(self):
            holder["monitor"] = self
            real_probe(self)

        real_start = worker_prod.start_health_server

        def capture_server(snapshot_fn, port=None, host="0.0.0.0"):
            server = real_start(snapshot_fn, port=0, host="127.0.0.1")
            holder["server"] = server
            return server

        with patch.object(worker_prod.DbProbeMonitor, "start", start_and_capture), \
                patch.object(worker_prod, "start_health_server", capture_server), \
                patch.object(worker_prod.task_store, "recover_stale_tasks",
                             return_value=[]), \
                patch.object(worker_prod.worker, "run_once", side_effect=run_once), \
                patch("builtins.print"):
            worker_prod.serve_forever(max_iterations=3, launch=tracker.launch,
                                      sleep=lambda s: None, max_silence=900)

        status, _, body = observed["first"]
        self.assertEqual((status, body["reason"]), (503, "STARTING"))
        self.assertEqual(observed["health"][0], 200)
        status, _, body = observed["second"]
        self.assertEqual((status, body["reason"]), (200, "READY"))
        monitor, server = holder["monitor"], holder["server"]
        self.assertFalse(monitor._thread.is_alive())
        self.assertEqual(tracker.active, 0)
        self.assertEqual(server.socket.fileno(), -1, "server socket closed")

    def test_N_exception_exit_still_cleans_up(self):
        calls = []

        def run_once():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError(SECRET)
            return {"status": "DONE", "task": {}}

        tracker, monitor, server, error = self.run_serve(run_once, max_iterations=5)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(len(calls), 2)
        self.assertFalse(monitor._thread.is_alive())
        self.assertEqual(tracker.active, 0)
        self.assertEqual(server.socket.fileno(), -1)

    def test_N_probe_period_independent_of_run_once_duration(self):
        counts = {}
        calls = []
        tracker = Tracker()

        def run_once():
            calls.append(1)
            if len(calls) == 1:
                counts["before"] = len(tracker.procs)
                counts["reached"] = wait_until(
                    lambda: len(tracker.procs) >= counts["before"] + 3)
                counts["after"] = len(tracker.procs)
            return {"status": "DONE", "task": {}}

        _, _, _, error = self.run_serve(run_once, 1, tracker=tracker)
        self.assertIsNone(error)
        self.assertTrue(counts["reached"], counts)
        self.assertGreaterEqual(counts["after"] - counts["before"], 3)


if __name__ == "__main__":
    unittest.main()
