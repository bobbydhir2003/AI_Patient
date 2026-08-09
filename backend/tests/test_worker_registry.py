"""Tests for real, Redis-backed worker presence monitoring (Part 2).

Uses a fake Redis double that implements exactly the commands the registry
issues (set with EX, get, delete, scan_iter, ping) AND honestly simulates TTL
expiry against a virtual clock - so 'a stopped heartbeat expires and the worker
disappears' is exercised for real, not asserted by fiat. No live Redis needed.

Honesty is the whole point: we verify that observed workers are DERIVED from
live records, that a missing store yields local_only (never a fabricated 4/4),
that an unreachable store is reported unavailable, and that no worker is ever
invented to fill the fleet.
"""
import json

import pytest

from app.core import worker_registry
from app.core.config import get_settings


# --------------------------------------------------------------------------
#  Fake Redis with real TTL-expiry semantics against a virtual clock
# --------------------------------------------------------------------------
class _FakeRedis:
    def __init__(self):
        self.store: dict[str, tuple[str, float]] = {}  # key -> (value, expires_at)
        self.now = 1000.0
        self.up = True

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _live(self, key: str):
        item = self.store.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at is not None and self.now >= expires_at:
            del self.store[key]  # TTL expiry: the record is gone
            return None
        return value

    def ping(self):
        if not self.up:
            raise Exception("redis down")
        return True

    def set(self, key, value, ex=None):
        expires_at = (self.now + ex) if ex is not None else None
        self.store[key] = (value, expires_at)
        return True

    def get(self, key):
        return self._live(key)

    def delete(self, key):
        self.store.pop(key, None)

    def scan_iter(self, match=None, count=100):
        prefix = (match or "").rstrip("*")
        for key in list(self.store.keys()):
            if self._live(key) is None:
                continue  # skip expired
            if key.startswith(prefix):
                yield key


@pytest.fixture()
def fake_redis(monkeypatch):
    from app.core import redis_client

    client = _FakeRedis()
    s = get_settings()
    monkeypatch.setattr(s, "redis_url", "redis://fake/0")
    monkeypatch.setattr(redis_client, "get_redis_client", lambda: client)
    monkeypatch.setattr(redis_client, "redis_configured", lambda: True)
    monkeypatch.setattr(redis_client, "ping", lambda: client.up)
    return client


def _write_worker(client, wid, *, ttl=12, now_offset=0, pid=1234, requests_total=5):
    """Directly write a worker record (as a heartbeat would)."""
    client.now += now_offset
    payload = {
        "worker_id": wid, "pid": pid, "hostname": wid.split(":")[0],
        "started_at": client.now - 100, "uptime_seconds": 100,
        "heartbeat_at": "2026-01-01T00:00:00+00:00", "requests_total": requests_total,
        "requests_per_minute": 1.0, "http_in_flight": 0,
        "interview_in_flight": 0, "tts_in_flight": 0, "assessment_in_flight": 0,
        "memory_mb": 50.0,
    }
    client.set(f"ptai:worker:{wid}", json.dumps(payload), ex=ttl)


# --------------------------------------------------------------------------
#  register + heartbeat + TTL
# --------------------------------------------------------------------------
def test_worker_registers_in_redis(fake_redis):
    hb = worker_registry.WorkerHeartbeat()
    hb._beat_once()
    workers = worker_registry.observed_workers()
    assert workers is not None
    assert len(workers) == 1
    assert workers[0]["pid"] == __import__("os").getpid()  # real PID, self-reported


def test_heartbeat_refreshes_ttl(fake_redis):
    hb = worker_registry.WorkerHeartbeat()
    hb._beat_once()
    key = f"ptai:worker:{worker_registry.worker_id()}"
    _, first_expiry = fake_redis.store[key]
    fake_redis.advance(5)          # time passes...
    hb._beat_once()                # ...heartbeat again
    _, second_expiry = fake_redis.store[key]
    assert second_expiry > first_expiry  # TTL was pushed forward


def test_expired_worker_disappears_from_fleet(fake_redis):
    ttl = get_settings().worker_heartbeat_ttl_seconds
    _write_worker(fake_redis, "hostA:1", ttl=ttl)
    assert len(worker_registry.observed_workers()) == 1
    fake_redis.advance(ttl + 1)    # heartbeat stops, TTL lapses
    assert worker_registry.observed_workers() == []  # gone, not fabricated


def test_unregister_removes_record(fake_redis):
    hb = worker_registry.WorkerHeartbeat()
    hb._beat_once()
    assert len(worker_registry.observed_workers()) == 1
    hb._unregister()
    assert worker_registry.observed_workers() == []


# --------------------------------------------------------------------------
#  fleet health derivation (real numbers only)
# --------------------------------------------------------------------------
def test_configured_equals_observed_is_healthy():
    assert worker_registry.fleet_status(4, 4) == "healthy"


def test_configured_gt_observed_is_degraded():
    assert worker_registry.fleet_status(4, 3) == "degraded"


def test_no_observed_is_unavailable():
    assert worker_registry.fleet_status(4, None) == "unavailable"
    assert worker_registry.fleet_status(4, 0) == "unavailable"


def test_service_fleet_healthy_when_matched(fake_redis, monkeypatch):
    from app.services import system_service

    s = get_settings()
    monkeypatch.setattr(s, "app_workers", 2)
    _write_worker(fake_redis, "hostA:1")
    _write_worker(fake_redis, "hostA:2")
    fleet = system_service.worker_fleet()
    assert fleet.monitoring == "observed"
    assert fleet.observed == 2
    assert fleet.configured == 2
    assert fleet.status == "healthy"
    # every worker carries real self-reported values, current_task stays null
    assert all(w.current_task is None for w in fleet.workers)
    assert all(w.pid is not None for w in fleet.workers)


def test_service_fleet_degraded_when_short(fake_redis, monkeypatch):
    from app.services import system_service

    s = get_settings()
    monkeypatch.setattr(s, "app_workers", 4)
    _write_worker(fake_redis, "hostA:1")
    _write_worker(fake_redis, "hostA:2")
    _write_worker(fake_redis, "hostA:3")
    fleet = system_service.worker_fleet()
    assert fleet.observed == 3
    assert fleet.configured == 4
    assert fleet.status == "degraded"


def test_redis_unavailable_is_reported_honestly(fake_redis, monkeypatch):
    from app.services import system_service

    fake_redis.up = False  # configured but unreachable
    fleet = system_service.worker_fleet()
    assert fleet.monitoring == "unavailable"
    assert fleet.status == "unavailable"
    assert fleet.observed is None
    assert fleet.workers == []  # NO fabricated workers


def test_no_redis_is_local_only(monkeypatch):
    """Default dev/single-process: no Redis => local_only, showing only THIS
    process, never a fabricated fleet."""
    from app.core import redis_client
    from app.services import system_service

    monkeypatch.setattr(redis_client, "redis_configured", lambda: False)
    fleet = system_service.worker_fleet()
    assert fleet.monitoring == "local_only"
    assert fleet.observed is None
    assert len(fleet.workers) == 1  # just us
    assert fleet.workers[0].pid == __import__("os").getpid()


def test_malformed_record_is_skipped_not_fabricated(fake_redis):
    fake_redis.set("ptai:worker:bad:1", "{not json", ex=12)
    _write_worker(fake_redis, "hostA:2")
    workers = worker_registry.observed_workers()
    assert len(workers) == 1  # the malformed one is skipped, not invented
    assert workers[0]["worker_id"] == "hostA:2"
