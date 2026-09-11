import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base

# Survey phases (still meaningful as the two STAGES of one case-level survey).
# Kept as plain strings, matching the codebase's lightweight enum style.
SURVEY_PHASE_PRE = "pre"
SURVEY_PHASE_POST = "post"

# Per-phase REDCap sync lifecycle.
SURVEY_SYNC_PENDING = "pending"   # stage not yet submitted / not yet confirmed
SURVEY_SYNC_SYNCED = "synced"     # REDCap accepted this stage
SURVEY_SYNC_FAILED = "failed"     # REDCap import failed; safe to retry
SURVEY_SYNC_SKIPPED = "skipped"   # REDCap unconfigured (dev/CI): recorded locally, not imported

# A stage counts as "done" (do-not-resubmit) when it has reached REDCap or was
# intentionally skipped because REDCap is unconfigured.
SURVEY_SYNC_DONE = (SURVEY_SYNC_SYNCED, SURVEY_SYNC_SKIPPED)

# Case-level survey lifecycle. IN_PROGRESS until the Post-Survey stage is
# successfully received by REDCap (or skipped when unconfigured), then COMPLETED.
SURVEY_OVERALL_IN_PROGRESS = "in_progress"
SURVEY_OVERALL_COMPLETED = "completed"
# API-only sentinel returned when NO receipt exists yet (never stored in the DB).
SURVEY_OVERALL_NOT_STARTED = "not_started"


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SurveyReceipt(Base):
    """ONE case-level survey package per (student, case) - the single source of
    truth for "has this student completed the survey for this case".

    Business rule: a student may complete ONE survey package per patient case.
    Pre and Post are the two STAGES of that one package, not two separate
    submissions. The ``UNIQUE(student_id, case_id)`` constraint enforces this at
    the database level, so a brand-new interview session for the same case, a
    double-click, or two concurrent requests can never create a second receipt.

    This table exists for LINKAGE, LIFECYCLE and RELIABILITY only. It stores NO
    survey answers (no Likert values, no open-ended text, no response JSON) -
    every actual response lives in REDCap. It records only: which REDCap
    ``record_id`` (the student's NUID) the package is filed under, whether each
    stage has reached REDCap, and whether the whole package is completed.

    Lifecycle (overall_status):
        NEW (no row) -> create row, overall_status = IN_PROGRESS
        Pre reaches REDCap  -> pre_sync_status = synced/skipped; still IN_PROGRESS
        Post reaches REDCap -> post_sync_status = synced/skipped; -> COMPLETED
    A stage that FAILS REDCap stays not-done so it can be retried, and never
    advances overall_status.
    """

    __tablename__ = "survey_receipts"
    __table_args__ = (
        UniqueConstraint("student_id", "case_id", name="uq_survey_receipts_student_case"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # The case-level uniqueness key. student_id + case_id resolves the ONE
    # receipt regardless of which interview session drove a given stage.
    student_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("students.id"), nullable=False, index=True
    )
    case_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # The most recent interview session that touched this package - useful for
    # audit/debug only; NEVER part of the uniqueness key (a new session must not
    # create a new receipt). Nullable so the receipt can outlive any one session.
    latest_session_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("interview_sessions.id"), nullable=True
    )
    # REDCap record_id used for this package = the student's NUID
    # (Student.student_number), resolved server-side. Never PII (no name/email).
    redcap_record_id: Mapped[str] = mapped_column(String(100), nullable=False)

    pre_sync_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SURVEY_SYNC_PENDING
    )
    pre_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    post_sync_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SURVEY_SYNC_PENDING
    )
    post_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    overall_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SURVEY_OVERALL_IN_PROGRESS
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    student = relationship("Student")
    latest_session = relationship("InterviewSession")
