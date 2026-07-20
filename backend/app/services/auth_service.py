"""Registration, login and current-user logic.

Security invariants:
- Passwords are only ever stored as bcrypt hashes.
- Login returns a single generic error for unknown email OR wrong password,
  so it never leaks which accounts exist.
- Student accounts are safely connected to existing Student profiles by
  student_number when possible, otherwise a new profile is created.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import USER_ROLE_STUDENT
from app.core.exceptions import (
    EmailAlreadyRegisteredError,
    InactiveAccountError,
    InvalidCredentialsError,
)
from app.core.logging import get_logger
from app.core.security import create_access_token, hash_password, verify_password
from app.models import Student, User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import RegisterRequest, TokenResponse, UserOut

logger = get_logger(__name__)


def _link_or_create_student(db: Session, payload: RegisterRequest) -> Student:
    """Connect the new student user to an existing Student profile (matched by
    student_number) so their prior interview data is preserved. Falls back to
    creating a fresh profile."""
    student: Student | None = None
    number = payload.student_number.strip()
    if number:
        stmt = select(Student).where(Student.student_number == number)
        for candidate in db.execute(stmt).scalars().all():
            # Prefer a profile that has no login account yet.
            if candidate.user is None:
                student = candidate
                break
    if student is None:
        student = Student(name=payload.full_name.strip(), student_number=number)
        db.add(student)
        db.flush()
    # Keep the profile's contact fields in step with the account.
    if not student.email:
        student.email = payload.email
    if not student.name:
        student.name = payload.full_name.strip()
    db.flush()
    return student


def register(db: Session, payload: RegisterRequest) -> TokenResponse:
    settings = get_settings()
    if not settings.allow_student_self_registration:
        raise InvalidCredentialsError()  # registration closed; stay generic

    repo = UserRepository(db)
    if repo.get_by_email(payload.email) is not None:
        raise EmailAlreadyRegisteredError()

    student = _link_or_create_student(db, payload)
    user = repo.create(
        email=payload.email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        student_number=payload.student_number,
        role=USER_ROLE_STUDENT,
        student_id=student.id,
        is_active=True,
    )
    db.commit()
    logger.info("user_registered user_id=%s role=%s", user.id, user.role)
    return _issue_token(db, user)


def login(db: Session, email: str, password: str) -> TokenResponse:
    repo = UserRepository(db)
    user = repo.get_by_email(email)
    # Always run verify to keep timing uniform whether or not the email exists.
    placeholder = "$2b$12$0000000000000000000000000000000000000000000000000000"
    if user is None:
        verify_password(password, placeholder)
        raise InvalidCredentialsError()
    if not verify_password(password, user.password_hash):
        raise InvalidCredentialsError()
    if not user.is_active:
        raise InactiveAccountError()

    repo.touch_last_login(user)
    db.commit()
    logger.info("user_login user_id=%s role=%s", user.id, user.role)
    return _issue_token(db, user)


def _issue_token(db: Session, user: User) -> TokenResponse:
    settings = get_settings()
    token = create_access_token(subject=user.id, role=user.role)
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserOut.model_validate(user),
    )


def to_user_out(user: User) -> UserOut:
    return UserOut.model_validate(user)
