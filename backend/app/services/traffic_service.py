"""Builds the Traffic Dashboard payloads from REAL telemetry + small DB queries.

Every value here is measured: process-local telemetry counters, the live-session
registry, cheap indexed assessment-queue aggregates, the runtime config, the
SQLAlchemy pool, and (if installed) psutil for CPU/memory. Anything that cannot
be measured in the current deployment is reported as "not configured" /
"not applicable" - never fabricated.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.concurrency import interview_capacity, tts_capacity
from app.core.config import get_settings
from app.core.telemetry import get_telemetry

try:  # optional dependency; the dashboard degrades gracefully without it
    import psutil
    _PROC = psutil.Process()
    psutil.cpu_percent(interval=None)  # prime the non-blocking sampler
except Exception:  # pragma: no cover - environment dependent
    psutil = None
    _PROC = None

# Live statuses that count as an "active" interview (i.e. not finished).
_ACTIVE_INTERVIEW_STATUSES = {
    "INTERVIEWING", "WAITING_FOR_AI", "STREAMING_RESPONSE", "STREAMING_AUDIO", "ASSESSMENT_PENDING",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def http_block() -> dict:
    tele = get_telemetry()
    reqs_5m = tele.http.sum("requests", 300)
    errors_5m = tele.http.sum("5xx", 300)
    return {
        "requests_per_minute": tele.http.rate_per_min("requests", 60),
        "in_flight": tele.http_in_flight.value,
        "error_rate": round(errors_5m / reqs_5m, 4) if reqs_5m else 0.0,
        "rate_limited_last_5m": tele.http.sum("429", 300),
        "p50_ms": tele.http.percentile(300, 50),
        "p95_ms": tele.http.percentile(300, 95),
        "p99_ms": tele.http.percentile(300, 99),
        "avg_ms": tele.http.avg_latency(300),
        "requests_last_5m": reqs_5m,
    }


def provider_block(pm) -> dict:
    snap = pm.snapshot()
    snap["success_rate"] = pm.success_rate_5m()
    return snap


def server_health() -> dict:
    tele = get_telemetry()
    out: dict = {"uptime_seconds": tele.uptime_seconds()}
    if psutil is not None and _PROC is not None:
        try:
            vm = psutil.virtual_memory()
            out.update({
                "available": True,
                "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
                "system_memory_percent": round(vm.percent, 1),
                "system_memory_total_mb": round(vm.total / 1_000_000, 1),
                "process_memory_mb": round(_PROC.memory_info().rss / 1_000_000, 1),
            })
        except Exception:
            out["available"] = False
    else:
        out["available"] = False
        out["note"] = "psutil not installed - CPU/memory not available"
    return out


def db_pool_stats() -> dict:
    settings = get_settings()
    if settings.database_url.startswith("sqlite"):
        return {"applicable": False, "kind": "sqlite", "note": "SQLite (local) - no connection pool"}
    try:
        from app.database.connection import get_engine

        engine = get_engine()
        pool = engine.pool
        size = pool.size()
        checked_out = pool.checkedout()
        overflow = pool.overflow()
        max_overflow = getattr(pool, "_max_overflow", None)
        capacity = size + (max_overflow if isinstance(max_overflow, int) and max_overflow > 0 else 0)
        return {
            "applicable": True,
            "kind": type(pool).__name__,
            "size": size,
            "checked_out": checked_out,
            "overflow": overflow,
            "max_overflow": max_overflow,
            "utilization": round(checked_out / capacity, 3) if capacity else None,
        }
    except Exception:
        return {"applicable": False, "note": "pool stats unavailable"}


def assessment_block(db: Session, capacity_state: str | None = None) -> dict:
    from app.assessment import assessment_service
    from app.core import capacity as cap

    stats = assessment_service.queue_stats(db)
    stats["execution"] = "background_queue" if get_settings().assessment_queue_enabled else "synchronous"
    stats["in_flight"] = get_telemetry().assessment_in_flight.value
    stats["workers"] = get_settings().assessment_worker_concurrency
    effective, mode = cap.effective_assessment_workers(capacity_state)
    stats["effective_workers"] = effective
    stats["throttle_mode"] = mode
    return stats


def overview(db: Session) -> dict:
    from app.core import capacity as cap

    settings = get_settings()
    tele = get_telemetry()
    status_counts = tele.live.status_counts(settings.live_session_idle_prune_seconds)
    active_interviews = sum(v for k, v in status_counts.items() if k in _ACTIVE_INTERVIEW_STATUSES)
    health = server_health()
    capacity_info = cap.openai_capacity()
    payload = {
        "timestamp": _now(),
        "users": {"active": tele.live.active_users(settings.active_user_window_seconds)},
        "interviews": {
            "active": active_interviews,
            "waiting_for_ai": status_counts.get("WAITING_FOR_AI", 0) or tele.interview_in_flight.value,
            "streaming": status_counts.get("STREAMING_RESPONSE", 0) + status_counts.get("STREAMING_AUDIO", 0),
            "by_status": status_counts,
        },
        "http": http_block(),
        "openai": {**provider_block(tele.openai), "active": tele.openai.active.value},
        "elevenlabs": {**provider_block(tele.elevenlabs), "active": tele.elevenlabs.active.value},
        "openai_capacity": capacity_info,
        "assessment": assessment_block(db, capacity_info["capacity_state"]),
        "concurrency": {"interview": interview_capacity(), "tts": tts_capacity()},
        "server": {**health, "db_pool": db_pool_stats()},
    }
    payload["alerts"] = evaluate_alerts(payload)
    payload["status"] = _overall_status(payload)
    payload["insights"] = operator_insights(payload)
    return payload


def live_sessions() -> list[dict]:
    settings = get_settings()
    out = []
    for s in get_telemetry().live.list_sessions(settings.live_session_idle_prune_seconds):
        started = datetime.fromtimestamp(s.started_at, timezone.utc)
        last = datetime.fromtimestamp(s.last_activity_w, timezone.utc)
        out.append({
            "session_id": s.session_id,
            "student_name": s.student_name,
            "student_number": s.student_number,
            "case_id": s.case_id,
            "case_name": s.case_name,
            "status": s.status,
            "started_at": started.isoformat(),
            "last_activity_at": last.isoformat(),
            "duration_seconds": int((datetime.now(timezone.utc) - started).total_seconds()),
            "latest_latency_ms": s.latest_latency_ms,
        })
    return out


def history(minutes: int) -> dict:
    seconds = max(1, minutes) * 60
    samples = get_telemetry().history.recent(seconds)
    keys = ("active_users", "http_rpm", "openai_rpm", "elevenlabs_rpm", "rate_limited")
    return {
        "minutes": minutes,
        "points": [{"t": s["t"], **{k: s.get(k) for k in keys}} for s in samples],
    }


def providers(db: Session) -> dict:
    tele = get_telemetry()
    from app.services import runtime_config_service

    try:
        openai_model = runtime_config_service.openai_runtime().model
    except Exception:
        openai_model = get_settings().openai_model
    try:
        el_model = runtime_config_service.elevenlabs_runtime().default_model
    except Exception:
        el_model = get_settings().elevenlabs_default_model
    from app.core import capacity as cap

    return {
        "openai": {**provider_block(tele.openai), "active": tele.openai.active.value,
                   "model": openai_model, "capacity": cap.openai_capacity()},
        "elevenlabs": {**provider_block(tele.elevenlabs), "active": tele.elevenlabs.active.value, "model": el_model},
    }


def capacity() -> dict:
    from app.core.redis_client import redis_health

    s = get_settings()
    redis = redis_health()
    concurrency_scope = "global (redis)" if redis["status"] == "connected" else "per_process"
    return {
        "deployment_mode": s.deployment_mode,
        "app_workers": s.app_workers,
        "max_ai_interview_concurrency": s.max_concurrent_ai_interviews,
        "max_tts_concurrency": s.max_concurrent_tts_requests,
        "assessment_workers": s.assessment_worker_concurrency,
        "rate_limiter_scope": "per_process",
        "concurrency_scope": concurrency_scope,
        "redis": redis,
        "notes": {
            "global_rate_limiting": "Global limits across workers/instances require shared state such as Redis (not configured).",
            "global_concurrency": (
                "OpenAI/TTS/assessment concurrency limits are enforced fleet-wide via Redis."
                if redis["status"] == "connected"
                else "Redis is not connected - OpenAI/TTS/assessment concurrency limits are "
                     "per-process (effective limit = configured x worker count) unless "
                     "redis_required is true, in which case admission fails closed instead."
            ),
            "autoscaling": "AWS autoscaling is a later deployment phase and is not queried by the app.",
        },
        "protection": protection(),
    }


def protection() -> dict:
    from app.core.redis_client import redis_health

    s = get_settings()
    redis = redis_health()
    concurrency_scope = "global" if redis["status"] == "connected" else "per_process"
    return {
        "source": "server_environment",
        "editable": False,
        "restart_required_to_change": True,
        "rate_limiting": {
            "enabled": s.rate_limit_enabled, "scope": "per_process",
            "login": s.login_rate_limit, "interview": s.interview_rate_limit,
            "voice": s.voice_rate_limit, "assessment": s.assessment_rate_limit,
        },
        "login_throttle": {
            "enabled": s.login_throttle_enabled,
            "max_failed_attempts": s.login_max_failed_attempts,
            "lockout_seconds": s.login_lockout_seconds,
        },
        "interview_concurrency": interview_capacity(),
        "tts_concurrency": tts_capacity(),
        "concurrency_scope": concurrency_scope,
        "redis": redis,
        "assessment_execution": "background_queue" if s.assessment_queue_enabled else "synchronous",
        "retry_backoff": {"enabled": True, "max_retries": s.provider_max_retries},
        "circuit_breaker": "not_configured",
    }


# ---------------------------------------------------------------------------
#  Alerts + overall status (threshold-driven, configurable)
# ---------------------------------------------------------------------------
def evaluate_alerts(ov: dict) -> list[dict]:
    s = get_settings()
    conditions: list[tuple[str, str, str]] = []
    cap = ov.get("openai_capacity", {})
    if cap.get("capacity_state") == "CRITICAL":
        conditions.append(("openai_capacity", "CRITICAL", "OpenAI capacity critical; new assessment execution paused"))
    elif cap.get("capacity_state") == "PROTECTING":
        conditions.append(("openai_capacity", "WARNING", "OpenAI capacity in PROTECTING state"))
    http = ov["http"]
    if http.get("p95_ms") and http["p95_ms"] > s.alert_p95_latency_ms:
        conditions.append(("http_p95", "WARNING", f"API p95 latency {http['p95_ms']}ms exceeds {s.alert_p95_latency_ms}ms"))
    if http.get("error_rate", 0) > s.alert_error_rate:
        conditions.append(("http_errors", "WARNING", f"API error rate {http['error_rate']:.2%} exceeds {s.alert_error_rate:.0%}"))
    if ov["openai"].get("rate_limits_last_5m", 0) > 0:
        conditions.append(("openai_429", "WARNING", "OpenAI 429 rate-limit responses detected"))
    if ov["elevenlabs"].get("rate_limits_last_5m", 0) > 0:
        conditions.append(("elevenlabs_429", "WARNING", "ElevenLabs 429 rate-limit responses detected"))
    ic = ov["concurrency"]["interview"]
    if ic["limit"] and ic["active"] / ic["limit"] >= s.alert_ai_concurrency_pct:
        conditions.append(("ai_concurrency", "WARNING", f"AI interview concurrency at {ic['active']}/{ic['limit']}"))
    if ov["assessment"].get("pending", 0) > s.alert_assessment_queue:
        conditions.append(("assessment_queue", "WARNING", f"Assessment queue depth {ov['assessment']['pending']} exceeds {s.alert_assessment_queue}"))
    if ov["elevenlabs"].get("failure_last_5m", 0) > 0 and ov["elevenlabs"].get("success_last_5m", 0) == 0:
        conditions.append(("tts_down", "INFO", "ElevenLabs failing; interviews continue with text-only fallback"))
    server = ov["server"]
    if server.get("available"):
        if server.get("cpu_percent", 0) >= s.alert_cpu_pct * 100:
            conditions.append(("cpu", "CRITICAL", f"CPU usage {server['cpu_percent']}% is critical"))
        if server.get("system_memory_percent", 0) >= s.alert_memory_pct * 100:
            conditions.append(("memory", "CRITICAL", f"System memory {server['system_memory_percent']}% is critical"))
    pool = server.get("db_pool", {})
    if pool.get("applicable") and pool.get("utilization") is not None and pool["utilization"] >= s.alert_db_pool_pct:
        conditions.append(("db_pool", "CRITICAL", f"DB pool utilization {pool['utilization']:.0%} is critical"))
    return get_telemetry().alerts.evaluate(conditions)


def operator_insights(ov: dict) -> list[dict]:
    """Deterministic, rule-based admin insights (NO AI). Each returns a tone
    (green/yellow/orange/red/info) + message derived from real metrics."""
    out: list[dict] = []
    cap = ov["openai_capacity"]
    state = cap["capacity_state"]
    http = ov["http"]
    interviews = ov["interviews"]["active"]
    iq = ov["concurrency"]["interview"]
    assess = ov["assessment"]

    # OpenAI capacity
    if state == "CRITICAL":
        out.append({"tone": "red", "message": "OpenAI capacity is critical; new assessment execution is paused to protect live interviews."})
    elif state == "PROTECTING":
        out.append({"tone": "orange", "message": "OpenAI capacity is PROTECTING; assessment throughput is minimized to protect live interviews."})
    elif state == "BUSY":
        out.append({"tone": "yellow", "message": "OpenAI usage has entered the BUSY state; assessment worker capacity has been reduced."})
    else:
        out.append({"tone": "green", "message": f"OpenAI capacity headroom is healthy ({int((1 - cap['utilization_pct']) * 100)}% free)."})

    # 429s
    if ov["openai"].get("rate_limits_last_5m", 0) > 0 or http.get("rate_limited_last_5m", 0) > 0:
        out.append({"tone": "red", "message": "429 responses detected in the last 5 minutes."})

    # Interview queue pressure
    if iq.get("wait_p95_ms") and iq["wait_p95_ms"] > 1000:
        out.append({"tone": "orange", "message": f"Interview queue wait p95 is elevated ({iq['wait_p95_ms']} ms)."})
    if iq.get("timeouts_5m", 0) > 0:
        out.append({"tone": "orange", "message": f"{iq['timeouts_5m']} interview requests hit a capacity timeout in the last 5 minutes."})

    # Assessment queue / throttle
    if assess.get("throttle_mode") == "PAUSED":
        out.append({"tone": "red", "message": "Assessment claiming is PAUSED; in-flight jobs will finish and claiming resumes when capacity recovers."})
    elif assess.get("throttle_mode") in ("REDUCED", "MINIMAL"):
        out.append({"tone": "yellow", "message": f"Assessment worker capacity reduced to {assess.get('effective_workers')} to protect live interviews."})
    elif assess.get("pending", 0) == 0 and assess.get("processing", 0) == 0:
        out.append({"tone": "green", "message": "Assessment queue is empty; all workers are available."})

    # TTS degradation
    if ov["elevenlabs"].get("failure_last_5m", 0) > 0 and ov["elevenlabs"].get("success_last_5m", 0) == 0:
        out.append({"tone": "info", "message": "TTS is degrading to text-only due to provider saturation."})

    # Idle / healthy summary
    if state == "NORMAL" and interviews == 0 and http.get("error_rate", 0) == 0:
        out.insert(0, {"tone": "green", "message": "System is healthy and idle. No live interviews are active."})

    return out


def _overall_status(ov: dict) -> str:
    """Coarse traffic-light for the 'Server Status' card."""
    alerts = ov.get("alerts", [])
    # Only alerts currently firing (the log includes recent history); use live conditions.
    s = get_settings()
    server = ov["server"]
    critical = False
    elevated = False
    if server.get("available"):
        cpu = server.get("cpu_percent", 0)
        mem = server.get("system_memory_percent", 0)
        critical = cpu >= s.alert_cpu_pct * 100 or mem >= s.alert_memory_pct * 100
        elevated = cpu >= s.health_elevated_cpu_pct * 100 or mem >= s.health_elevated_memory_pct * 100
    if ov["http"].get("error_rate", 0) > s.alert_error_rate:
        elevated = True
    pool = server.get("db_pool", {})
    if pool.get("applicable") and pool.get("utilization") and pool["utilization"] >= s.alert_db_pool_pct:
        critical = True
    if critical:
        return "critical"
    if elevated:
        return "elevated"
    return "healthy"
