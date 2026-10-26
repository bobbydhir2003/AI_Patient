"""AI iEXCEL Course 1 integration (LTI 1.3) tables. All additive.

- platform_identities: (issuer, platform subject) -> AI SP user. The subject is
  the platform's opaque, application-scoped identifier; accounts are never
  matched by email, name, or student number.
- lti_login_states: OIDC login state + nonce (single use, short-lived), shared
  by every worker through the database.
- lti_login_codes: one-time codes that hand a launch to the AI SP web app
  (stored hashed, 60 s, single use).
- course_contexts / context_case_assignments / context_members: the platform
  course offering (LTI context), which cases it uses, and who launched into it.
- platform_event_outbox: durable progress events for the platform Events API.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PlatformIdentity(Base):
    __tablename__ = "platform_identities"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_platform_identities_issuer_subject"),
        UniqueConstraint("issuer", "user_id", name="uq_platform_identities_issuer_user"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    last_launch_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class LtiLoginState(Base):
    __tablename__ = "lti_login_states"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    nonce: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LtiLoginCode(Base):
    __tablename__ = "lti_login_codes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False)
    context_id: Mapped[str] = mapped_column(String(64), ForeignKey("course_contexts.context_id"), nullable=False)
    launch_role: Mapped[str] = mapped_column(String(20), nullable=False)  # learner | instructor
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CourseContext(Base):
    __tablename__ = "course_contexts"

    context_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # platform offering id
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    deployment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class ContextCaseAssignment(Base):
    __tablename__ = "context_case_assignments"
    __table_args__ = (UniqueConstraint("context_id", "case_id", name="uq_context_case_assignments"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    context_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("course_contexts.context_id"), nullable=False, index=True
    )
    case_id: Mapped[str] = mapped_column(String(50), nullable=False)
    assigned_by: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class ContextMember(Base):
    __tablename__ = "context_members"
    __table_args__ = (UniqueConstraint("context_id", "user_id", "role", name="uq_context_members"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    context_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("course_contexts.context_id"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # learner | instructor
    first_launch_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    last_launch_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class PlatformEventOutbox(Base):
    __tablename__ = "platform_event_outbox"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)  # stable, deterministic
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    context_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # JSON event (no transcript content)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    last_error: Mapped[str | None] = mapped_column(String(120), nullable=True)  # error code only
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
