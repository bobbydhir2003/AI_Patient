import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import USER_ROLE_STUDENT
from app.database.base import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """Authentication account. A 'student' user is linked 1:1 to a Student
    profile (which owns the interview sessions); an 'admin' user has no
    Student profile. Passwords are stored only as bcrypt hashes."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    student_number: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=USER_ROLE_STUDENT)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Nullable so admins (no interview data) and yet-unlinked students are valid.
    student_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("students.id"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    student = relationship("Student", back_populates="user")
