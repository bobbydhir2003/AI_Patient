"""Load & Capacity Testing admin API (Priority J).

ADMIN ONLY. Every route requires require_admin, so a student receives 403 while
every administrator has full access. The load generator runs in a SEPARATE
process; these endpoints only create/read/stop jobs and return REAL measured
telemetry. Real (paid) provider modes require an explicit confirmation flag
before they start.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.connection import get_db
from app.dependencies.auth import require_admin
from app.models.user import User
from app.schemas.load_test_schema import LoadTestCreateRequest
from app.services import load_test_service

router = APIRouter(
    prefix="/admin/system/load-tests",
    tags=["admin-load-tests"],
    dependencies=[Depends(require_admin)],
)


@router.get("/config")
def config() -> dict:
    """Static capabilities/limits for the UI (caps, modes, test types)."""
    settings = get_settings()
    from app.schemas.load_test_schema import PROVIDER_MODES, TEST_TYPES

    return {
        "enabled": settings.load_test_enabled,
        "environment": "local",
        "awsReady": False,  # architecture-ready only; no AWS resources are created
        "maxUsers": settings.load_test_max_users,
        "maxDurationSeconds": settings.load_test_max_duration_seconds,
        "targetBaseUrl": settings.load_test_target_base_url,
        "testTypes": list(TEST_TYPES),
        "providerModes": list(PROVIDER_MODES),
    }


@router.post("")
def create(
    payload: LoadTestCreateRequest,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    job = load_test_service.create_job(db, req=payload, created_by=current_user.email)
    return load_test_service.to_out(job)


@router.get("/recent")
def recent(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    jobs = load_test_service.recent_jobs(db, limit=limit)
    return {"jobs": [load_test_service.to_out(j) for j in jobs]}


@router.get("/active")
def active(db: Session = Depends(get_db)) -> dict:
    job = load_test_service.active_job(db)
    return {"job": load_test_service.to_out(job) if job else None}


@router.get("/{job_id}")
def get_one(job_id: str, db: Session = Depends(get_db)) -> dict:
    return load_test_service.to_out(load_test_service.get_job(db, job_id))


@router.get("/{job_id}/metrics")
def get_metrics(job_id: str, db: Session = Depends(get_db)) -> dict:
    return load_test_service.metrics(db, job_id)


@router.post("/{job_id}/stop")
def stop(job_id: str, db: Session = Depends(get_db)) -> dict:
    return load_test_service.to_out(load_test_service.stop_job(db, job_id))
