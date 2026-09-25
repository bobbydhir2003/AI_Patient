"""Admin account management: approve/reject/enable/disable + role changes.

All permission rules are enforced HERE (backend), never trusted from the client:
- an admin can never change their own role (self-lockout protection);
- only student/admin are ASSIGNABLE; super_admin is granted solely by the
  server-side bootstrap command (scripts/create_super_admin.py), never here;
- a super_admin account is PROTECTED: only another super admin may approve,
  reject, disable, enable, re-role or delete it (single and bulk paths alike);
- the LAST active admin cannot be demoted, disabled or rejected, and the LAST
  active super admin cannot be demoted, disabled, rejected or deleted;
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
    ASSIGNABLE_ROLES,
    AUDIT_RECORD_SUPER_ADMIN_USER,
    SUPER_ADMIN_ROLES,
    USER_ROLE_SUPER_ADMIN,
)
from app.core.exceptions import (
    DeleteConfirmationError,
    ForbiddenError,
    UserNotFoundError,
    ValidationFailedError,
)
from app.core.logging import get_logger
from app.models import Student, User
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


def _active_super_admins(db: Session, exclude_id: str | None = None) -> int:
    stmt = select(func.count(User.id)).where(
        User.role == USER_ROLE_SUPER_ADMIN, User.account_status == ACCOUNT_STATUS_ACTIVE
    )
    if exclude_id:
        stmt = stmt.where(User.id != exclude_id)
    return int(db.execute(stmt).scalar_one())


PROTECTED_SUPER_ADMIN = "protected_super_admin"


def _guard_privileged_target(actor: User, target: User) -> None:
    """A super_admin account can only be managed by a super admin. Raised for
    EVERY mutating account operation so a normal admin can never disable,
    reject, delete, re-role or otherwise alter a super admin, directly or via a
    bulk request."""
    if target.role == USER_ROLE_SUPER_ADMIN and actor.role not in SUPER_ADMIN_ROLES:
        raise ForbiddenError("Only a Super Admin can manage a Super Admin account.")


def _user_record_type(*roles: str | None) -> str:
    """Account events that involve a super admin (target is one, or the role
    change is from/to super_admin) are tagged so only super admins see them in
    the Activity Log / notifications (see constants.PRIVILEGED_AUDIT_*)."""
    return AUDIT_RECORD_SUPER_ADMIN_USER if USER_ROLE_SUPER_ADMIN in roles else "user"


def _audit(db: Session, actor: User, action: str, target: User, old: str, new: str) -> None:
    AuditRepository(db).record(
        admin_user_id=actor.id,
        admin_email=actor.email,
        action_type=action,
        # old/new are statuses or roles; either way a super_admin mention counts.
        record_type=_user_record_type(target.role, old, new),
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
    _guard_privileged_target(actor, target)
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
    _guard_privileged_target(actor, target)
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
    _guard_privileged_target(actor, target)
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
    _guard_privileged_target(actor, target)
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


def _is_protected(actor: User, target: User) -> bool:
    try:
        _guard_privileged_target(actor, target)
    except ForbiddenError:
        return True
    return False


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
        if _is_protected(actor, target):
            skipped.append({"user_id": uid, "reason": PROTECTED_SUPER_ADMIN})
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
        if _is_protected(actor, target):
            skipped.append({"user_id": uid, "reason": PROTECTED_SUPER_ADMIN})
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


# ---------------------------------------------------------------- delete ops
def bulk_delete(db: Session, actor: User, user_ids: list[str], confirm: str) -> dict:
    """PERMANENTLY delete every account in `user_ids` and its ENTIRE local data
    tree (student profile, interview sessions, transcripts, assessment runs/
    results/evidence, and survey receipts), in ONE transaction.

    This is a HARD delete, deliberately distinct from `disable` (which only blocks
    access and preserves all data). Each account is deleted inside its OWN
    SAVEPOINT, so a single account's failure is recorded in `skipped` and never
    corrupts or rolls back the rest of the batch.

    Safety: requires the typed confirmation ("DELETE"); never deletes the acting
    admin (`cannot_delete_self`) or the last active administrator
    (`cannot_delete_last_admin`, enforced cumulatively across the batch).

    REDCap: survey *answers* live exclusively in REDCap and are NEVER touched -
    only the LOCAL SurveyReceipt linkage rows are removed. No REDCap request is
    made by this path.
    """
    from app.services import admin_service  # local import avoids an import cycle

    if (confirm or "").strip().upper() != "DELETE":
        raise DeleteConfirmationError()

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
            skipped.append({"user_id": uid, "reason": "cannot_delete_self"})
            continue
        if _is_protected(actor, target):
            skipped.append({"user_id": uid, "reason": PROTECTED_SUPER_ADMIN})
            continue
        try:
            _guard_not_last_admin(db, target, "delete")
        except ForbiddenError as exc:
            skipped.append({"user_id": uid, "reason": str(exc)})
            continue
        # Capture identity BEFORE the row is queued for deletion (attributes are
        # still readable but we avoid relying on post-delete access).
        email = target.email
        prior_status = target.account_status
        student_id = target.student_id
        record_type = _user_record_type(target.role)
        try:
            with db.begin_nested():
                if student_id:
                    student = db.get(Student, student_id)
                    if student is not None:
                        # Deletes the student's whole tree AND this login account
                        # (student.user), and writes its own AUDIT_STUDENT_DELETED.
                        admin_service.purge_student_tree(db, actor, student)
                    else:
                        db.delete(target)  # dangling student_id: remove login only
                        _audit_delete(db, actor, uid, email, prior_status, record_type)
                else:
                    db.delete(target)  # admin-only / studentless account
                    _audit_delete(db, actor, uid, email, prior_status, record_type)
                db.flush()
        except Exception as exc:  # savepoint auto-rolled back; batch continues
            skipped.append({"user_id": uid, "reason": f"delete_failed: {exc.__class__.__name__}"})
            continue
        succeeded.append(uid)
    db.commit()
    logger.info("bulk_delete actor=%s deleted=%d skipped=%d", actor.email, len(succeeded), len(skipped))
    return {"succeeded": succeeded, "skipped": skipped, "summary": status_summary(db)}


def _audit_delete(
    db: Session, actor: User, user_id: str, email: str, prior_status: str, record_type: str = "user"
) -> None:
    AuditRepository(db).record(
        admin_user_id=actor.id,
        admin_email=actor.email,
        action_type="ACCOUNT_DELETED",
        record_type=record_type,
        record_id=user_id,
        description=f"{email}: {prior_status} -> permanently deleted (account + all local data).",
    )


# ---------------------------------------------------------------- role ops
def change_role(db: Session, actor: User, user_id: str, new_role: str) -> User:
    # 1) Only student/admin are assignable through this API - by ANY caller,
    #    super admins included. super_admin comes only from the bootstrap command.
    if new_role == USER_ROLE_SUPER_ADMIN:
        raise ForbiddenError("The Super Admin role cannot be assigned here.")
    if new_role not in ASSIGNABLE_ROLES:
        raise ValidationFailedError("Unknown role.")
    target = _get(db, user_id)
    old_role = target.role
    if old_role == new_role:
        return target

    # 2) No one may change their OWN role (prevents self-promotion/lockout).
    if target.id == actor.id:
        raise ForbiddenError("You cannot change your own role.")
    # 3) Only a super admin may change a super admin's role.
    _guard_privileged_target(actor, target)
    # 4) Never demote/remove the last active administrator / super admin.
    if old_role in ADMIN_ROLES and new_role not in ADMIN_ROLES:
        _guard_not_last_admin(db, target, "demote")
    _guard_not_last_super_admin(db, target, "demote")

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
    _guard_not_last_super_admin(db, target, action)


def _guard_not_last_super_admin(db: Session, target: User, action: str) -> None:
    """System administration must never become unreachable: the last active
    super admin cannot be demoted, disabled, rejected or deleted (the bootstrap
    command remains the recovery path)."""
    if target.role == USER_ROLE_SUPER_ADMIN and target.account_status == ACCOUNT_STATUS_ACTIVE:
        if _active_super_admins(db, exclude_id=target.id) == 0:
            raise ForbiddenError(f"Cannot {action} the last active Super Admin.")
