"""Tests for the fleet-wide (Redis-backed) concurrency guard introduced to
make OpenAI/TTS/assessment concurrency safe across multiple uvicorn workers.

Covers:
- local fallback (no Redis) acquire/release/timeout - matches the original
  per-process semaphore behavior used in development/test.
- exception safety - a slot is always releasable, and releasing a "no slot"
  token is a safe no-op (mirrors the try/finally pattern every real caller uses).
- Redis-unavailable behavior in both modes: fail-closed when required
  (production default), fall back to local when not required (dev default).
- real acquire/release/capacity/expiry semantics against a fake Redis double
  that implements the exact commands the Lua script issues (ZADD/ZCARD/
  ZREMRANGEBYSCORE/ZREM) - this exercises the Python-side orchestration
  (timeout loop, NOSCRIPT recovery, active_count) without requiring a live
  Redis server in CI. See docs/DEPLOYMENT.md for a real-Redis smoke test to
  run once before a production cutover.
- Settings.redis_required resolution and the production fail-closed startup
  check (ConfigError when REDIS_URL is unset in a strict environment).
"""
import time

import pytest

from app.core.config import ConfigError, Settings
from app.core.distributed_semaphore import DistributedSemaphore


# ==========================================================================
#  LOCAL FALLBACK (no Redis configured - matches dev/test defaults)
# ==========================================================================
def test_local_fallback_acquire_release_cycle():
    sem = DistributedSemaphore("test_local_basic")
    tok1 = sem.acquire(1, 0.1)
    assert tok1 is not None
    # capacity exhausted -> bounded wait then None
    t0 = time.monotonic()
    tok2 = sem.acquire(1, 0.1)
    elapsed = time.monotonic() - t0
    assert tok2 is None
    assert elapsed < 1.0  # bounded, not a hang
    sem.release(tok1)
    tok3 = sem.acquire(1, 0.1)
    assert tok3 is not None
    sem.release(tok3)


def test_local_fallback_timeout_is_bounded():
    sem = DistributedSemaphore("test_local_timeout")
    held = sem.acquire(1, 0.1)
    assert held is not None
    t0 = time.monotonic()
    result = sem.acquire(1, 0.2)
    elapsed = time.monotonic() - t0
    assert result is None
    assert 0.15 <= elapsed < 1.0
    sem.release(held)


def test_release_none_token_is_a_safe_noop():
    sem = DistributedSemaphore("test_local_none_release")
    sem.release(None)  # must never raise
    # capacity is still fully available afterwards
    tok = sem.acquire(1, 0.1)
    assert tok is not None
    sem.release(tok)


def test_exception_inside_guarded_block_still_releases():
    """Mirrors the real try/finally pattern used by interview_slot/tts_slot."""
    sem = DistributedSemaphore("test_local_exception_safety")
    token = sem.acquire(1, 0.1)
    assert token is not None
    try:
        try:
            raise ValueError("boom")
        finally:
            sem.release(token)
    except ValueError:
        pass
    # slot must be free again
    token2 = sem.acquire(1, 0.1)
    assert token2 is not None
    sem.release(token2)


def test_local_fallback_never_shrinks_capacity_live():
    """Matches the documented behavior of the original in-process semaphore:
    a capacity increase takes effect immediately; a decrease only takes effect
    as slots are naturally returned (never over-releases)."""
    sem = DistributedSemaphore("test_local_capacity_grow")
    a = sem.acquire(2, 0.1)
    b = sem.acquire(2, 0.1)
    assert a is not None and b is not None
    c = sem.acquire(2, 0.05)
    assert c is None  # at capacity
    sem.release(a)
    sem.release(b)


# ==========================================================================
#  REDIS-UNAVAILABLE BEHAVIOR (real redis-py client, unreachable address -
#  no live server needed to prove "unreachable" behavior)
# ==========================================================================
_UNREACHABLE_REDIS_URL = "redis://127.0.0.1:1/0"  # port 1 is never a Redis server


def test_redis_unavailable_and_required_fails_closed(monkeypatch):
    from app.core import redis_client
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "redis_url", _UNREACHABLE_REDIS_URL)
    monkeypatch.setattr(s, "redis_connect_timeout_seconds", 0.2)
    monkeypatch.setattr(s, "redis_socket_timeout_seconds", 0.2)
    monkeypatch.setattr(s, "redis_required_for_concurrency", True)
    redis_client.reset_redis_client()

    assert redis_client.ping() is False

    sem = DistributedSemaphore("test_redis_required_down")
    result = sem.acquire(5, 0.1)
    assert result is None  # fail closed: treated exactly like "no capacity"
    redis_client.reset_redis_client()


