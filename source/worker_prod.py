import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import task_store
import worker
import worker_health


POLL_SECONDS = int(os.getenv("WORKER_POLL_SECONDS", "2"))
STALE_SECONDS = int(os.getenv("WORKER_STALE_SECONDS", "300"))

DEFAULT_READY_MAX_SILENCE_SECONDS = 900.0
DB_PROBE_PERIOD_SECONDS = 10.0
DB_PROBE_TIMEOUT_SECONDS = 5.0
DB_PROBE_TTL_SECONDS = 15.0
SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PROBE_CODE = "import postgres_store; postgres_store.ping()"


class ConfigError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_ready_max_silence(environ=None):
    environ = os.environ if environ is None else environ
    raw = environ.get("WORKER_READY_MAX_SILENCE_SECONDS")
    if raw is None:
        return DEFAULT_READY_MAX_SILENCE_SECONDS
    try:
        return worker_health.validate_max_silence(float(raw))
    except ValueError:
        raise ConfigError("INVALID_WORKER_READY_MAX_SILENCE_SECONDS") from None


def launch_db_probe():
    """Start one probe child: same interpreter, no shell, output discarded."""
    return subprocess.Popen(
        [sys.executable, "-c", DB_PROBE_CODE],
        cwd=SOURCE_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


def _kill_and_reap(proc):
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait()
    except Exception:
        pass


class _Attempt:
    """One probe attempt: _launch() runs in its own thread.

    launch_done is set only by the launch thread, only after _launch() actually
    returned or raised and the outcome was recorded under `lock`. `abandoned`
    means the outcome may no longer be used; a process that appears after that
    is killed and reaped by the launch thread itself before it exits.
    """

    def __init__(self, launch):
        self._launch = launch
        self.lock = threading.Lock()
        self.launch_done = threading.Event()
        self.abandoned = False
        self.proc = None
        self.error = False
        self.thread = threading.Thread(
            target=self._run, name="db-probe-launch", daemon=True
        )

    def _run(self):
        proc = None
        error = False
        try:
            proc = self._launch()
        except Exception:
            error = True
        late = None
        with self.lock:
            if self.abandoned:
                late = proc
            else:
                self.proc = proc
                self.error = error
            self.launch_done.set()
        if late is not None:
            _kill_and_reap(late)

    def abandon(self):
        """Mark the attempt unusable unless launch already finished.

        Returns True if launch had already finished (outcome stays usable).
        """
        with self.lock:
            if self.launch_done.is_set():
                return True
            self.abandoned = True
            return False


class DbProbeMonitor:
    """Independent DB availability monitor.

    One probe child at a time, started every `period` seconds measured from the
    start of the previous attempt. HTTP code only reads db_ok(), which uses the
    cached result and never starts a probe. _launch() runs in a per-attempt
    thread so a blocked launch cannot stall the deadline or stop(); a new
    attempt starts only after the launch thread of an abandoned one has exited.
    """

    _POLL_SECONDS = 0.02

    def __init__(
        self,
        *,
        launch=None,
        clock=time.monotonic,
        deadline_clock=time.monotonic,
        period=DB_PROBE_PERIOD_SECONDS,
        timeout=DB_PROBE_TIMEOUT_SECONDS,
        ttl=DB_PROBE_TTL_SECONDS,
    ):
        self._launch = launch or launch_db_probe
        self._clock = clock
        self._deadline_clock = deadline_clock
        self._period = period
        self._timeout = timeout
        self._ttl = ttl
        self._lock = threading.Lock()
        self._ok_at = None
        self._last_code = None
        self._proc = None
        self._attempt = None
        self._stop = threading.Event()
        self._thread = None

    @property
    def last_code(self):
        with self._lock:
            return self._last_code

    def db_ok(self):
        with self._lock:
            if self._ok_at is None:
                return False
            return (self._clock() - self._ok_at) <= self._ttl

    def _record_failure(self, code):
        with self._lock:
            self._ok_at = None
            self._last_code = code

    def _cleanup_pending(self):
        with self._lock:
            attempt = self._attempt
        return (
            attempt is not None
            and attempt.abandoned
            and attempt.thread.is_alive()
        )

    def _await_launch(self, attempt, started):
        """Wait for launch_done, the deadline or stop; decide under the lock."""
        while not attempt.launch_done.is_set():
            if self._stop.is_set():
                if not attempt.abandon():
                    return "stopped"
                break
            remaining = self._timeout - (self._deadline_clock() - started)
            if remaining <= 0:
                if not attempt.abandon():
                    return "timeout"
                break
            attempt.launch_done.wait(min(remaining, self._POLL_SECONDS))
        return "done"

    def probe_once(self):
        """Run one attempt bounded by a single deadline that includes launch.

        Returns True only for exit code 0.
        """
        if self._stop.is_set() or self._cleanup_pending():
            return False
        started = self._deadline_clock()
        attempt = _Attempt(self._launch)
        with self._lock:
            self._attempt = attempt
        attempt.thread.start()
        proc = None
        try:
            outcome = self._await_launch(attempt, started)
            if outcome == "stopped":
                self._record_failure("DB_PROBE_STOPPED")
                return False
            if outcome == "timeout":
                self._record_failure("DB_PROBE_TIMEOUT")
                return False
            with attempt.lock:
                proc = attempt.proc
                error = attempt.error
                abandoned = attempt.abandoned
            if abandoned:
                self._record_failure("DB_PROBE_STOPPED")
                return False
            if error or proc is None:
                self._record_failure("DB_PROBE_ERROR")
                return False
            with self._lock:
                self._proc = proc
            if self._stop.is_set():
                _kill_and_reap(proc)
                self._record_failure("DB_PROBE_STOPPED")
                return False
            remaining = self._timeout - (self._deadline_clock() - started)
            try:
                if remaining <= 0:
                    raise subprocess.TimeoutExpired("db-probe", 0)
                returncode = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self._record_failure("DB_PROBE_TIMEOUT")
                _kill_and_reap(proc)
                return False
            if returncode == 0:
                with self._lock:
                    self._ok_at = self._clock()
                    self._last_code = "DB_OK"
                return True
            self._record_failure("DB_PROBE_FAILED")
            return False
        except Exception:
            self._record_failure("DB_PROBE_ERROR")
            if proc is not None:
                _kill_and_reap(proc)
            return False
        finally:
            with self._lock:
                self._proc = None

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            self.probe_once()
            delay = max(0.0, started + self._period - time.monotonic())
            if self._stop.wait(delay):
                break
            while self._cleanup_pending():
                if self._stop.wait(self._POLL_SECONDS):
                    return

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="db-probe-monitor", daemon=True
        )
        self._thread.start()

    def stop(self, join_timeout=30.0):
        self._stop.set()
        with self._lock:
            self._ok_at = None
            proc = self._proc
            attempt = self._attempt
        if attempt is not None:
            attempt.abandon()
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        end = time.monotonic() + join_timeout
        thread = self._thread
        if thread is not None:
            thread.join(max(0.0, end - time.monotonic()))
        if attempt is not None:
            attempt.thread.join(max(0.0, end - time.monotonic()))
        with self._lock:
            self._ok_at = None


