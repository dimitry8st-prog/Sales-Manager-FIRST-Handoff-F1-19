"""Thread-safe readiness state of the background worker loop.

The class holds only monotonic timestamps and normalized codes. It performs no
network, SQL, subprocess or other I/O and never stores raw exception text.
"""

import math
import threading
import time


STARTING = "STARTING"
RECOVERY_FAILED = "RECOVERY_FAILED"
LOOP_EXCEPTION = "LOOP_EXCEPTION"
LOOP_NOT_ALIVE = "LOOP_NOT_ALIVE"
STALE_HEARTBEAT = "STALE_HEARTBEAT"
DB_UNAVAILABLE = "DB_UNAVAILABLE"
READY = "READY"

FAILURE_CODES = frozenset({RECOVERY_FAILED, LOOP_EXCEPTION})


def validate_max_silence(value):
    """Return value as float; only positive finite real numbers are accepted."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("max_silence_seconds must be a positive finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("max_silence_seconds must be a positive finite number")
    return number


class WorkerHealth:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._initialized = False
        self._last_tick = None
        self._failure = None

    def mark_started(self):
        with self._lock:
            self._initialized = False
            self._last_tick = None
            self._failure = None

    def mark_tick(self):
        with self._lock:
            self._last_tick = self._clock()
            self._initialized = True
            self._failure = None

    def mark_failed(self, code):
        if code not in FAILURE_CODES:
            code = LOOP_EXCEPTION
        with self._lock:
            self._failure = code

    def snapshot(self, *, loop_alive, db_ok, max_silence_seconds):
        limit = validate_max_silence(max_silence_seconds)
        with self._lock:
            initialized = self._initialized
            failure = self._failure
            age = None
            if self._last_tick is not None:
                age = max(0.0, float(self._clock() - self._last_tick))
        if not initialized:
            reason = STARTING
        elif failure is not None:
            reason = failure
        elif not loop_alive:
            reason = LOOP_NOT_ALIVE
        elif age is None or age > limit:
            reason = STALE_HEARTBEAT
        elif not db_ok:
            reason = DB_UNAVAILABLE
        else:
            reason = READY
        return {
            "ready": reason == READY,
            "reason": reason,
            "initialized": initialized,
            "heartbeat_age_seconds": age,
        }
