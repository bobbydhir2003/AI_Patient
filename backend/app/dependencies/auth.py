"""Authentication / authorization FastAPI dependencies.

`get_current_user` validates the bearer token and loads the account.
`require_admin` / `require_student` enforce role-based access control.
`require_session_access` centralizes the ownership check so a student can only
reach their own sessions while admins can reach any.
"""
import jwt
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.constants import ADMIN_ROLES, USER_ROLE_STUDENT, USER_ROLE_SUPER_ADMIN
from app.core.exceptions import (
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


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    token = _extract_bearer_token(request)
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
    if current_user.role in ADMIN_ROLES:
        return session
    if current_user.student_id and session.student_id == current_user.student_id:
        return session
    # Do not reveal existence of another student's session.
    raise SessionNotFoundError(session_id)
