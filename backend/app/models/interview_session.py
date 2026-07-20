import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import SESSION_STATUS_ACTIVE
from app.database.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InterviewSession(Base):
    __tablename__ = "interview_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    student_id: Mapped[str] = mapped_column(String(32), ForeignKey("students.id"), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    case_category: Mapped[str] = mapped_column(String(20), nullable=False, default="standard")
    assessment_capabilities: Mapped[str] = mapped_column(
        Text, nullable=False, default='["standard_interview"]'
    )  # JSON list
    protected_reference_version: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=SESSION_STATUS_ACTIVE)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    disclosed_fact_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON list
    active_topic: Mapped[str | None] = mapped_column(String(40), nullable=True)  # follow-up context
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    student = relationship("Student", back_populates="sessions")
    turns = relationship(
        "ConversationTurn",
        back_populates="session",
        order_by="ConversationTurn.turn_index",
        cascade="all, delete-orphan",
    )
