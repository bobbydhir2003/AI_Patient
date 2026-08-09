"""Admin account management: approve/reject/enable/disable + role changes.

All permission rules are enforced HERE (backend), never trusted from the client:
- an admin can never change their own role (self-lockout protection);
- there are exactly two roles (student/admin); every admin has all admin powers;
- the LAST active admin cannot be demoted, disabled or rejected;
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


def _active_admins(db: Session, exclude_id: str | None = None) -> int:
    """Count active administrators. Used to guarantee the system can never be
    left without an administrator (last-admin protection)."""
    stmt = select(func.count(User.id)).where(
        User.role.in_(list(ADMIN_ROLES)), User.account_status == ACCOUNT_STATUS_ACTIVE
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


def status_summary(db: Session) -> dict:
    """Real per-status counts for the summary cards - single grouped query plus
    an admin-role count. Never hardcoded example numbers."""
    rows = db.execute(
        select(User.account_status, func.count(User.id)).group_by(User.account_status)
    ).all()
    by_status = {status: int(count) for status, count in rows}
    admins = int(
        db.execute(
            select(func.count(User.id)).where(User.role.in_(list(ADMIN_ROLES)))
        ).scalar_one()
    )
    return {
        "total": sum(by_status.values()),
        "pending": by_status.get(ACCOUNT_STATUS_PENDING, 0),
        "active": by_status.get(ACCOUNT_STATUS_ACTIVE, 0),
        "disabled": by_status.get(ACCOUNT_STATUS_DISABLED, 0),
        "rejected": by_status.get(ACCOUNT_STATUS_REJECTED, 0),
        "admins": admins,
    }


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
    _guard_not_last_admin(db, target, "reject")
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
    _guard_not_last_admin(db, target, "disable")
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


# ---------------------------------------------------------------- bulk ops
def _pending_ids(db: Session) -> list[str]:
    return list(
        db.execute(
            select(User.id).where(User.account_status == ACCOUNT_STATUS_PENDING)
        ).scalars().all()
    )


def bulk_approve(db: Session, actor: User, user_ids: list[str]) -> dict:
    """Approve every PENDING account in `user_ids` in ONE transaction. Accounts
    that are missing or not pending are skipped (reported), never silently
    'approved'. Idempotent: re-approving an already-active account is a skip,
    so a duplicate submission cannot double-apply."""
    succeeded: list[str] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for uid in user_ids:
        if uid in seen:
            continue
        seen.add(uid)
        target = db.get(User, uid)
        if target is None:
            skipped.append({"user_id": uid, "reason": "not_found"})
            continue
        if target.account_status != ACCOUNT_STATUS_PENDING:
            skipped.append({"user_id": uid, "reason": f"not_pending ({target.account_status})"})
            continue
        old = target.account_status
        target.account_status = ACCOUNT_STATUS_ACTIVE
        _sync_active(target)
        _review(target, actor, None)
        _audit(db, actor, "ACCOUNT_APPROVED", target, old, ACCOUNT_STATUS_ACTIVE)
        succeeded.append(uid)
    db.commit()
    logger.info("bulk_approve actor=%s approved=%d skipped=%d", actor.email, len(succeeded), len(skipped))
    return {"succeeded": succeeded, "skipped": skipped, "summary": status_summary(db)}


def approve_all_pending(db: Session, actor: User) -> dict:
    """Approve ALL currently-pending accounts. The confirmation count the admin
    saw is a live snapshot; this re-reads pending at execution time so the real
    action is exactly 'approve whatever is pending now'."""
    return bulk_approve(db, actor, _pending_ids(db))


def bulk_reject(db: Session, actor: User, user_ids: list[str], note: str | None = None) -> dict:
    """Reject every account in `user_ids` in ONE transaction. Skips missing
    accounts, self, and the last active super-admin (never silently)."""
    succeeded: list[str] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for uid in user_ids:
        if uid in seen:
            continue
        seen.add(uid)
        target = db.get(User, uid)
        if target is None:
            skipped.append({"user_id": uid, "reason": "not_found"})
            continue
        if target.id == actor.id:
            skipped.append({"user_id": uid, "reason": "cannot_reject_self"})
            continue
        if target.account_status == ACCOUNT_STATUS_REJECTED:
            skipped.append({"user_id": uid, "reason": "already_rejected"})
            continue
        try:
            _guard_not_last_admin(db, target, "reject")
        except ForbiddenError as exc:
            skipped.append({"user_id": uid, "reason": str(exc)})
            continue
        old = target.account_status
        target.account_status = ACCOUNT_STATUS_REJECTED
        _sync_active(target)
        _review(target, actor, note)
        _audit(db, actor, "ACCOUNT_REJECTED", target, old, ACCOUNT_STATUS_REJECTED)
        succeeded.append(uid)
    db.commit()
    logger.info("bulk_reject actor=%s rejected=%d skipped=%d", actor.email, len(succeeded), len(skipped))
    return {"succeeded": succeeded, "skipped": skipped, "summary": status_summary(db)}


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

    # 2) Only the two application roles are assignable. Every admin has the same
    #    (full) powers, so any admin may promote a student to admin or demote an
    #    admin to student.
    if new_role not in (USER_ROLE_STUDENT, USER_ROLE_ADMIN):
        raise ForbiddenError("Administrators can only assign the student or admin role.")
    # 3) Never demote/remove the last active administrator.
    if old_role in ADMIN_ROLES and new_role not in ADMIN_ROLES:
        _guard_not_last_admin(db, target, "demote")

    target.role = new_role
    _audit(db, actor, "ROLE_CHANGED", target, old_role, new_role)
    db.commit()
    return target


def _guard_not_last_admin(db: Session, target: User, action: str) -> None:
    """Prevent an action that would remove the LAST active administrator, so an
    admin can never accidentally lock every administrator out of the system."""
    if target.role in ADMIN_ROLES and target.account_status == ACCOUNT_STATUS_ACTIVE:
        if _active_admins(db, exclude_id=target.id) == 0:
            raise ForbiddenError(f"Cannot {action} the last active administrator.")
