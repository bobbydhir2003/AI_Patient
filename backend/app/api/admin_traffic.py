"""Traffic Dashboard admin API (Priority B).

All routes require an admin account (require_admin) - this operational telemetry
is never exposed to students. Every value comes from real measurement; see
app/services/traffic_service.py. Responses are intentionally lightweight (rolling
in-memory counters + cheap indexed queries) so polling the dashboard does not
itself become a source of load.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.dependencies.auth import require_admin
from app.services import traffic_service

router = APIRouter(
    prefix="/admin/system/traffic",
    tags=["admin-traffic"],
    dependencies=[Depends(require_admin)],
)


@router.get("/overview")
def overview(db: Session = Depends(get_db)) -> dict:
    return traffic_service.overview(db)


@router.get("/live-sessions")
def live_sessions() -> dict:
    sessions = traffic_service.live_sessions()
    return {"count": len(sessions), "sessions": sessions}


@router.get("/history")
def history(minutes: int = Query(default=60, ge=1, le=60)) -> dict:
    return traffic_service.history(minutes)


@router.get("/providers")
def providers(db: Session = Depends(get_db)) -> dict:
    return traffic_service.providers(db)


@router.get("/capacity")
def capacity() -> dict:
    return traffic_service.capacity()


@router.get("/alerts")
def alerts(db: Session = Depends(get_db)) -> dict:
    ov = traffic_service.overview(db)
    return {"alerts": ov["alerts"]}
