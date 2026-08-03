import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base

# B2/B7: at most ONE active (queued/running) assessment per session. This partial
# unique index is the atomic guard against the read-then-create double-submit
# race - a second concurrent enqueue hits an IntegrityError and returns the
# existing run instead of spending twice. Supported by both SQLite and Postgres.
_ACTIVE_STATUSES_SQL = "status IN ('PENDING','PROCESSING','VERIFYING')"


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AssessmentRun(Base):
    __tablename__ = "assessment_runs"
    __table_args__ = (
        Index("ix_assessment_runs_status", "status"),
        Index(
            "uq_active_assessment_per_session", "session_id", unique=True,
            sqlite_where=text(_ACTIVE_STATUSES_SQL),
            postgresql_where=text(_ACTIVE_STATUSES_SQL),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("interview_sessions.id"), nullable=False, index=True
    )
    case_id: Mapped[str] = mapped_column(String(50), nullable=False)
    assessment_mode: Mapped[str] = mapped_column(String(30), nullable=False, default="standard")
    referral_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_version: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    rubric_version: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    overall_level: Mapped[str | None] = mapped_column(String(30), nullable=True)
    overall_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    focus_areas: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    verification_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    domain_results = relationship(
        "AssessmentDomainResult", back_populates="run", cascade="all, delete-orphan"
    )


class AssessmentDomainResult(Base):
    __tablename__ = "assessment_domain_results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    assessment_run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("assessment_runs.id"), nullable=False, index=True
    )
    rubric_domain: Mapped[str] = mapped_column(String(60), nullable=False)
    performance_level: Mapped[str] = mapped_column(String(30), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    narrative: Mapped[str] = mapped_column(Text, nullable=False, default="")
    strengths: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON list
    areas_for_growth: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON list

    run = relationship("AssessmentRun", back_populates="domain_results")
    evidence_items = relationship(
        "AssessmentEvidence", back_populates="domain_result", cascade="all, delete-orphan"
    )


class AssessmentEvidence(Base):
    __tablename__ = "assessment_evidence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    domain_result_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("assessment_domain_results.id"), nullable=False, index=True
    )
    turn_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("conversation_turns.id"), nullable=False
    )
    turn_label: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    evidence_type: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    student_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    patient_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    why_it_matters: Mapped[str] = mapped_column(Text, nullable=False, default="")
    suggested_alternative: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence_level: Mapped[str] = mapped_column(String(20), nullable=False, default="moderate")
    reviewer_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    domain_result = relationship("AssessmentDomainResult", back_populates="evidence_items")
