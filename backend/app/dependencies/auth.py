"""Authentication / authorization FastAPI dependencies.

`get_current_user` validates the bearer token and loads the account.
`require_admin` / `require_student` enforce role-based access control.
`require_session_access` centralizes the ownership check so a student can only
reach their own sessions while admins can reach any.
"""
import jwt
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.constants import (
    ADMIN_ROLES,
    USER_ROLE_STUDENT,
    USER_ROLE_SUPER_ADMIN,
    USER_ROLES,
)
from app.core.exceptions import (
    AssessmentNotFoundError,
    ForbiddenError,
    InactiveAccountError,
    NotAuthenticatedError,
    SessionNotFoundError,
)
from app.core.security import decode_access_token
from app.database.connection import get_db
from app.models import InterviewSession, User
from app.repositories.user_repository import UserRepository


def _extract_bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not header or not header.lower().startswith("bearer "):
        raise NotAuthenticatedError()
    token = header.split(" ", 1)[1].strip()
    if not token:
        raise NotAuthenticatedError()
    return token


def load_user_from_token(db: Session, token: str) -> User:
    """Validate a bearer token and return the active account. Shared by the
    request dependency and the streaming path (which cannot hold a DB session)."""
    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError:
        raise NotAuthenticatedError("Your session has expired. Please sign in again.")
    except jwt.PyJWTError:
        raise NotAuthenticatedError("Invalid authentication token.")

    user_id = payload.get("sub")
    if not user_id:
        raise NotAuthenticatedError("Invalid authentication token.")

    user = UserRepository(db).get(user_id)
    if user is None:
        raise NotAuthenticatedError("Account no longer exists.")
    if not user.is_active:
        raise InactiveAccountError()
    return user


def user_can_access_session(user: User, session: InterviewSession) -> bool:
    """Ownership rule (single source of truth): admins may reach any session; a
    student may reach only sessions owned by their linked Student profile."""
    if user.role in ADMIN_ROLES:
        return True
    return bool(user.student_id and session.student_id == user.student_id)


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    return load_user_from_token(db, _extract_bearer_token(request))


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    # super_admin is a strict superset of admin, so it passes admin checks too.
    if current_user.role not in ADMIN_ROLES:
        raise ForbiddenError("Administrator access is required.")
    return current_user


def require_super_admin(current_user: User = Depends(get_current_user)) -> User:
    """Highest tier: API-key replacement/removal and configuration rollback."""
    if current_user.role != USER_ROLE_SUPER_ADMIN:
        raise ForbiddenError("Super administrator access is required.")
    return current_user


def require_student(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != USER_ROLE_STUDENT:
        raise ForbiddenError("This resource is only available to student accounts.")
    return current_user


def require_simulator_access(current_user: User = Depends(get_current_user)) -> User:
    """Gate for the patient-simulator flow (create session, run interview, view
    own sessions/assessments). Any authenticated, active account may use the
    simulator: students for coursework, and admins/super-admins for testing the
    full workflow. This is deliberately broader than `require_student` so that a
    promoted admin/professor is no longer blocked from running patient cases,
    while remaining strictly authenticated (never public). Admin-created sessions
    are separately flagged as practice so they do not affect student analytics."""
    if current_user.role not in USER_ROLES:
        raise ForbiddenError("Simulator access is not permitted for this account.")
    return current_user


def require_session_access(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InterviewSession:
    """Load a session and enforce ownership. Admins may access any session; a
    student may only access sessions owned by their linked Student profile."""
    session = db.get(InterviewSession, session_id)
    if session is None:
        raise SessionNotFoundError(session_id)
    if user_can_access_session(current_user, session):
        return session
    # Do not reveal existence of another student's session.
    raise SessionNotFoundError(session_id)


def authorize_session_from_token(session_factory, request: Request, session_id: str) -> User:
    """Authenticate + ownership-check for endpoints that must NOT hold a
    request-scoped DB connection open for the whole response (streaming).

    Opens a short-lived session, verifies the token and ownership, closes it,
    and returns the authenticated user. Raises BEFORE any streaming begins so
    the client receives a clean 401/404 instead of a broken stream. Not leaking
    existence: unknown or unauthorized sessions both raise SessionNotFoundError.
    """
    token = _extract_bearer_token(request)
    db = session_factory()
    try:
        user = load_user_from_token(db, token)
        session = db.get(InterviewSession, session_id)
        if session is None or not user_can_access_session(user, session):
            raise SessionNotFoundError(session_id)
        return user
    finally:
        db.close()


def require_assessment_access(
    assessment_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ownership check for assessment-id-scoped routes: the caller must own (or
    admin) the session the assessment belongs to. Never leaks existence."""
    from app.models import AssessmentRun  # local import avoids import cycles

    run = db.get(AssessmentRun, assessment_id)
    if run is None:
        raise AssessmentNotFoundError(assessment_id)
    session = db.get(InterviewSession, run.session_id)
    if session is None or not user_can_access_session(current_user, session):
        raise AssessmentNotFoundError(assessment_id)
    return run
