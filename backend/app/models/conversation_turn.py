import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConversationTurn(Base):
    __tablename__ = "conversation_turns"
    __table_args__ = (
        UniqueConstraint("session_id", "turn_index", name="uq_turn_per_session"),
        # Idempotency: one saved turn per client-generated id per session.
        Index("uq_turn_client_id", "session_id", "client_turn_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("interview_sessions.id"), nullable=False, index=True
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # student | patient
    client_turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # typed | speech | openai
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Generation metadata (patient turns only; null for student turns).
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    facts_used: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of fact ids
    response_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    validation_status: Mapped[str | None] = mapped_column(String(20), nullable=True)  # valid
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    session = relationship("InterviewSession", back_populates="turns")
