"""AI provider usage event — the source of truth for the AI Usage & Cost dashboard.

ONE row per real provider request that returned usage:
  - OpenAI: one row per completed model request (provider-reported input/output
    tokens). Recorded once, at turn commit — never per streamed chunk.
  - ElevenLabs: one row per real TTS synthesis request (characters generated).
    Recorded only on a cache miss (a cache hit is not a new provider request).

Each row stores the unit prices + pricing_version used at record time so the
cost of a past interview is reproducible even after prices change.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AiUsageEvent(Base):
    __tablename__ = "ai_usage_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)

    # Attribution (session may be null for non-interview usage; still counted in
    # global totals). student_id/case_id are denormalized for fast grouping.
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    student_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    case_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    provider: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # openai|elevenlabs
    model: Mapped[str] = mapped_column(String(60), nullable=False, default="")

    # What produced this call, for distinguishing spend on the dashboard, e.g.
    # "interview", "assessment_generate", "assessment_verify",
    # "assessment_correction". Nullable/additive; legacy rows stay null.
    purpose: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    # OpenAI usage units (provider-reported). Zero for ElevenLabs rows.
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ElevenLabs usage units. Zero for OpenAI rows.
    characters_generated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    audio_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Historical pricing snapshot (per single unit) so cost is reproducible.
    input_unit_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    output_unit_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    provider_unit_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    pricing_version: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)

    provider_request_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, index=True
    )

    __table_args__ = (
        # Hot path for the dashboard: recent events by provider / by session.
        Index("ix_ai_usage_provider_created", "provider", "created_at"),
        Index("ix_ai_usage_session_provider", "session_id", "provider"),
    )
