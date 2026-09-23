import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AssessmentViewSession(Base):
    """Aggregate "active assessment viewing time" for ONE interview session.

    Silent, admin-only telemetry: how long the owning student actively viewed
    their Assessment Results page. There is exactly ONE row per interview session
    (the authoritative aggregation key, ``UNIQUE`` below); reopens/refreshes/tabs
    all upsert into it. Time is credited SERVER-SIDE from ``last_heartbeat_at``
    (the client never sends a duration), so accumulated ``active_seconds`` can
    never exceed real elapsed visible time. See assessment_view_service.
    """

    __tablename__ = "assessment_view_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # Authoritative key: one row per interview session (unique). ON DELETE CASCADE
    # so a purged session/student tree never leaves an orphan timing row (the
    # deletion services also delete these explicitly, belt-and-suspenders).
    interview_session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    # Latest assessment run viewed (denormalized; retries create new runs but the
    # session key is stable). SET NULL if that run is deleted while the session
    # survives - the viewing time belongs to the session, not the run.
    assessment_run_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("assessment_runs.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Denormalized for fast admin lookup/grouping (mirrors the session).
    student_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(50), nullable=False)

    active_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    first_viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
