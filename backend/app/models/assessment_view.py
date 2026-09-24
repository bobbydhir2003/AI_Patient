import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


# Where an assessment VISIT came from. The client declares the source ONLY when a
# visit is first created (validated against VISIT_CLIENT_SOURCES); it is never
# changed by later heartbeats. LEGACY marks the one synthetic visit backfilled per
# pre-visit-tracking summary row (migration 0025); UNKNOWN marks a real visit from
# a version-skewed client that sent no source.
VISIT_SOURCE_INITIAL = "initial_assessment"   # opened right after the interview
VISIT_SOURCE_DASHBOARD = "student_dashboard"  # reopened from the student dashboard
VISIT_SOURCE_LEGACY = "legacy"
VISIT_SOURCE_UNKNOWN = "unknown"
VISIT_CLIENT_SOURCES = (VISIT_SOURCE_INITIAL, VISIT_SOURCE_DASHBOARD)


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
    # Latest successful active-view heartbeat across all visits (admin "Last
    # viewed"). Nullable for backward compatibility with pre-0025 summary rows.
    last_viewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class AssessmentViewVisit(Base):
    """One row per REAL assessment page visit (a page mount identified by a
    client-generated ``visit_id``). Source of truth for visit COUNT and per-visit
    active time; ``assessment_view_sessions`` is the fast denormalized summary
    over these. A visit is created on its first heartbeat and never merged with
    another - so views reflect actual navigations, never elapsed-gap guessing.
    Active time is still credited SERVER-SIDE from ``last_heartbeat_at`` (clamped),
    and a new visit's first ping credits 0, so away/between-visit gaps never count.
    """

    __tablename__ = "assessment_view_visits"
    __table_args__ = (
        # Idempotency: one visit per (session, client visit_id). A retried/duplicate
        # first ping upserts the same row instead of creating a second visit.
        UniqueConstraint(
            "interview_session_id", "visit_id", name="uq_assessment_view_visit"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # Opaque client-generated id (UUID) - stable for one page visit. Never trusted
    # for anything but grouping heartbeats of the same visit.
    visit_id: Mapped[str] = mapped_column(String(64), nullable=False)
    interview_session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assessment_run_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("assessment_runs.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    student_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(50), nullable=False)
    # Set once at visit creation (see VISIT_SOURCE_*); immutable afterwards.
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default=VISIT_SOURCE_UNKNOWN,
        server_default=VISIT_SOURCE_LEGACY,
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    active_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
