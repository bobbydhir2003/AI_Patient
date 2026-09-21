import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Student(Base):
    __tablename__ = "students"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    student_number: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Admin archive flag. Authoritative status for the student profile; kept in
    # sync with the linked User.is_active so an archived student cannot log in.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # True only for a profile that was auto-provisioned so an admin/professor can
    # test-drive the simulator. These profiles are NEVER real academic records and
    # are excluded from the admin roster, dashboard counts and analytics. A real
    # student who is later promoted to admin keeps is_practice=False (their prior
    # sessions remain real); only their newly created admin sessions are flagged.
    is_practice: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    # Global survey ownership (one survey PACKAGE per student, not per case).
    # The single case that successfully established this student's survey package
    # (its Pre stage reached REDCap or was skipped). NULL until a first Pre
    # succeeds; every other case is then gated to "already submitted / skip".
    # This is a denormalized pointer over survey_receipts (the authoritative
    # lifecycle record): it gives O(1) gating and a stable row to lock
    # (SELECT ... FOR UPDATE) when serialising the ownership claim. Kept as a case
    # slug (e.g. "carly"), matching InterviewSession.case_id.
    survey_owner_case_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Set once when the owning case's Post stage completes the package. When
    # non-NULL the global survey state is COMPLETED; when NULL but an owner exists
    # it is IN_PROGRESS.
    survey_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    sessions = relationship("InterviewSession", back_populates="student")
    # 1:1 login account for this student (null until an account is linked).
    user = relationship("User", back_populates="student", uselist=False)