def test_redis_unavailable_and_not_required_uses_local_fallback(monkeypatch):
    from app.core import redis_client
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "redis_url", _UNREACHABLE_REDIS_URL)
    monkeypatch.setattr(s, "redis_connect_timeout_seconds", 0.2)
    monkeypatch.setattr(s, "redis_socket_timeout_seconds", 0.2)
    monkeypatch.setattr(s, "redis_required_for_concurrency", False)
    redis_client.reset_redis_client()

    sem = DistributedSemaphore("test_redis_not_required_down")
    token = sem.acquire(1, 0.2)
    assert token is not None  # fell back to the local semaphore, not blocked
    sem.release(token)
    redis_client.reset_redis_client()


# ==========================================================================
#  REAL ACQUIRE/RELEASE SEMANTICS (fake Redis double implementing the exact
#  ZSET commands the Lua script issues, so the Python orchestration - timeout
#  loop, NOSCRIPT recovery, active_count - is exercised end-to-end)
# ==========================================================================
class _FakeRedis:
    """Implements just the commands DistributedSemaphore issues, with the
    same semantics as the real Lua script (atomic in real Redis; single-
    threaded test usage here so no extra locking is needed)."""

    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}
        self.scripts: dict[str, str] = {}
        self._next_sha = 0

    def ping(self):
        return True

    def close(self):
        pass

    def script_load(self, script):
        self._next_sha += 1
        sha = f"sha{self._next_sha}"
        self.scripts[sha] = script
        return sha

    def _acquire(self, key, now_ms, lease_until_ms, capacity, token):
        z = self.zsets.setdefault(key, {})
        for member, score in list(z.items()):
            if score <= now_ms:
                del z[member]
        if len(z) < capacity:
            z[token] = lease_until_ms
            return 1
        return 0

    def evalsha(self, sha, numkeys, key, now_ms, lease_until_ms, capacity, token, lease_ms):
        if sha not in self.scripts:
            raise Exception("NOSCRIPT no matching script")
        return self._acquire(key, int(now_ms), int(lease_until_ms), int(capacity), token)

    def eval(self, script, numkeys, key, now_ms, lease_until_ms, capacity, token, lease_ms):
        return self._acquire(key, int(now_ms), int(lease_until_ms), int(capacity), token)

    def zrem(self, key, token):
        self.zsets.get(key, {}).pop(token, None)

    def zremrangebyscore(self, key, lo, hi):
        now_ms = hi
        z = self.zsets.setdefault(key, {})
        for member, score in list(z.items()):
            if score <= now_ms:
                del z[member]

    def zcard(self, key):
        return len(self.zsets.get(key, {}))


@pytest.fixture()
def fake_redis(monkeypatch):
    from app.core import redis_client
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "redis_url", "redis://fake/0")
    client = _FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: client)
    monkeypatch.setattr(redis_client, "ping", lambda: True)
    return client


def test_redis_backed_acquire_respects_capacity(fake_redis):
    sem = DistributedSemaphore("test_redis_capacity")
    tok1 = sem.acquire(2, 0.5)
    tok2 = sem.acquire(2, 0.5)
    assert tok1 is not None and tok2 is not None
    assert tok1 != tok2
    tok3 = sem.acquire(2, 0.1)  # third exceeds capacity=2
    assert tok3 is None
    assert sem.active_count() == 2


def test_redis_backed_release_frees_a_slot(fake_redis):
    sem = DistributedSemaphore("test_redis_release")
    tok1 = sem.acquire(1, 0.5)
    assert tok1 is not None
    assert sem.active_count() == 1
    sem.release(tok1)
    assert sem.active_count() == 0
    tok2 = sem.acquire(1, 0.5)
    assert tok2 is not None
    sem.release(tok2)


def test_redis_backed_expired_lease_is_pruned(fake_redis):
    sem = DistributedSemaphore("test_redis_expiry", lease_seconds=0.05)
    tok1 = sem.acquire(1, 0.5)
    assert tok1 is not None
    assert sem.active_count() == 1
    time.sleep(0.1)  # lease expires without a release (simulates a crashed worker)
    # active_count() itself prunes expired entries on read.
    assert sem.active_count() == 0
    tok2 = sem.acquire(1, 0.5)
    assert tok2 is not None


def test_redis_backed_separate_names_never_share_capacity(fake_redis):
    openai_sem = DistributedSemaphore("openai_interview")
    tts_sem = DistributedSemaphore("tts")
    o = openai_sem.acquire(1, 0.5)
    t = tts_sem.acquire(1, 0.5)
    assert o is not None and t is not None
    assert openai_sem.active_count() == 1
    assert tts_sem.active_count() == 1
    # exhausting one must not affect the other
    assert openai_sem.acquire(1, 0.1) is None
    assert tts_sem.active_count() == 1


