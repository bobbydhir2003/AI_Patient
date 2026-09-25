from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.constants import (
    PRIVILEGED_AUDIT_ACTIONS,
    PRIVILEGED_AUDIT_RECORD_TYPES,
    SUPER_ADMIN_ROLES,
)
from app.models import AuditLog

# Rows a normal admin may see (see constants.PRIVILEGED_AUDIT_*).
_NOT_PRIVILEGED = and_(
    AuditLog.action_type.not_in(PRIVILEGED_AUDIT_ACTIONS),
    AuditLog.record_type.not_in(PRIVILEGED_AUDIT_RECORD_TYPES),
)


def can_view_privileged(role: str | None) -> bool:
    """Only super admins may see system-administration audit events."""
    return role in SUPER_ADMIN_ROLES


class AuditRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def record(
        self,
        *,
        admin_user_id: str | None,
        admin_email: str,
        action_type: str,
        record_type: str,
        record_id: str,
        description: str,
    ) -> AuditLog:
        entry = AuditLog(
            admin_user_id=admin_user_id,
            admin_email=admin_email,
            action_type=action_type,
            record_type=record_type,
            record_id=record_id,
            description=description,
        )
        self.db.add(entry)
        self.db.flush()
        return entry

    def list(
        self, *, limit: int, offset: int, include_privileged: bool = False
    ) -> tuple[list[AuditLog], int]:
        """Newest first. Privileged (system-administration) rows are EXCLUDED
        unless the caller explicitly opts in for a super admin, so a new reader
        can never leak them by default."""
        where = [] if include_privileged else [_NOT_PRIVILEGED]
        total = int(
            self.db.execute(select(func.count(AuditLog.id)).where(*where)).scalar_one()
        )
        stmt = (
            select(AuditLog)
            .where(*where)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = list(self.db.execute(stmt).scalars().all())
        return rows, total
