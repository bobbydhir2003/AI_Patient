"""Server-authoritative "active assessment viewing time" tracking.

The client sends only silent heartbeats (no duration). Each ping credits the
time elapsed since the last heartbeat, CLAMPED, so accumulated ``active_seconds``
can never exceed real elapsed visible time and duplicate/parallel pings (refresh,
two tabs) self-deduplicate. One aggregate row per interview session.

Crediting rule per ping (server timestamps only):
  delta = now - last_heartbeat_at
  delta < 0            -> 0 (clock skew)
  0 <= delta <= 45     -> credit delta   (one dropped 20s beat still counts)
  delta > 45           -> credit 0        (student was away / idle)
  delta > 120          -> also a NEW visit (view_count += 1)
The first ping creates the row and credits 0.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import SESSION_STATUS_COMPLETED
from app.models import AssessmentRun, AssessmentViewSession, InterviewSession, User

# Expected client heartbeat interval is 20s; MAX_CREDIT (45s) tolerates one
# dropped beat while ignoring real idle gaps; GAP_RESET (120s) starts a new visit.
HEARTBEAT_INTERVAL_SECONDS = 20
MAX_CREDIT_SECONDS = 45
GAP_RESET_SECONDS = 120


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive timestamp as UTC. Postgres (timezone=True) returns aware
    datetimes; SQLite and some drivers return naive ones - normalize so the delta
    arithmetic is always aware-vs-aware."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def record_ping(
    db: Session,
    *,
    run: AssessmentRun,
    user: User,
    now: datetime | None = None,
) -> AssessmentViewSession | None:
    """Record one heartbeat for the owning student and return the row (or None
    when nothing is credited). Runs in one short transaction.

    Only the OWNING student's active viewing counts: an admin (or any non-owner)
    viewing the assessment never adds time. The session must be COMPLETED.
    """
    now = now or _now()
    session = db.get(InterviewSession, run.session_id)
    if session is None:
        return None
    # Owner-only: admins/others viewing never credit time.
    if session.student_id is None or user.student_id != session.student_id:
        return None
    if session.status != SESSION_STATUS_COMPLETED:
        return None

    row = db.execute(
        select(AssessmentViewSession).where(
            AssessmentViewSession.interview_session_id == session.id
        )
    ).scalar_one_or_none()

    if row is None:
        # First ping: create the row, credit nothing yet (baseline timestamp).
        row = AssessmentViewSession(
            interview_session_id=session.id,
            assessment_run_id=run.id,
            user_id=user.id,
            student_id=session.student_id,
            case_id=session.case_id,
            active_seconds=0,
            view_count=1,
            first_viewed_at=now,
            last_heartbeat_at=now,
        )
        db.add(row)
        db.commit()
        return row

    delta = (now - _as_utc(row.last_heartbeat_at)).total_seconds()
    if delta < 0:
        delta = 0.0
    if delta > GAP_RESET_SECONDS:
        row.view_count += 1
    if 0 <= delta <= MAX_CREDIT_SECONDS:
        row.active_seconds += int(delta)
    # delta > MAX_CREDIT_SECONDS credits nothing (student was away).
    row.last_heartbeat_at = now
    row.assessment_run_id = run.id
    db.commit()
    return row


def get_for_session(db: Session, session_id: str) -> AssessmentViewSession | None:
    """The single timing row for an interview session (admin display)."""
    return db.execute(
        select(AssessmentViewSession).where(
            AssessmentViewSession.interview_session_id == session_id
        )
    ).scalar_one_or_none()


def delete_for_sessions(db: Session, session_ids: list[str]) -> int:
    """Explicitly remove timing rows for the given sessions BEFORE the sessions
    themselves are deleted (belt-and-suspenders alongside ON DELETE CASCADE, so a
    purge is never blocked and never orphans a timing row). Caller owns commit."""
    if not session_ids:
        return 0
    rows = list(
        db.execute(
            select(AssessmentViewSession).where(
                AssessmentViewSession.interview_session_id.in_(session_ids)
            )
        ).scalars().all()
    )
    for row in rows:
        db.delete(row)
    db.flush()
    return len(rows)
