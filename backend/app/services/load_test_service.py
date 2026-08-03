"""Load & Capacity Testing controller (Priority J).

Responsibilities:
  - create / get / stop / list load-test jobs (super-admin only, enforced at API)
  - enforce the single-run guard (one heavy test at a time)
  - require explicit confirmation before any REAL (paid) provider traffic
  - provision + reuse ISOLATED `is_load_test` accounts (never real students)
  - launch the load generator in a SEPARATE process (load_tests.worker) - never
    inside the FastAPI request/event loop
  - read the worker's REAL measured snapshots, merge the backend's own REAL
    live telemetry (provider activity, infra health), and compute a transparent
    capacity analysis on completion. Persist only summary metadata.

No metric, capacity number, or provider figure is ever fabricated here. When a
value cannot be measured it is returned as null / "Not available".
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import (
    LoadTestConfirmationError,
    LoadTestConflictError,
    LoadTestDisabledError,
    LoadTestNotFoundError,
    ValidationFailedError,
)
from app.core.logging import get_logger
from app.core.security import hash_password
from app.database.connection import get_session_factory
from app.models import Student, User
from app.models.load_test_job import (
    ACTIVE_STATUSES,
    PROVIDER_SIMULATED,
    REAL_PROVIDER_MODES,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_STARTING,
    STATUS_STOPPING,
    LoadTestJob,
)
from app.schemas.load_test_schema import PROVIDER_MODES, TEST_TYPES
from app.services import load_capacity_analysis, runtime_config_service

logger = get_logger(__name__)

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
METRICS_DIR = os.path.join(tempfile.gettempdir(), "pt_load_tests")

# Isolated load-test accounts share one throwaway password. These accounts are
# flagged is_load_test=True and are NEVER real students; their sessions/turns
# are load-test artifacts, not academic records.
_LOAD_TEST_PASSWORD = "load-test-only-do-not-use-x9Q2"
_LOAD_TEST_EMAIL_FMT = "loadtest+{n:03d}@loadtest.invalid"
_USER_CAP = 500  # never provision more accounts than this regardless of request

# In-process registry of running child processes (single-run guard keeps size<=1).
_RUNNING: dict[str, dict] = {}
_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _metrics_path(job_id: str) -> str:
    return os.path.join(METRICS_DIR, f"{job_id}.json")


# ------------------------------------------------------------------ isolation
def provision_test_users(db: Session, count: int) -> list[dict]:
    """Reuse existing is_load_test accounts, creating any that are missing.

    Returns login credentials for `count` isolated accounts. All accounts are
    ACTIVE students flagged is_load_test=True."""
    count = max(1, min(count, _USER_CAP))
    existing = list(
        db.execute(
            select(User).where(User.is_load_test.is_(True)).order_by(User.email)
        ).scalars()
    )
    creds: list[dict] = []
    pw_hash = hash_password(_LOAD_TEST_PASSWORD)
    for i in range(count):
        email = _LOAD_TEST_EMAIL_FMT.format(n=i)
        user = next((u for u in existing if u.email == email), None)
        if user is None:
            student = Student(name=f"Load Test {i:03d}", student_number=f"LT{i:03d}", email=email)
            db.add(student)
            db.flush()
            user = User(
                email=email, password_hash=pw_hash, full_name=f"Load Test {i:03d}",
                student_number=f"LT{i:03d}", role="student", student_id=student.id,
                is_active=True, is_load_test=True,
            )
            user.account_status = "ACTIVE"
            db.add(user)
            db.flush()
        else:
            # Ensure the account can still log in with the shared password + is active.
            user.password_hash = pw_hash
            user.is_active = True
            user.account_status = "ACTIVE"
            user.is_load_test = True
        creds.append({"email": email, "password": _LOAD_TEST_PASSWORD, "name": f"Load Test {i:03d}"})
    db.flush()
    return creds


# --------------------------------------------------------------------- guard
def active_job(db: Session) -> LoadTestJob | None:
    return db.execute(
        select(LoadTestJob).where(LoadTestJob.status.in_(ACTIVE_STATUSES))
        .order_by(LoadTestJob.created_at.desc())
    ).scalars().first()


# -------------------------------------------------------------- worker params
def _derive_worker_params(test_type: str, provider_mode: str) -> dict:
    enable_tts = provider_mode == "REAL_OPENAI_TTS" or test_type == "tts_traffic"
    assessment = test_type in ("concurrent", "soak", "stress")
    turns = {"smoke": 3, "spike": 3, "ai_traffic": 6, "stress": 6}.get(test_type, 4)
    think = {"spike": 200, "soak": 1500}.get(test_type, 800)
    return {"enable_tts": enable_tts, "assessment": assessment, "turns": turns, "think_ms": think}


def _launch_worker(job: LoadTestJob, creds_file: str) -> subprocess.Popen:
    os.makedirs(METRICS_DIR, exist_ok=True)
    params = _derive_worker_params(job.test_type, job.provider_mode)
    settings = get_settings()
    argv = [
        sys.executable, "-m", "load_tests.worker",
        "--job-id", job.id,
        "--metrics-dir", METRICS_DIR,
        "--credentials-file", creds_file,
        "--base-url", settings.load_test_target_base_url,
        "--target-users", str(job.target_users),
        "--ramp-seconds", str(job.ramp_seconds),
        "--duration-seconds", str(job.duration_seconds),
        "--turns", str(params["turns"]),
        "--think-time-ms", str(params["think_ms"]),
        "--test-type", job.test_type,
        "--provider-mode", job.provider_mode,
    ]
    if params["enable_tts"]:
        argv.append("--enable-tts")
    if params["assessment"]:
        argv.append("--assessment")
    argv.append("--complete")
    return subprocess.Popen(argv, cwd=BACKEND_DIR)


# --------------------------------------------------------------------- create
def create_job(db: Session, *, req, created_by: str) -> LoadTestJob:
    settings = get_settings()
    if not settings.load_test_enabled:
        raise LoadTestDisabledError()

    if req.test_type not in TEST_TYPES:
        raise ValidationFailedError(f"Unknown test type '{req.test_type}'.")
    if req.provider_mode not in PROVIDER_MODES:
        raise ValidationFailedError(f"Unknown provider mode '{req.provider_mode}'.")
    if req.target_users > settings.load_test_max_users:
        raise ValidationFailedError(
            f"Target users {req.target_users} exceeds the safety cap of {settings.load_test_max_users}.")
    if req.duration_seconds > settings.load_test_max_duration_seconds:
        raise ValidationFailedError(
            f"Duration {req.duration_seconds}s exceeds the ceiling of "
            f"{settings.load_test_max_duration_seconds}s.")
    if req.ramp_seconds > req.duration_seconds + settings.load_test_max_duration_seconds:
        raise ValidationFailedError("Ramp is longer than allowed.")

    # Real-provider traffic spends money => require explicit confirmation.
    if req.provider_mode in REAL_PROVIDER_MODES and not req.confirm_real_provider:
        raise LoadTestConfirmationError()

    # Single-run guard: only one heavy test at a time.
    with _LOCK:
        if active_job(db) is not None or _RUNNING:
            raise LoadTestConflictError()

        job = LoadTestJob(
            created_by=created_by, environment="local", test_type=req.test_type,
            provider_mode=req.provider_mode, target_users=req.target_users,
            ramp_seconds=req.ramp_seconds, duration_seconds=req.duration_seconds,
            status=STATUS_STARTING,
        )
        db.add(job)
        db.flush()

        creds = provision_test_users(db, req.target_users)

        # Simulated mode => turn on the runtime mock override so this backend
        # uses test doubles for the duration (no provider spend). Real modes =>
        # ensure the override is cleared so genuine provider calls happen.
        if req.provider_mode == PROVIDER_SIMULATED:
            runtime_config_service.set_mock_ai(db, enabled=True, admin_email=created_by)
        else:
            runtime_config_service.clear_mock_ai(db)

        db.commit()

        creds_file = os.path.join(METRICS_DIR, f"{job.id}.creds.json")
        os.makedirs(METRICS_DIR, exist_ok=True)
        with open(creds_file, "w") as f:
            json.dump(creds, f)

        try:
            popen = _launch_worker(job, creds_file)
        except Exception as exc:
            job.status = STATUS_FAILED
            job.error_message = f"Failed to launch load worker: {exc}"
            job.ended_at = _now()
            if req.provider_mode == PROVIDER_SIMULATED:
                runtime_config_service.clear_mock_ai(db)
            db.commit()
            raise

        job.status = STATUS_RUNNING
        job.worker_identifier = f"pid:{popen.pid}"
        job.started_at = _now()
        db.commit()

        _RUNNING[job.id] = {"popen": popen, "creds_file": creds_file,
                            "provider_mode": job.provider_mode, "cancelled": False}

    monitor = threading.Thread(target=_monitor, args=(job.id,), name=f"loadtest-{job.id}", daemon=True)
    monitor.start()
    logger.info("load_test_started job=%s type=%s provider=%s users=%s",
                job.id, job.test_type, job.provider_mode, job.target_users)
    return job


# ------------------------------------------------------------------- monitor
def _monitor(job_id: str) -> None:
    entry = _RUNNING.get(job_id)
    if entry is None:
        return
    popen = entry["popen"]
    try:
        popen.wait()
    except Exception:
        pass
    _finalize(job_id)


def _finalize(job_id: str) -> None:
    entry = _RUNNING.pop(job_id, None)
    cancelled = bool(entry and entry.get("cancelled"))
    creds_file = entry.get("creds_file") if entry else None
    factory = get_session_factory()
    db = factory()
    try:
        job = db.get(LoadTestJob, job_id)
        if job is None:
            return
        snapshot = _read_snapshot(job_id)
        overall = None
        series = []
        worker_status = None
        worker_error = None
        if snapshot:
            worker_status = snapshot.get("status")
            worker_error = snapshot.get("error")
            final = snapshot.get("final") or {}
            overall = final.get("overall") or snapshot.get("overall")
            series = snapshot.get("series") or []

        analysis = load_capacity_analysis.analyze(
            target_users=job.target_users, duration_seconds=job.duration_seconds,
            ramp_seconds=job.ramp_seconds, overall=overall, series=series,
        )
        results = {
            "overall": overall,
            "series": series,
            "capacity": analysis,
            "workerStatus": worker_status,
        }
        job.results = json.dumps(results)
        job.ended_at = _now()
        if cancelled:
            job.status = STATUS_CANCELLED
        elif worker_error or worker_status == STATUS_FAILED:
            job.status = STATUS_FAILED
            job.error_message = worker_error or "Load worker reported failure."
        elif overall is None:
            job.status = STATUS_FAILED
            job.error_message = "No measurements were produced by the load worker."
        else:
            job.status = STATUS_COMPLETED

        # Always clear the simulated-AI runtime override after the run.
        if job.provider_mode == PROVIDER_SIMULATED:
            runtime_config_service.clear_mock_ai(db)
        db.commit()
        logger.info("load_test_finalized job=%s status=%s", job_id, job.status)
    finally:
        db.close()
        if creds_file and os.path.exists(creds_file):
            try:
                os.unlink(creds_file)
            except OSError:
                pass


def _read_snapshot(job_id: str) -> dict | None:
    path = _metrics_path(job_id)
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ----------------------------------------------------------------------- read
def get_job(db: Session, job_id: str) -> LoadTestJob:
    job = db.get(LoadTestJob, job_id)
    if job is None:
        raise LoadTestNotFoundError(job_id)
    return job


def recent_jobs(db: Session, limit: int = 20) -> list[LoadTestJob]:
    return list(
        db.execute(
            select(LoadTestJob).order_by(LoadTestJob.created_at.desc()).limit(limit)
        ).scalars()
    )


def stop_job(db: Session, job_id: str) -> LoadTestJob:
    job = get_job(db, job_id)
    if job.status not in ACTIVE_STATUSES:
        return job  # already finished; idempotent
    job.status = STATUS_STOPPING
    db.commit()
    entry = _RUNNING.get(job_id)
    if entry is not None:
        entry["cancelled"] = True
        popen = entry["popen"]
        try:
            popen.send_signal(signal.SIGTERM)
        except Exception:
            pass
    else:
        # No live process (e.g. after a restart): finalize as cancelled now.
        job.status = STATUS_CANCELLED
        job.ended_at = _now()
        if job.provider_mode == PROVIDER_SIMULATED:
            runtime_config_service.clear_mock_ai(db)
        db.commit()
    return job


# --------------------------------------------------------------- live metrics
def _live_telemetry(db: Session) -> dict:
    """REAL provider activity + infrastructure health from the backend's own
    telemetry. Never fabricated; blocks report availability explicitly."""
    from app.services import traffic_service

    return {
        "providers": {
            "openai": traffic_service.provider_block(_openai_pm()),
            "elevenlabs": traffic_service.provider_block(_elevenlabs_pm()),
        },
        "infrastructure": {
            "server": traffic_service.server_health(),
            "dbPool": traffic_service.db_pool_stats(),
            "assessmentQueue": traffic_service.assessment_block(db),
            "httpInFlight": _http_in_flight(),
        },
    }


def _openai_pm():
    from app.core.telemetry import get_telemetry
    return get_telemetry().openai


def _elevenlabs_pm():
    from app.core.telemetry import get_telemetry
    return get_telemetry().elevenlabs


def _http_in_flight() -> int:
    from app.core.telemetry import get_telemetry
    return get_telemetry().http_in_flight.value


def metrics(db: Session, job_id: str) -> dict:
    job = get_job(db, job_id)
    is_active = job.status in ACTIVE_STATUSES

    if is_active:
        snapshot = _read_snapshot(job_id)
        overall = None
        series = []
        elapsed = None
        if snapshot:
            overall = snapshot.get("overall")
            series = snapshot.get("series") or []
            elapsed = snapshot.get("elapsedSeconds")
        # Live capacity is provisional while the test runs; recompute each poll.
        analysis = load_capacity_analysis.analyze(
            target_users=job.target_users, duration_seconds=job.duration_seconds,
            ramp_seconds=job.ramp_seconds, overall=overall, series=series,
        )
        return {
            "job": to_out(job),
            "live": True,
            "elapsedSeconds": elapsed,
            "overall": overall,
            "series": series,
            "capacity": analysis,
            "telemetry": _live_telemetry(db),
        }

    # Terminal: return the persisted results (real measurements from the run).
    results = json.loads(job.results) if job.results else {}
    return {
        "job": to_out(job),
        "live": False,
        "overall": results.get("overall"),
        "series": results.get("series") or [],
        "capacity": results.get("capacity"),
        "telemetry": _live_telemetry(db),
    }


# ---------------------------------------------------------------- serialization
def to_out(job: LoadTestJob) -> dict:
    return {
        "id": job.id,
        "createdBy": job.created_by,
        "environment": job.environment,
        "testType": job.test_type,
        "providerMode": job.provider_mode,
        "targetUsers": job.target_users,
        "rampSeconds": job.ramp_seconds,
        "durationSeconds": job.duration_seconds,
        "status": job.status,
        "workerIdentifier": job.worker_identifier,
        "errorMessage": job.error_message,
        "createdAt": _iso(job.created_at),
        "startedAt": _iso(job.started_at),
        "endedAt": _iso(job.ended_at),
    }
