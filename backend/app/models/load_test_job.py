import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Lifecycle states. Only one non-terminal heavy job is allowed to exist at a
# time (single-run guard enforced in the service layer).
STATUS_PENDING = "PENDING"
STATUS_STARTING = "STARTING"
STATUS_RUNNING = "RUNNING"
STATUS_STOPPING = "STOPPING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"

TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED)
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_STARTING, STATUS_RUNNING, STATUS_STOPPING)

# Provider modes (see load_test_service).
PROVIDER_SIMULATED = "SIMULATED_AI"          # test doubles, no provider spend
PROVIDER_REAL_OPENAI = "REAL_OPENAI"         # real OpenAI only
PROVIDER_REAL_OPENAI_TTS = "REAL_OPENAI_TTS"  # real OpenAI + ElevenLabs
REAL_PROVIDER_MODES = (PROVIDER_REAL_OPENAI, PROVIDER_REAL_OPENAI_TTS)


class LoadTestJob(Base):
    """Metadata + final results for one load/capacity test run.

    A row is created when a super-admin starts a test and updated as the
    separate load-generator process reports progress. Only completed-run
    summary metadata is persisted here (never massive raw time-series); the
    `results` JSON holds the computed summary + capacity analysis produced by
    the controller from REAL measured samples. Nothing in this table is ever
    fabricated to fill the UI."""

    __tablename__ = "load_test_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False, default="", index=True)
    environment: Mapped[str] = mapped_column(String(20), nullable=False, default="local")
    test_type: Mapped[str] = mapped_column(String(40), nullable=False, default="smoke")
    provider_mode: Mapped[str] = mapped_column(String(32), nullable=False, default=PROVIDER_SIMULATED)

    target_users: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ramp_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_PENDING, index=True)
    worker_identifier: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Computed summary + capacity analysis (JSON, from real samples only).
    results: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
