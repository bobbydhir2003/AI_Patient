"""Real cross-worker presence monitoring, backed by Redis.

Uvicorn runs N worker PROCESSES. There is no built-in way for one worker (the
one that happens to serve the System Dashboard request) to see the others, so
this module gives each worker a heartbeat: on startup it writes a record into
Redis keyed by a unique identity (hostname + PID), and a small daemon thread
refreshes that record every few seconds with a short TTL. If a worker crashes
or is killed, its record simply EXPIRES out of Redis after the TTL - no reaper
process needed - and it disappears from the observed fleet. On graceful
shutdown the worker deletes its own record immediately.

Honesty rules (the whole point of this module):
- The observed fleet is DERIVED from live Redis records, never from config.
  `configured` (settings.app_workers) and `observed` (len of live records) are
  reported separately so a mismatch is visible, not hidden.
- Every per-worker value in a heartbeat is SELF-REPORTED by that worker from
  its own real telemetry / process state (PID, uptime, requests handled,
  in-flight counts, RSS memory). Nothing is invented to fill the UI.
- Without Redis (local/dev default) there is no shared store, so the fleet
  cannot be observed across processes. We report that honestly as
  "local_only": we still surface THIS process's real record, but never claim
  fleet-wide health. With Redis configured-but-unreachable we report
  "unavailable".

Redis keys / TTL:
- key:   ptai:worker:{hostname}:{pid}
- value: JSON heartbeat payload (see _build_payload)
- TTL:   settings.worker_heartbeat_ttl_seconds (record self-expires; refreshed
         on every heartbeat, roughly every worker_heartbeat_interval_seconds)
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import datetime, timezone

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_KEY_PREFIX = "ptai:worker:"

try:  # optional; per-worker RSS memory is omitted (not faked) when unavailable
    import psutil

    _PROC = psutil.Process()
except Exception:  # pragma: no cover - environment dependent
    psutil = None
    _PROC = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def worker_id() -> str:
    """Stable identity for THIS process: hostname + PID. Unique per worker."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _key(wid: str) -> str:
    return f"{_KEY_PREFIX}{wid}"


class WorkerHeartbeat:
    """Owns this process's Redis presence record + the heartbeat thread."""

    def __init__(self) -> None:
        self._wid = worker_id()
        self._started_at = time.time()
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._lock = threading.Lock()

    # -- payload -------------------------------------------------------------
    def _build_payload(self) -> dict:
        """Snapshot of THIS worker's real state. Every field is measured; a
        field that cannot be measured (e.g. RSS without psutil) is omitted."""
        from app.core.telemetry import get_telemetry

        tele = get_telemetry()
        payload = {
            "worker_id": self._wid,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": self._started_at,
            "uptime_seconds": int(time.time() - self._started_at),
            "heartbeat_at": _now_iso(),
            # Real per-worker telemetry (this process only):
            "requests_total": tele.http_requests_total(),
            "requests_per_minute": tele.http.rate_per_min("requests", 60),
            "http_in_flight": tele.http_in_flight.value,
            "interview_in_flight": tele.interview_in_flight.value,
            "tts_in_flight": tele.tts_in_flight.value,
            "assessment_in_flight": tele.assessment_in_flight.value,
        }
        if _PROC is not None:
            try:
                payload["memory_mb"] = round(_PROC.memory_info().rss / 1_000_000, 1)
            except Exception:
                pass  # omit rather than fabricate
        return payload

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        """Register + begin heartbeating. No-op (safe) when Redis is not
        configured - nothing to write to, so there is no local heartbeat."""
        from app.core.redis_client import redis_configured

        if not redis_configured():
            return
        with self._lock:
            if self._thread is not None:
                return
            self._stop = threading.Event()
            self._beat_once()  # register immediately so the fleet is visible at once
            interval = max(1, int(get_settings().worker_heartbeat_interval_seconds))

            def _loop() -> None:
                while not self._stop.wait(interval):
                    self._beat_once()

            self._thread = threading.Thread(target=_loop, name="worker-heartbeat", daemon=True)
            self._thread.start()
            logger.info("worker_heartbeat_started worker_id=%s interval=%ds", self._wid, interval)

    def _beat_once(self) -> None:
        from app.core.redis_client import get_redis_client

        client = get_redis_client()
        if client is None:
            return
        ttl = max(2, int(get_settings().worker_heartbeat_ttl_seconds))
        try:
            client.set(_key(self._wid), json.dumps(self._build_payload()), ex=ttl)
        except Exception as exc:  # a failed heartbeat degrades to TTL expiry, never crashes
            logger.warning("worker_heartbeat_write_failed worker_id=%s error=%s", self._wid, exc)

    def stop(self) -> None:
        with self._lock:
            if self._stop is not None:
                self._stop.set()
            self._thread = None
        self._unregister()

    def _unregister(self) -> None:
        from app.core.redis_client import get_redis_client

        client = get_redis_client()
        if client is None:
            return
        try:
            client.delete(_key(self._wid))
        except Exception:
            pass  # record self-expires via TTL if delete fails

    def local_payload(self) -> dict:
        """This process's real record, for the local-only (no-Redis) view."""
        return self._build_payload()


_heartbeat: WorkerHeartbeat | None = None
_hb_lock = threading.Lock()


def get_heartbeat() -> WorkerHeartbeat:
    global _heartbeat
    with _hb_lock:
        if _heartbeat is None:
            _heartbeat = WorkerHeartbeat()
        return _heartbeat


# ---------------------------------------------------------------------------
#  Observed fleet (read side, used by the dashboard)
# ---------------------------------------------------------------------------
def observed_workers() -> list[dict] | None:
    """Live worker records from Redis, or None if Redis is not
    configured/unreachable (i.e. the fleet cannot be observed). Never
    fabricated: an expired heartbeat is simply absent."""
    from app.core.redis_client import get_redis_client, ping, redis_configured

    if not redis_configured():
        return None
    client = get_redis_client()
    if client is None or not ping():
        return None
    try:
        workers: list[dict] = []
        for raw_key in client.scan_iter(match=f"{_KEY_PREFIX}*", count=100):
            try:
                raw = client.get(raw_key)
                if raw is None:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                workers.append(json.loads(raw))
            except Exception:
                continue  # skip a malformed/racing record rather than fabricating
        workers.sort(key=lambda w: (w.get("hostname", ""), w.get("pid", 0)))
        return workers
    except Exception as exc:
        logger.warning("worker_registry_scan_failed error=%s", exc)
        return None


def fleet_status(configured: int, observed_count: int | None) -> str:
    """Derive fleet health from real numbers only."""
    if observed_count is None:
        return "unavailable"
    if observed_count == 0:
        return "unavailable"
    if observed_count == configured:
        return "healthy"
    return "degraded"  # observed < or > configured is a real mismatch
