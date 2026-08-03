"""Concurrency guards for paid AI work.

Sync FastAPI endpoints run in a threadpool, so we use bounded threading
semaphores (not asyncio). Two independent limits:

- interview generation (OpenAI): a hard cap with a short bounded wait; if still
  full, a controlled ServiceOverloadedError (503) is raised BEFORE any provider
  call, so students see a clean "at capacity" message instead of a raw failure.
- TTS (ElevenLabs): a separate cap. TTS is best-effort - if it is saturated the
  caller DEGRADES to text-only rather than failing the interview.

Limits are read live from settings, so the effective cap tracks configuration.
They are PER PROCESS (per worker), matching the process-local rate limiter.
"""
from __future__ import annotations

import threading

from app.core.config import get_settings
from app.core.exceptions import ServiceOverloadedError
from app.core.telemetry import get_telemetry


class _ResizableSemaphore:
    """A semaphore whose capacity can follow a settings value at acquire time."""

    def __init__(self) -> None:
        self._sem = threading.Semaphore(0)
        self._capacity = 0
        self._lock = threading.Lock()

    def _ensure_capacity(self, target: int) -> None:
        with self._lock:
            while self._capacity < target:
                self._sem.release()
                self._capacity += 1
            # We never shrink live (would risk over-releasing); a lowered limit
            # takes effect as slots are returned and simply not re-added.
            self._capacity = max(self._capacity, target)

    def acquire(self, capacity: int, timeout: float) -> bool:
        self._ensure_capacity(capacity)
        return self._sem.acquire(timeout=max(0.0, timeout))

    def release(self) -> None:
        self._sem.release()


_interview_sem = _ResizableSemaphore()
_tts_sem = _ResizableSemaphore()


class interview_slot:
    """Context manager: reserve an AI-interview slot or raise ServiceOverloadedError.

    Usage:
        with interview_slot():
            ... call OpenAI ...
    """

    def __enter__(self):
        s = get_settings()
        tele = get_telemetry()
        # Track how long we wait for a slot (queue pressure), who is waiting, and
        # timeouts (controlled overloads).
        import time as _time

        tele.interview_waiting.inc()
        t0 = _time.monotonic()
        try:
            acquired = _interview_sem.acquire(s.max_concurrent_ai_interviews, s.ai_interview_wait_seconds)
        finally:
            wait_ms = (_time.monotonic() - t0) * 1000.0
            tele.interview_waiting.dec()
            tele.interview_wait.observe_latency(wait_ms)
        if not acquired:
            tele.interview_wait.incr("timeout")
            tele.http.incr("interview_overload")
            raise ServiceOverloadedError()
        tele.interview_in_flight.inc()
        return self

    def __exit__(self, *exc):
        get_telemetry().interview_in_flight.dec()
        _interview_sem.release()
        return False


def interview_capacity() -> dict:
    s = get_settings()
    tele = get_telemetry()
    return {
        "active": tele.interview_in_flight.value,
        "limit": s.max_concurrent_ai_interviews,
        "waiting": tele.interview_waiting.value,
        "wait_p50_ms": tele.interview_wait.percentile(300, 50),
        "wait_p95_ms": tele.interview_wait.percentile(300, 95),
        "timeouts_5m": tele.interview_wait.sum("timeout", 300),
    }


class tts_slot:
    """Context manager that tries to reserve a TTS slot.

    Unlike interviews, TTS is optional: `acquired` reports whether a slot was
    obtained so the caller can DEGRADE to text-only instead of failing.

        slot = tts_slot().acquire()
        if not slot.ok:
            ... skip audio, keep the interview turn ...
    """

    def __init__(self):
        self.ok = False

    def acquire(self):
        s = get_settings()
        self.ok = _tts_sem.acquire(s.max_concurrent_tts_requests, s.tts_wait_seconds)
        if self.ok:
            get_telemetry().tts_in_flight.inc()
        else:
            get_telemetry().elevenlabs.window.incr("degraded")
        return self

    def __enter__(self):
        return self.acquire()

    def release(self):
        if self.ok:
            get_telemetry().tts_in_flight.dec()
            _tts_sem.release()
            self.ok = False

    def __exit__(self, *exc):
        self.release()
        return False


def tts_capacity() -> dict:
    s = get_settings()
    return {"active": get_telemetry().tts_in_flight.value, "limit": s.max_concurrent_tts_requests}
