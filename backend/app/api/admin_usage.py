"""AI Usage & Cost admin API.

ADMIN ONLY (require_admin on every route). Serves the real-time AI Usage & Cost
dashboard from recorded ai_usage_events — the backend does the aggregation, the
frontend only renders. Costs are ESTIMATED usage costs, never provider invoices.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.pricing import pricing_snapshot
from app.database.connection import get_db
from app.dependencies.auth import require_admin
from app.services import runtime_config_service as rc
from app.services import usage_service

router = APIRouter(
    prefix="/admin/usage",
    tags=["admin-usage"],
    dependencies=[Depends(require_admin)],
)

_RANGES = ("live", "5m", "15m", "1h", "6h", "24h", "7d", "30d", "today", "custom")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@router.get("/summary")
def get_summary(
    range: str = Query(default="today"),
    start: str | None = None,
    end: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    rng = range if range in _RANGES else "today"
    data = usage_service.summary(db, rng, _parse_dt(start), _parse_dt(end))
    data["pricing"] = pricing_snapshot()
    data["providers"] = _provider_health(db)
    return data


@router.get("/timeseries")
def get_timeseries(
    range: str = Query(default="24h"),
    start: str | None = None,
    end: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    rng = range if range in _RANGES else "24h"
    return usage_service.timeseries(db, rng, _parse_dt(start), _parse_dt(end))


@router.get("/sessions")
def get_sessions(
    range: str = Query(default="today"),
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(default=25, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    rng = range if range in _RANGES else "today"
    return usage_service.sessions(db, rng, _parse_dt(start), _parse_dt(end), limit=limit)


@router.get("/sessions/{session_id}")
def get_session_detail(session_id: str, db: Session = Depends(get_db)) -> dict:
    detail = usage_service.session_detail(db, session_id)
    if detail is None:
        return {"found": False, "session_id": session_id}
    return {"found": True, **detail}


def _provider_health(db: Session) -> dict:
    """REAL provider status only: credential-configured state + last recorded
    usage event time. No invented latency/RPM numbers."""
    configured = {c.get("service"): bool(c.get("configured")) for c in rc.credential_status(db)}
    from sqlalchemy import func, select

    from app.models import AiUsageEvent

    def _last(provider: str):
        v = db.execute(
            select(func.max(AiUsageEvent.created_at)).where(AiUsageEvent.provider == provider)
        ).scalar_one()
        if v is not None and v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.isoformat() if v is not None else None

    return {
        "openai": {"configured": configured.get("openai", False), "last_event_at": _last("openai")},
        "elevenlabs": {"configured": configured.get("elevenlabs", False), "last_event_at": _last("elevenlabs")},
    }
