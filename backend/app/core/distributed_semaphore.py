"""Fleet-wide (cross-worker) concurrency limits, backed by Redis.

Uvicorn runs N worker PROCESSES. A plain threading.Semaphore (the original
implementation in this module, now `_LocalFallbackSemaphore`) only bounds
concurrency within ONE process, so N workers x limit = N x the real cap. This
is the fix: the same acquire/release/timeout contract, backed by a Redis
sorted set so the limit is enforced across the whole fleet.

Design: each holder writes a unique token into a ZSET with score = now_ms +
lease_ms (an expiry). Acquiring is a single Lua script (atomic in Redis) that
prunes expired tokens, then admits a new one only if the live count is under
capacity. A holder that crashes without releasing simply expires out of the
set after `lease_ms` - no separate reaper process needed.

Separate limits per provider: construct one `DistributedSemaphore` per name
("openai_interview", "tts", "assessment") - they use different Redis keys and
are NEVER shared, matching separate OpenAI/ElevenLabs provider limits.

Fallback (development/test only): when Redis is not configured, or not
required (`settings.redis_required` is false - the default outside
production/staging), this transparently uses the original in-process
semaphore so local dev and the test suite behave exactly as before.

Production safety (fail closed): when `settings.redis_required` is true (the
default in production/staging - see core/config.py) and Redis is unreachable,
`acquire()` returns None - i.e. treated exactly like "no capacity available",
the same path already used when the pool is genuinely full. We deliberately
never fall back to a per-process limit in this mode: that would silently
re-introduce the "N workers x limit" bug this module exists to fix.
"""
from __future__ import annotations

import threading
import time
import uuid

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# KEYS[1] = semaphore zset key
# ARGV[1] = now_ms, ARGV[2] = lease_until_ms, ARGV[3] = capacity,
# ARGV[4] = token, ARGV[5] = lease_ms (used only to refresh the key's own TTL)
_ACQUIRE_SCRIPT = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local lease_until_ms = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])
local token = ARGV[4]
local lease_ms = tonumber(ARGV[5])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms)
local count = redis.call('ZCARD', key)
if count < capacity then
    redis.call('ZADD', key, lease_until_ms, token)
    redis.call('PEXPIRE', key, lease_ms)
    return 1
end
return 0
"""

_POLL_INTERVAL_SECONDS = 0.05
_LOCAL_TOKEN = "local"


class _LocalFallbackSemaphore:
    """The original per-process resizable semaphore (dev/test only)."""

    def __init__(self) -> None:
        self._sem = threading.Semaphore(0)
        self._capacity = 0
        self._lock = threading.Lock()

    def _ensure_capacity(self, target: int) -> None:
        with self._lock:
            while self._capacity < target:
                self._sem.release()
                self._capacity += 1
            # Never shrink live (would risk over-releasing); a lowered limit
            # takes effect as slots are returned and simply not re-added.
            self._capacity = max(self._capacity, target)

    def acquire(self, capacity: int, timeout: float) -> bool:
        self._ensure_capacity(capacity)
        return self._sem.acquire(timeout=max(0.0, timeout))

    def release(self) -> None:
        self._sem.release()


class DistributedSemaphore:
    """A named, capacity-bounded semaphore shared across all backend workers."""

    def __init__(self, name: str, lease_seconds: float | None = None) -> None:
        self._name = name
        self._key = f"ptai:sem:{name}"
        self._lease_seconds = lease_seconds
        self._local = _LocalFallbackSemaphore()
        self._script_sha: str | None = None
        self._script_lock = threading.Lock()

    def _lease_ms(self) -> int:
        seconds = self._lease_seconds
        if seconds is None:
            seconds = get_settings().redis_semaphore_lease_seconds
        return max(1, int(seconds * 1000))

    # -- mode selection ------------------------------------------------------
    def _use_redis(self) -> bool:
        from app.core.redis_client import redis_configured

        return redis_configured()

    def _redis_required(self) -> bool:
        return get_settings().redis_required

    # -- public API ------------------------------------------------------
    def acquire(self, capacity: int, timeout: float) -> str | None:
        """Reserve a slot. Returns an opaque release token, or None if no slot
        became available within `timeout` (or capacity is exhausted / Redis is
        required-and-down). `capacity` is read live by the caller from
        settings on every call, so a config change takes effect immediately."""
        if capacity <= 0:
            return None
        if not self._use_redis():
            return _LOCAL_TOKEN if self._local.acquire(capacity, timeout) else None
        return self._acquire_redis(capacity, timeout)

    def release(self, token: str | None) -> None:
        if token is None:
            return
        if token == _LOCAL_TOKEN:
            self._local.release()
            return
        self._release_redis(token)

    def active_count(self) -> int | None:
        """Fleet-wide count of currently held slots, or None if not
        measurable (Redis not configured/unreachable) - never fabricated."""
        if not self._use_redis():
            return None
        client = self._client()
        if client is None:
            return None
        try:
            now_ms = int(time.time() * 1000)
            client.zremrangebyscore(self._key, "-inf", now_ms)
            return int(client.zcard(self._key))
        except Exception:
            return None

    # -- redis path ------------------------------------------------------
    def _client(self):
        from app.core.redis_client import get_redis_client

        return get_redis_client()

    def _get_sha(self, client) -> str:
        with self._script_lock:
            if self._script_sha is None:
                self._script_sha = client.script_load(_ACQUIRE_SCRIPT)
            return self._script_sha

    def _try_once(self, client, capacity: int, token: str) -> bool:
        now_ms = int(time.time() * 1000)
        lease_ms = self._lease_ms()
        lease_until = now_ms + lease_ms
        try:
            sha = self._get_sha(client)
            result = client.evalsha(sha, 1, self._key, now_ms, lease_until, capacity, token, lease_ms)
        except Exception:
            # NOSCRIPT (Redis restarted and lost cached scripts) or a stale
            # sha - fall back to a direct eval once rather than failing.
            self._script_sha = None
            result = client.eval(_ACQUIRE_SCRIPT, 1, self._key, now_ms, lease_until, capacity, token, lease_ms)
        return bool(result)

    def _acquire_redis(self, capacity: int, timeout: float) -> str | None:
        from app.core.redis_client import ping

        client = self._client()
        if client is None or not ping():
            if self._redis_required():
                logger.warning(
                    "redis_concurrency_blocked semaphore=%s reason=redis_unavailable action=deny",
                    self._name,
                )
                return None  # fail closed: treated exactly like "no capacity"
            logger.warning(
                "redis_concurrency_local_fallback semaphore=%s reason=redis_unavailable_not_required",
                self._name,
            )
            return _LOCAL_TOKEN if self._local.acquire(capacity, timeout) else None

        token = uuid.uuid4().hex
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                if self._try_once(client, capacity, token):
                    return token
            except Exception as exc:
                logger.warning("redis_concurrency_error semaphore=%s error=%s", self._name, exc)
                if self._redis_required():
                    return None  # fail closed
                return _LOCAL_TOKEN if self._local.acquire(capacity, timeout) else None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))

    def _release_redis(self, token: str) -> None:
        client = self._client()
        if client is None:
            return
        try:
            client.zrem(self._key, token)
        except Exception as exc:
            # A held lease self-expires (PEXPIRE'd on acquire), so a failed
            # release here degrades to "slot frees up after the lease" rather
            # than leaking capacity forever.
            logger.warning("redis_release_failed semaphore=%s error=%s", self._name, exc)
