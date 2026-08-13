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
import time

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_client = None
_client_lock = threading.Lock()
_unavailable_warned = False
_ever_connected = False

# Short-TTL cache for the reachability pre-check used by the concurrency guard
# (see DistributedSemaphore._acquire_redis). Deliberately separate from
# redis_health()'s always-fresh ping so an admin/health endpoint never reports
# stale latency, while the hot per-request acquire path avoids an extra Redis
# round trip on every single call.
_health_cache_lock = threading.Lock()
_cached_healthy: bool | None = None
_cached_at: float = 0.0


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
    """Real, always-fresh reachability check. Never raises.

    Logs state TRANSITIONS only (never spams): `redis_connected` the first
    time this process ever reaches Redis, `redis_recovered` when it comes
    back after being down, `redis_unavailable` (warn-once) while it stays
    down."""
    global _unavailable_warned, _ever_connected
    client = get_redis_client()
    if client is None:
        return False
    try:
        ok = bool(client.ping())
        if ok:
            if _unavailable_warned:
                logger.info("redis_recovered")
            elif not _ever_connected:
                logger.info("redis_connected")
            _unavailable_warned = False
            _ever_connected = True
        return ok
    except Exception as exc:
        if not _unavailable_warned:
            logger.warning("redis_unavailable error=%s", exc)
            _unavailable_warned = True
        return False


def ping_cached() -> bool:
    """Cheap reachability pre-check for the hot concurrency-acquire path.

    Caches the last real ping() result for `settings.redis_health_cache_seconds`
    so a semaphore acquire on every TTS/interview/assessment request does not
    cost an extra Redis round trip on top of the Lua acquire script. The Lua
    acquire/release call - the operation that actually determines correctness -
    still hits Redis on every single call; this only decides whether to
    attempt it or fail fast when Redis is known-down from a moment ago."""
    global _cached_healthy, _cached_at
    ttl = max(0.0, get_settings().redis_health_cache_seconds)
    now = time.monotonic()
    with _health_cache_lock:
        if _cached_healthy is not None and (now - _cached_at) < ttl:
            return _cached_healthy
    result = ping()
    with _health_cache_lock:
        _cached_healthy = result
        _cached_at = now
    return result


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
    global _client, _unavailable_warned, _ever_connected, _cached_healthy, _cached_at
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
        _client = None
    _unavailable_warned = False
    _ever_connected = False
    with _health_cache_lock:
        _cached_healthy = None
        _cached_at = 0.0
