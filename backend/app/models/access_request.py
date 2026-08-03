import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base

ACCESS_PENDING = "PENDING"
ACCESS_APPROVED = "APPROVED"
ACCESS_REJECTED = "REJECTED"


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AccessRequest(Base):
    """A request to be granted access to register. One row per (normalized) email
    - the unique index makes duplicate active requests impossible, so a re-submit
    updates the single row rather than creating a second."""

    __tablename__ = "access_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ACCESS_PENDING)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewer_note: Mapped[str | None] = mapped_column(Text, nullable=True)
