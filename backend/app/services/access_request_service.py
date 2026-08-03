"""Email access-request flow (Version 1 - no OTP, no phone).

A prospective user submits their email; an admin approves or rejects it. When the
registration gate (`REQUIRE_ACCESS_APPROVAL`) is on, only APPROVED emails may
register. Emails are normalized before storage/comparison and there is exactly
one row per email (unique index), so re-submitting never creates duplicates.

Enumeration safety: the PUBLIC endpoint only ever returns the result for the
submitted email and there is NO public "status by email" lookup.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.access_request import (
    ACCESS_APPROVED,
    ACCESS_PENDING,
    ACCESS_REJECTED,
    AccessRequest,
)
from app.schemas.auth import _normalize_email  # reuse the same normalization

logger = get_logger(__name__)


def get_by_email(db: Session, email: str) -> AccessRequest | None:
    email = _normalize_email(email)
    return db.execute(select(AccessRequest).where(AccessRequest.email == email)).scalar_one_or_none()


def is_email_approved(db: Session, email: str) -> bool:
    ar = get_by_email(db, email)
    return ar is not None and ar.status == ACCESS_APPROVED


def submit_request(db: Session, email: str) -> str:
    """Public submission. Returns one of: PENDING | ALREADY_PENDING | ALREADY_APPROVED.
    (A previously REJECTED email is cleanly re-opened to PENDING - single row, no
    duplicate.) Never reveals anything beyond the submitted email's own result."""
    email = _normalize_email(email)
    existing = get_by_email(db, email)
    if existing is not None:
        if existing.status == ACCESS_PENDING:
            return "ALREADY_PENDING"
        if existing.status == ACCESS_APPROVED:
            return "ALREADY_APPROVED"
        if existing.status == ACCESS_REJECTED:
            # Re-open the SAME row (no duplicate); admin can review again.
            existing.status = ACCESS_PENDING
            existing.requested_at = datetime.now(timezone.utc)
            existing.reviewed_by = None
            existing.reviewed_at = None
            existing.reviewer_note = None
            db.commit()
            logger.info("access_request_reopened email=%s", email)
            return "PENDING"
    ar = AccessRequest(email=email, status=ACCESS_PENDING)
    db.add(ar)
    db.commit()
    logger.info("access_request_created email=%s", email)
    return "PENDING"


# ------------------------------------------------------------------ admin ops
def list_requests(db: Session, status: str | None = None) -> list[AccessRequest]:
    stmt = select(AccessRequest).order_by(AccessRequest.requested_at.desc())
    if status:
        stmt = stmt.where(AccessRequest.status == status.upper())
    return list(db.execute(stmt).scalars().all())


def _review(db: Session, request_id: str, new_status: str, reviewer: str, note: str | None) -> AccessRequest:
    from app.core.exceptions import AccessRequestNotFoundError

    ar = db.get(AccessRequest, request_id)
    if ar is None:
        raise AccessRequestNotFoundError(request_id)
    ar.status = new_status
    ar.reviewed_by = reviewer
    ar.reviewed_at = datetime.now(timezone.utc)
    ar.reviewer_note = (note or "").strip() or None
    db.commit()
    logger.info("access_request_reviewed id=%s status=%s reviewer=%s", request_id, new_status, reviewer)
    return ar


def approve(db: Session, request_id: str, reviewer: str, note: str | None = None) -> AccessRequest:
    return _review(db, request_id, ACCESS_APPROVED, reviewer, note)


def reject(db: Session, request_id: str, reviewer: str, note: str | None = None) -> AccessRequest:
    return _review(db, request_id, ACCESS_REJECTED, reviewer, note)