def make_handler(snapshot_fn):
    class HealthHandler(BaseHTTPRequestHandler):
        def _send(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in {"/", "/health"}:
                self._send(200, {"status": "ALIVE"})
            elif path == "/ready":
                try:
                    snap = snapshot_fn()
                except Exception:
                    self._send(503, {
                        "ready": False,
                        "reason": "READINESS_ERROR",
                        "initialized": False,
                        "heartbeat_age_seconds": None,
                    })
                    return
                self._send(200 if snap["ready"] else 503, snap)
            else:
                self._send(404, {"status": "NOT_FOUND"})

        def log_message(self, _fmt, *_args):
            return

    return HealthHandler


def start_health_server(snapshot_fn, port=None, host="0.0.0.0"):
    if port is None:
        port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), make_handler(snapshot_fn))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_worker_loop(health, loop_alive, *, max_iterations=None, sleep=None):
    """Existing queue loop; heartbeat only after a fully completed iteration."""
    sleep = sleep or time.sleep
    loop_alive.set()
    iterations = 0
    try:
        while max_iterations is None or iterations < max_iterations:
            try:
                recovered = task_store.recover_stale_tasks(STALE_SECONDS)
            except Exception:
                health.mark_failed(worker_health.RECOVERY_FAILED)
                raise
            if recovered:
                print(json.dumps({
                    "timestamp": utc_now(),
                    "event": "tasks_recovered",
                    "task_ids": [str(item["task_id"]) for item in recovered],
                }), flush=True)
            try:
                result = worker.run_once()
            except Exception:
                health.mark_failed(worker_health.LOOP_EXCEPTION)
                raise
            health.mark_tick()
            iterations += 1
            if result["status"] != "IDLE":
                task = result.get("task") or {}
                print(json.dumps({
                    "timestamp": utc_now(),
                    "event": "task_finished",
                    "status": result["status"],
                    "task_id": str(task.get("task_id")),
                    "task_status": task.get("status"),
                    "attempts": task.get("attempts"),
                    "alert_queued": result.get("alert_queued"),
                }, default=str), flush=True)
            if result["status"] == "IDLE":
                sleep(POLL_SECONDS)
    finally:
        loop_alive.clear()


def serve_forever(*, max_iterations=None, port=None, launch=None, sleep=None,
                  max_silence=None):
    if max_silence is None:
        max_silence = load_ready_max_silence()
    health = worker_health.WorkerHealth()
    health.mark_started()
    loop_alive = threading.Event()
    monitor = DbProbeMonitor(launch=launch)

    def snapshot():
        return health.snapshot(
            loop_alive=loop_alive.is_set(),
            db_ok=monitor.db_ok(),
            max_silence_seconds=max_silence,
        )

    server = None
    try:
        monitor.start()
        server = start_health_server(snapshot, port)
        print(json.dumps({"timestamp": utc_now(), "event": "worker_started"}), flush=True)
        run_worker_loop(health, loop_alive, max_iterations=max_iterations, sleep=sleep)
    finally:
        loop_alive.clear()
        monitor.stop()
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    serve_forever()
