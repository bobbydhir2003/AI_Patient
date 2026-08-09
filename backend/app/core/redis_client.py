"""Shared Redis connection used ONLY for fleet-wide (cross-worker) concurrency
control (see core/distributed_semaphore.py). Not used for caching or sessions.

Sync client (redis-py), matching the sync/threadpool style of the rest of the
backend (endpoints run in FastAPI's threadpool; provider calls are blocking).
One connection pool per process, created lazily so importing this module never
requires the `redis` package or a reachable server (local dev without Redis
stays fully functional - see distributed_semaphore.py's local fallback).

Socket/connect timeouts are intentionally short so a Redis outage fails fast
(bounded) instead of hanging a request for the full concurrency-wait window.
"""
from __future__ import annotations

import threading

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_client = None
_client_lock = threading.Lock()
_unavailable_warned = False


def redis_configured() -> bool:
    return bool(get_settings().redis_url.strip())


def get_redis_client():
    """Return the shared redis.Redis client, or None if REDIS_URL is not set."""
    global _client
    settings = get_settings()
    if not settings.redis_url.strip():
        return None
    with _client_lock:
        if _client is None:
            import redis  # imported lazily so the package is only required when Redis is actually used

            _client = redis.Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=settings.redis_connect_timeout_seconds,
                socket_timeout=settings.redis_socket_timeout_seconds,
                health_check_interval=30,
            )
        return _client


def ping() -> bool:
    """Best-effort reachability check. Never raises."""
    global _unavailable_warned
    client = get_redis_client()
    if client is None:
        return False
    try:
        ok = bool(client.ping())
        if ok:
            _unavailable_warned = False
        return ok
    except Exception as exc:
        if not _unavailable_warned:
            logger.warning("redis_unavailable error=%s", exc)
            _unavailable_warned = True
        return False


def redis_health() -> dict:
    """Real, truthful Redis status for health/admin endpoints. Never fabricated."""
    settings = get_settings()
    if not settings.redis_url.strip():
        return {"status": "not_configured", "required": settings.redis_required, "latency_ms": None}
    import time

    t0 = time.perf_counter()
    ok = ping()
    latency_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "status": "connected" if ok else "unavailable",
        "required": settings.redis_required,
        "latency_ms": latency_ms if ok else None,
    }


def reset_redis_client() -> None:
    """Test helper: drop the cached client so a new REDIS_URL takes effect."""
    global _client, _unavailable_warned
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
        _client = None
    _unavailable_warned = False
