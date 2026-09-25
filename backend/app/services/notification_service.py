"""Admin notification feed derived from REAL recorded events.

There is no fabricated data here: notifications are built from the audit log
(real admin/system activity with real timestamps). The unread count is the
number of events newer than the admin's `notifications_read_at` timestamp, so
"3" means three genuine unacknowledged events. "Mark all read" advances that
timestamp; new activity makes the badge tick up again.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import AuditLog, User
from app.repositories.audit_repository import AuditRepository, can_view_privileged
from app.schemas.notification_schema import NotificationListOut, NotificationOut

# audit action_type -> (human title, notification type, link). The system events
# below are only ever delivered to super admins (list_notifications filters
# server-side), so they link into the Super Admin area.
_MAP = {
    "audio_cache_cleared": ("Audio cache cleared", "system", "/superadmin/dashboard"),
    "voice_previewed": ("Patient voice preview used", "voice", "/superadmin/dashboard"),
    "voice_updated": ("Patient voice updated", "voice", "/superadmin/dashboard"),
    "voice_restored": ("Patient voice restored to default", "voice", "/superadmin/dashboard"),
    "credential_replaced": ("API credential replaced", "credential", "/superadmin/credentials"),
    "credential_removed": ("API credential removed", "credential", "/superadmin/credentials"),
    "credential_tested": ("API connection tested", "credential", "/superadmin/credentials"),
    "ai_config_updated": ("AI configuration updated", "config", "/superadmin/ai-config"),
    "config_restored": ("Configuration restored", "config", "/superadmin/ai-config"),
    "load_test_started": ("Load test started", "system", "/superadmin/load-capacity"),
    "load_test_stopped": ("Load test stopped", "system", "/superadmin/load-capacity"),
    "survey_reset": ("Survey reset", "activity", "/superadmin/survey-resets"),
    "student_archived": ("Student archived", "student", "/admin/students"),
    "student_reactivated": ("Student reactivated", "student", "/admin/students"),
    "student_deleted": ("Student deleted", "student", "/admin/students"),
    "session_archived": ("Session archived", "session", "/admin/sessions"),
    "session_deleted": ("Session deleted", "session", "/admin/sessions"),
    "assessment_deleted": ("Assessment removed", "assessment", "/admin/assessments"),
    "message_deleted": ("Transcript message removed", "session", "/admin/sessions"),
}


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_notification(row: AuditLog, read_at: datetime | None) -> NotificationOut:
    title, ntype, link = _MAP.get(
        row.action_type, ("New admin activity recorded", "activity", "/admin/audit-log")
    )
    created = _aware(row.created_at)
    is_read = bool(read_at and created and created <= read_at)
    # Student/session links can target the specific record when we have its id.
    if row.record_type == "student" and row.record_id:
        link = f"/admin/students/{row.record_id}"
    elif row.record_type in ("session", "message") and row.record_id:
        link = f"/admin/sessions/{row.record_id}"
    return NotificationOut(
        id=row.id,
        title=title,
        message=row.description or title,
        type=ntype,
        created_at=created.isoformat() if created else "",
        is_read=is_read,
        link=link,
    )


def list_notifications(db: Session, user: User, limit: int = 20) -> NotificationListOut:
    rows, _ = AuditRepository(db).list(  # newest first; role-aware visibility
        limit=limit, offset=0, include_privileged=can_view_privileged(user.role)
    )
    read_at = _aware(user.notifications_read_at)
    items = [_to_notification(r, read_at) for r in rows]
    unread = sum(1 for n in items if not n.is_read)
    return NotificationListOut(notifications=items, unread_count=unread)


def mark_all_read(db: Session, user: User) -> None:
    user.notifications_read_at = datetime.now(timezone.utc)
    db.add(user)
    db.flush()
