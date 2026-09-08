"""Concurrency guards for paid AI work.

Sync FastAPI endpoints run in a threadpool, so these guards are blocking (not
asyncio).

- interview generation (OpenAI): a hard cap with a short bounded wait; if still
  full, a controlled ServiceOverloadedError (503) is raised BEFORE any provider
  call, so students see a clean "at capacity" message instead of a raw failure.

Limits are read live from settings, so the effective cap tracks configuration.

GLOBAL across workers: the guard is backed by DistributedSemaphore (Redis), so
with N uvicorn workers the configured limit is the real fleet-wide cap, not
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
