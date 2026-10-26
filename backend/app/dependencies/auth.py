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
    LTI_ROLE_INSTRUCTOR,
    LTI_ROLE_LEARNER,
    USER_ROLE_STUDENT,
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
    user.lti_claims = _lti_claims(payload)
    return user


def _lti_claims(payload: dict) -> dict | None:
    """The course-offering scope of a token issued by an AI iEXCEL launch, or
    None for a direct-login token. LTI tokens stop working the moment the
    integration is switched off."""
    claims = payload.get("lti")
    if claims is None:
        return None
    from app.core.config import get_settings

    if not get_settings().ai_sp_lti_enabled:
        raise NotAuthenticatedError("Course launches are not enabled. Please sign in again.")
    if (
        not isinstance(claims, dict)
        or not isinstance(claims.get("ctx"), str)
        or claims.get("role") not in (LTI_ROLE_LEARNER, LTI_ROLE_INSTRUCTOR)
    ):
        raise NotAuthenticatedError("Invalid authentication token.")
    return {"ctx": claims["ctx"], "role": claims["role"]}


def lti_role(user: User) -> str | None:
    claims = getattr(user, "lti_claims", None)
    return claims["role"] if claims else None


def user_can_access_session(user: User, session: InterviewSession) -> bool:
    """Ownership rule (single source of truth): admins may reach any session; a
    student may reach only sessions owned by their linked Student profile.

    A launched (LTI) learner may reach only their own sessions in the launched
    course offering. A launched instructor never uses the student routes; the
    course-scoped instructor routes (app.api.lti) do their own checks."""
    claims = getattr(user, "lti_claims", None)
    if claims is not None:
        if claims["role"] != LTI_ROLE_LEARNER:
            return False
        return bool(
            user.student_id
            and session.student_id == user.student_id
            and session.context_id == claims["ctx"]
        )
    if user.role in ADMIN_ROLES:
        return True
    return bool(user.student_id and session.student_id == user.student_id)


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    return load_user_from_token(db, _extract_bearer_token(request))


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """The single administrative gate. Every admin holds ALL admin powers."""
    if current_user.role not in ADMIN_ROLES:
        raise ForbiddenError("Administrator access is required.")
    return current_user


# Backward-compatibility alias. The former super/system-admin tier has been
# consolidated into the normal admin role, so any endpoint that historically
# required a "super admin" now simply requires a normal admin. Kept as an alias
# so existing imports keep working without granting extra privileges.
require_super_admin = require_admin


def require_student(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != USER_ROLE_STUDENT or lti_role(current_user) == LTI_ROLE_INSTRUCTOR:
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
    if current_user.role not in USER_ROLES or lti_role(current_user) == LTI_ROLE_INSTRUCTOR:
        raise ForbiddenError("Simulator access is not permitted for this account.")
    return current_user


def require_lti_instructor(current_user: User = Depends(get_current_user)) -> User:
    """Course-scoped instructor access from an AI iEXCEL launch. Grants only the
    launched offering (claims["ctx"]); never AI SP administrator access."""
    if lti_role(current_user) != LTI_ROLE_INSTRUCTOR:
        raise ForbiddenError("Course instructor access is required.")
    return current_user


def require_lti_session(current_user: User = Depends(get_current_user)) -> User:
    if current_user.lti_claims is None:
        raise ForbiddenError("This resource is only available to course launches.")
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
