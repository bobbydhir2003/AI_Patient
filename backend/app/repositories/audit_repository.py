from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditLog


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

    def list(self, *, limit: int, offset: int) -> tuple[list[AuditLog], int]:
        total = int(self.db.execute(select(func.count(AuditLog.id))).scalar_one())
        stmt = (
            select(AuditLog)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = list(self.db.execute(stmt).scalars().all())
        return rows, total
