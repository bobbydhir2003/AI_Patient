"""Admin account management: approve/reject/enable/disable + role changes.

All permission rules are enforced HERE (backend), never trusted from the client:
- an admin can never change their own role (self-lockout protection);
- only a super_admin may promote to / demote from super_admin;
- the LAST active super_admin cannot be demoted, disabled or rejected;
- a user cannot disable/reject themselves.
Every action is audited (target + old->new). Secrets are never involved here.

account_status is the source of truth for the approval lifecycle; is_active is
kept in lock-step (ACTIVE => True) so all existing is_active checks keep working.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.constants import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_DISABLED,
    ACCOUNT_STATUS_PENDING,
    ACCOUNT_STATUS_REJECTED,
    ADMIN_ROLES,
    USER_ROLE_ADMIN,
    USER_ROLE_STUDENT,
    USER_ROLE_SUPER_ADMIN,
    USER_ROLES,
)
from app.core.exceptions import (
    ForbiddenError,
    UserNotFoundError,
    ValidationFailedError,
)
from app.core.logging import get_logger
from app.models import User
from app.repositories.audit_repository import AuditRepository

logger = get_logger(__name__)


# ---------------------------------------------------------------- helpers
def _get(db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise UserNotFoundError(user_id)
    return user


def _sync_active(user: User) -> None:
    user.is_active = user.account_status == ACCOUNT_STATUS_ACTIVE


def _active_super_admins(db: Session, exclude_id: str | None = None) -> int:
    stmt = select(func.count(User.id)).where(
        User.role == USER_ROLE_SUPER_ADMIN, User.account_status == ACCOUNT_STATUS_ACTIVE
    )
    if exclude_id:
        stmt = stmt.where(User.id != exclude_id)
    return int(db.execute(stmt).scalar_one())


def _audit(db: Session, actor: User, action: str, target: User, old: str, new: str) -> None:
    AuditRepository(db).record(
        admin_user_id=actor.id,
        admin_email=actor.email,
        action_type=action,
        record_type="user",
        record_id=target.id,
        description=f"{target.email}: {old} -> {new}",
    )


def _review(target: User, actor: User, note: str | None) -> None:
    target.reviewed_by = actor.email
    target.reviewed_at = datetime.now(timezone.utc)
    target.review_note = (note or "").strip() or None


# ---------------------------------------------------------------- queries
def list_users(db: Session, *, status: str | None = None, role: str | None = None) -> list[User]:
    stmt = select(User).order_by(User.created_at.desc())
    if status and status.upper() != "ALL":
        if status.upper() == "ADMINS":
            stmt = stmt.where(User.role.in_(list(ADMIN_ROLES)))
        else:
            stmt = stmt.where(User.account_status == status.upper())
    if role:
        stmt = stmt.where(User.role == role)
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------- status ops
def approve(db: Session, actor: User, user_id: str) -> User:
    target = _get(db, user_id)
    old = target.account_status
    if old != ACCOUNT_STATUS_PENDING:
        raise ValidationFailedError("Only a pending account can be approved.")
    target.account_status = ACCOUNT_STATUS_ACTIVE
    _sync_active(target)
    _review(target, actor, None)
    _audit(db, actor, "ACCOUNT_APPROVED", target, old, ACCOUNT_STATUS_ACTIVE)
    db.commit()
    return target


def reject(db: Session, actor: User, user_id: str, note: str | None = None) -> User:
    target = _get(db, user_id)
    _guard_not_last_super_admin(db, target, "reject")
    old = target.account_status
    target.account_status = ACCOUNT_STATUS_REJECTED
    _sync_active(target)
    _review(target, actor, note)
    _audit(db, actor, "ACCOUNT_REJECTED", target, old, ACCOUNT_STATUS_REJECTED)
    db.commit()
    return target


def disable(db: Session, actor: User, user_id: str, note: str | None = None) -> User:
    target = _get(db, user_id)
    if target.id == actor.id:
        raise ForbiddenError("You cannot disable your own account.")
    _guard_not_last_super_admin(db, target, "disable")
    old = target.account_status
    target.account_status = ACCOUNT_STATUS_DISABLED
    _sync_active(target)
    _review(target, actor, note)
    _audit(db, actor, "ACCOUNT_DISABLED", target, old, ACCOUNT_STATUS_DISABLED)
    db.commit()
    return target


def enable(db: Session, actor: User, user_id: str) -> User:
    target = _get(db, user_id)
    old = target.account_status
    target.account_status = ACCOUNT_STATUS_ACTIVE
    _sync_active(target)
    _review(target, actor, None)
    _audit(db, actor, "ACCOUNT_ENABLED", target, old, ACCOUNT_STATUS_ACTIVE)
    db.commit()
    return target


# ---------------------------------------------------------------- role ops
def change_role(db: Session, actor: User, user_id: str, new_role: str) -> User:
    if new_role not in USER_ROLES:
        raise ValidationFailedError("Unknown role.")
    target = _get(db, user_id)
    old_role = target.role
    if old_role == new_role:
        return target

    # 1) No one may change their OWN role (prevents self-promotion/lockout).
    if target.id == actor.id:
        raise ForbiddenError("You cannot change your own role.")

    involves_super = USER_ROLE_SUPER_ADMIN in (old_role, new_role)
    # 2) Only a super_admin may promote to / demote from super_admin.
    if involves_super and actor.role != USER_ROLE_SUPER_ADMIN:
        raise ForbiddenError("Only a super administrator can grant or remove super administrator.")
    # 3) A normal admin may only move between student and admin.
    if actor.role == USER_ROLE_ADMIN and new_role not in (USER_ROLE_STUDENT, USER_ROLE_ADMIN):
        raise ForbiddenError("Administrators can only assign the student or admin role.")
    # 4) Never demote the last active super_admin.
    if old_role == USER_ROLE_SUPER_ADMIN and new_role != USER_ROLE_SUPER_ADMIN:
        _guard_not_last_super_admin(db, target, "demote")

    target.role = new_role
    _audit(db, actor, "ROLE_CHANGED", target, old_role, new_role)
    db.commit()
    return target


def _guard_not_last_super_admin(db: Session, target: User, action: str) -> None:
    if target.role == USER_ROLE_SUPER_ADMIN and target.account_status == ACCOUNT_STATUS_ACTIVE:
        if _active_super_admins(db, exclude_id=target.id) == 0:
            raise ForbiddenError(f"Cannot {action} the last active super administrator.")
