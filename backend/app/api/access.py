"""Email access-request endpoints (Version 1).

Public:
  POST /api/access/request         -> submit an email (rate-limited, no enumeration)
Admin (require_admin):
  GET  /api/admin/access-requests
  POST /api/admin/access-requests/{id}/approve
  POST /api/admin/access-requests/{id}/reject

There is deliberately NO public "status by email" endpoint - the public route
only ever returns the result for the email that was submitted.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.rate_limit import rate_limit
from app.database.connection import get_db
from app.dependencies.auth import require_admin
from app.models import User
from app.schemas.access_schema import (
    AccessRequestIn,
    AccessRequestOut,
    AccessRequestResult,
    AccessReviewIn,
)
from app.services import access_request_service

_PUBLIC_MESSAGES = {
    "PENDING": "Your request has been submitted and is pending admin approval.",
    "ALREADY_PENDING": "Your request is already pending review.",
    "ALREADY_APPROVED": "You are already approved. Continue to sign in.",
}

# --- public ---
public_router = APIRouter(prefix="/access", tags=["access"])
_access_rate_limit = rate_limit("access", lambda s: s.login_rate_limit)


@public_router.post("/request", response_model=AccessRequestResult, dependencies=[Depends(_access_rate_limit)])
def request_access(payload: AccessRequestIn, db: Session = Depends(get_db)) -> AccessRequestResult:
    result = access_request_service.submit_request(db, payload.email)
    return AccessRequestResult(result=result, message=_PUBLIC_MESSAGES.get(result, "Request received."))


# --- admin ---
admin_router = APIRouter(
    prefix="/admin/access-requests",
    tags=["admin-access"],
    dependencies=[Depends(require_admin)],
)


@admin_router.get("", response_model=list[AccessRequestOut])
def list_access_requests(
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[AccessRequestOut]:
    return access_request_service.list_requests(db, status)


@admin_router.post("/{request_id}/approve", response_model=AccessRequestOut)
def approve_request(
    request_id: str,
    payload: AccessReviewIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AccessRequestOut:
    return access_request_service.approve(db, request_id, admin.email, payload.note)


@admin_router.post("/{request_id}/reject", response_model=AccessRequestOut)
def reject_request(
    request_id: str,
    payload: AccessReviewIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AccessRequestOut:
    return access_request_service.reject(db, request_id, admin.email, payload.note)
