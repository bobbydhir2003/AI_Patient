"""Concurrency guards for paid AI work.

Sync FastAPI endpoints run in a threadpool, so these guards are blocking (not
asyncio). Two independent, NEVER-shared limits:

- interview generation (OpenAI): a hard cap with a short bounded wait; if still
  full, a controlled ServiceOverloadedError (503) is raised BEFORE any provider
  call, so students see a clean "at capacity" message instead of a raw failure.
- TTS (ElevenLabs): a separate cap. TTS is best-effort - if it is saturated the
  caller DEGRADES to text-only rather than failing the interview.

Limits are read live from settings, so the effective cap tracks configuration.

GLOBAL across workers: both guards are backed by DistributedSemaphore (Redis),
so with N uvicorn workers the configured limit is the real fleet-wide cap, not
N x the limit. See core/distributed_semaphore.py for the Redis design and the
local (per-process) fallback used in development/test.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.core.distributed_semaphore import DistributedSemaphore
from app.core.exceptions import ServiceOverloadedError
from app.core.logging import get_logger
from app.core.telemetry import get_telemetry

logger = get_logger(__name__)

_interview_sem = DistributedSemaphore("openai_interview")
_tts_sem = DistributedSemaphore("tts")


class interview_slot:
    """Context manager: reserve an AI-interview slot or raise ServiceOverloadedError.

    Usage:
        with interview_slot():
            ... call OpenAI ...
    """

    def __init__(self) -> None:
        self._token: str | None = None

    def __enter__(self):
        s = get_settings()
        tele = get_telemetry()
        # Track how long we wait for a slot (queue pressure), who is waiting, and
        # timeouts (controlled overloads).
        import time as _time

        tele.interview_waiting.inc()
        t0 = _time.monotonic()
        try:
            self._token = _interview_sem.acquire(s.max_concurrent_ai_interviews, s.ai_interview_wait_seconds)
        finally:
            wait_ms = (_time.monotonic() - t0) * 1000.0
            tele.interview_waiting.dec()
            tele.interview_wait.observe_latency(wait_ms)
        if self._token is None:
            tele.interview_wait.incr("timeout")
            tele.http.incr("interview_overload")
            raise ServiceOverloadedError()
        tele.interview_in_flight.inc()
        return self

    def __exit__(self, *exc):
        get_telemetry().interview_in_flight.dec()
        _interview_sem.release(self._token)
        self._token = None
        return False


def interview_capacity() -> dict:
    s = get_settings()
    tele = get_telemetry()
    global_active = _interview_sem.active_count()
    return {
        "active": global_active if global_active is not None else tele.interview_in_flight.value,
        "active_scope": "global" if global_active is not None else "process",
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
        self._token: str | None = None

    def acquire(self):
        s = get_settings()
        tele = get_telemetry()
        import time as _time

        tele.tts_waiting.inc()
        t0 = _time.monotonic()
        try:
            self._token = _tts_sem.acquire(s.max_concurrent_tts_requests, s.tts_wait_seconds)
        finally:
            wait_ms = (_time.monotonic() - t0) * 1000.0
            tele.tts_waiting.dec()
            tele.tts_wait.observe_latency(wait_ms)
        self.ok = self._token is not None
        if self.ok:
            tele.tts_wait.incr("acquired")
            if wait_ms >= 50.0:  # negligible waits are not worth a counter bump
                tele.tts_wait.incr("waited")
                logger.info("tts_slot_waited wait_ms=%.0f", wait_ms)
            get_telemetry().tts_in_flight.inc()
        else:
            tele.tts_wait.incr("timeout")
            get_telemetry().elevenlabs.window.incr("degraded")
            logger.info(
                "tts_slot_timeout wait_ms=%.0f max_wait_s=%.1f reason=no_capacity_slot",
                wait_ms, s.tts_wait_seconds,
            )
        return self

    def __enter__(self):
        return self.acquire()

    def release(self):
        if self.ok:
            get_telemetry().tts_in_flight.dec()
            _tts_sem.release(self._token)
            self.ok = False
            self._token = None

    def __exit__(self, *exc):
        self.release()
        return False


def tts_capacity() -> dict:
    s = get_settings()
    tele = get_telemetry()
    global_active = _tts_sem.active_count()
    return {
        "active": global_active if global_active is not None else tele.tts_in_flight.value,
        "active_scope": "global" if global_active is not None else "process",
        "limit": s.max_concurrent_tts_requests,
        "waiting": tele.tts_waiting.value,
        "wait_p50_ms": tele.tts_wait.percentile(300, 50),
        "wait_p95_ms": tele.tts_wait.percentile(300, 95),
        "acquired_5m": tele.tts_wait.sum("acquired", 300),
        "waited_5m": tele.tts_wait.sum("waited", 300),
        "timeouts_5m": tele.tts_wait.sum("timeout", 300),
    }
