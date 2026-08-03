"""Registration, login and current-user logic.

Security invariants:
- Passwords are only ever stored as bcrypt hashes.
- Login returns a single generic error for unknown email OR wrong password,
  so it never leaks which accounts exist.
- Student accounts are safely connected to existing Student profiles by
  student_number when possible, otherwise a new profile is created.
"""
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_DISABLED,
    ACCOUNT_STATUS_PENDING,
    ACCOUNT_STATUS_REJECTED,
    USER_ROLE_STUDENT,
)
from app.core.exceptions import (
    AccountDisabledError,
    AccountPendingError,
    AccountRejectedError,
    EmailAlreadyRegisteredError,
    InactiveAccountError,
    InvalidCredentialsError,
)
from app.core import rate_limit as login_throttle
from app.core.logging import get_logger
from app.core.security import create_access_token, hash_password, verify_password
from app.models import Student, User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import RegisterRequest, RegisterResult, TokenResponse, UserOut

logger = get_logger(__name__)


def _link_or_create_student(db: Session, payload: RegisterRequest) -> Student:
    """Create a FRESH Student profile for the new account.

    A8 - student-number account claiming fix. Self-registration must NEVER
    attach the new account to a pre-existing Student profile purely because the
    user typed a matching free-text student_number: doing so would let anyone
    claim another student's prior interview history and assessments just by
    guessing/knowing their number. Every self-registered account therefore gets
    its own new profile. Linking an account to a historical roster profile is a
    deliberate ADMIN action (identity is verified out of band), not something a
    self-service registrant can trigger.
    """
    number = payload.student_number.strip()
    student = Student(
        name=payload.full_name.strip(),
        student_number=number,
        email=payload.email,
    )
    db.add(student)
    db.flush()
    return student


def register(db: Session, payload: RegisterRequest) -> RegisterResult:
    """D2: create a STUDENT account in PENDING status. Registration IS the access
    request - an administrator must approve the account before it can sign in. The
    new account is NOT auto-logged-in and NOT granted simulator access."""
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
        is_active=False,  # kept in lock-step with account_status
    )
    user.account_status = ACCOUNT_STATUS_PENDING
    db.commit()
    logger.info("user_registered user_id=%s role=%s status=PENDING", user.id, user.role)
    return RegisterResult(
        status="pending",
        message="Account created successfully. Your account is waiting for administrator approval.",
    )


def login(db: Session, email: str, password: str, client_ip: str = "unknown") -> TokenResponse:
    # A9: brute-force throttle. Refuse early (generic 429) when this IP/email is
    # temporarily locked out, without revealing whether the account exists.
    login_throttle.check_login_allowed(client_ip, email)

    repo = UserRepository(db)
    user = repo.get_by_email(email)
    # Always run verify to keep timing uniform whether or not the email exists.
    placeholder = "$2b$12$0000000000000000000000000000000000000000000000000000"
    if user is None:
        verify_password(password, placeholder)
        login_throttle.record_login_failure(client_ip, email)
        raise InvalidCredentialsError()
    if not verify_password(password, user.password_hash):
        login_throttle.record_login_failure(client_ip, email)
        raise InvalidCredentialsError()

    # D4: account-status gate (distinct, non-network messages). Credentials were
    # correct, so a successful attempt is recorded (clears the throttle) before
    # we report the status - a pending/disabled user is not a failed password.
    login_throttle.record_login_success(client_ip, email)
    status = getattr(user, "account_status", ACCOUNT_STATUS_ACTIVE)
    if status == ACCOUNT_STATUS_PENDING:
        raise AccountPendingError()
    if status == ACCOUNT_STATUS_REJECTED:
        raise AccountRejectedError()
    if status == ACCOUNT_STATUS_DISABLED:
        raise AccountDisabledError()
    if status != ACCOUNT_STATUS_ACTIVE or not user.is_active:
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
