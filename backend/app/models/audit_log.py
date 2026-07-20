import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog(Base):
    """Append-only record of administrative actions. The admin's email is
    denormalized so the trail survives even if the admin account is later
    removed. Rows are never updated after creation."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    admin_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    admin_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    action_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    record_type: Mapped[str] = mapped_column(String(40), nullable=False)
    record_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, index=True
    )