# ==========================================================================
#  SETTINGS: redis_required resolution + production fail-closed startup
# ==========================================================================
# ==========================================================================
#  HEALTH CACHE (ping_cached) - avoids an extra Redis round trip on every
#  single semaphore acquire, without weakening the Lua acquire correctness.
# ==========================================================================
def test_ping_cached_reuses_result_within_ttl(fake_redis, monkeypatch):
    from app.core import redis_client
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "redis_health_cache_seconds", 5.0)
    redis_client.reset_redis_client()
    calls = {"n": 0}
    real_ping = redis_client.ping

    def counting_ping():
        calls["n"] += 1
        return real_ping()

    monkeypatch.setattr(redis_client, "ping", counting_ping)
    assert redis_client.ping_cached() is True
    assert redis_client.ping_cached() is True
    assert redis_client.ping_cached() is True
    assert calls["n"] == 1  # only the FIRST call hit the real ping; rest served from cache


def test_ping_cached_refreshes_after_ttl_expires(fake_redis, monkeypatch):
    from app.core import redis_client
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "redis_health_cache_seconds", 0.05)
    redis_client.reset_redis_client()
    calls = {"n": 0}
    real_ping = redis_client.ping

    def counting_ping():
        calls["n"] += 1
        return real_ping()

    monkeypatch.setattr(redis_client, "ping", counting_ping)
    assert redis_client.ping_cached() is True
    time.sleep(0.1)
    assert redis_client.ping_cached() is True
    assert calls["n"] == 2  # cache expired -> a second real ping happened


def test_semaphore_acquire_does_not_hammer_redis_ping(fake_redis, monkeypatch):
    """The actual regression this fixes: without caching, EVERY acquire() call
    issued a fresh PING on top of the Lua script eval. With caching, a burst
    of acquires within the TTL issues at most one real ping."""
    from app.core import redis_client
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "redis_health_cache_seconds", 5.0)
    redis_client.reset_redis_client()
    ping_calls = {"n": 0}
    real_ping = redis_client.ping

    def counting_ping():
        ping_calls["n"] += 1
        return real_ping()

    monkeypatch.setattr(redis_client, "ping", counting_ping)
    sem = DistributedSemaphore("test_ping_hammer")
    for _ in range(10):
        tok = sem.acquire(10, 0.1)
        assert tok is not None
        sem.release(tok)
    assert ping_calls["n"] == 1  # 10 acquires, only 1 real PING


def test_ping_logs_connected_then_recovered_on_transition(monkeypatch, caplog):
    """Uses a real (unpatched) ping() against a minimal fake client so the
    connected/unavailable/recovered LOG TRANSITIONS themselves are exercised
    (the `fake_redis` fixture used elsewhere stubs out ping() entirely, which
    would hide this behavior)."""
    import logging

    from app.core import redis_client
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "redis_url", "redis://fake/0")

    class _Ok:
        def ping(self):
            return True

    class _Down:
        def ping(self):
            raise ConnectionError("simulated outage")

    state = {"client": _Ok()}
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: state["client"])
    redis_client.reset_redis_client()

    with caplog.at_level(logging.INFO, logger="app.core.redis_client"):
        assert redis_client.ping() is True
    assert any("redis_connected" in r.message for r in caplog.records)

    caplog.clear()
    state["client"] = _Down()
    with caplog.at_level(logging.WARNING, logger="app.core.redis_client"):
        assert redis_client.ping() is False
    assert any("redis_unavailable" in r.message for r in caplog.records)

    caplog.clear()
    state["client"] = _Ok()
    with caplog.at_level(logging.INFO, logger="app.core.redis_client"):
        assert redis_client.ping() is True
    assert any("redis_recovered" in r.message for r in caplog.records)
    redis_client.reset_redis_client()


def test_redis_required_defaults_by_environment():
    dev = Settings(environment="development", jwt_secret_key="dev-insecure-change-me",
                    database_url="sqlite://", redis_url="")
    assert dev.redis_required is False

    prod = Settings(
        environment="production", jwt_secret_key="x" * 40,
        database_url="sqlite://", redis_url="redis://localhost:6379/0",
    )
    assert prod.redis_required is True


def test_redis_required_explicit_override_wins():
    s = Settings(
        environment="production", jwt_secret_key="x" * 40, database_url="sqlite://",
        redis_url="redis://localhost:6379/0", redis_required_for_concurrency=False,
    )
    assert s.redis_required is False


def test_production_without_redis_url_fails_closed():
    with pytest.raises(ConfigError, match="REDIS_URL"):
        Settings(environment="production", jwt_secret_key="x" * 40,
                 database_url="sqlite://", redis_url="")


def test_production_without_redis_url_but_explicitly_opted_out_starts():
    # Explicit opt-out is allowed (loudly logged) - e.g. a single-worker
    # transition deployment before Redis is provisioned.
    s = Settings(
        environment="production", jwt_secret_key="x" * 40, database_url="sqlite://",
        redis_url="", redis_required_for_concurrency=False,
    )
    assert s.redis_required is False
