from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.redis_client import redis_health
from app.database.connection import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    database = "connected"
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        database = "unavailable"
    redis = redis_health()
    status = "ok"
    if database == "unavailable":
        status = "degraded"
    elif redis["required"] and redis["status"] != "connected":
        # Redis is required for global concurrency control in this environment
        # (see core/config.py Settings.redis_required) - surface that honestly
        # rather than reporting "ok" while concurrency limits are unsafe.
        status = "degraded"
    return {"status": status, "database": database, "redis": redis["status"]}
