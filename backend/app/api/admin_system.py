"""Technical System Dashboard endpoints (admin-only).

Every route is protected by `require_admin`. Values come from real runtime
checks or real recorded activity; no secrets are ever returned.
"""
import time

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.database.connection import get_db
from app.dependencies.auth import require_admin
from app.schemas.system_schema import (
    SystemLiveOut,
    SystemOverviewOut,
)
from app.services import system_service

logger = get_logger(__name__)

router = APIRouter(
    prefix="/admin/system",
    tags=["admin-system"],
    dependencies=[Depends(require_admin)],
)


@router.get("/overview", response_model=SystemOverviewOut)
def system_overview(db: Session = Depends(get_db)) -> SystemOverviewOut:
    started = time.perf_counter()
    return system_service.build_overview(db, started)


@router.get("/live", response_model=SystemLiveOut)
def system_live(db: Session = Depends(get_db)) -> SystemLiveOut:
    """Lean live snapshot for fast polling (backend/db/redis health, observed
    worker fleet, global concurrency, realtime infra checks, alerts). Every
    value is a real runtime measurement; nothing is fabricated to fill the UI."""
    started = time.perf_counter()
    return system_service.build_live(db, started)
