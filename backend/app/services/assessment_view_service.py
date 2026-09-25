"""Server-authoritative "active assessment viewing time" tracking.

A VISIT is one real assessment page mount, identified by a client-generated
``visit_id``. ``assessment_view_visits`` holds one row per visit (the source of
truth for view COUNT and per-visit time); ``assessment_view_sessions`` is the
fast denormalized summary over them (total active_seconds, view_count, first/last
viewed). The client sends only a heartbeat + its visit_id (never a duration).

Crediting per heartbeat (server timestamps only):
  new visit_id            -> create the visit (recording its declared source),
                             credit 0, summary.view_count += 1
  same visit_id:
    delta = now - visit.last_heartbeat_at
    delta < 0             -> credit 0 (clock skew)
    0 <= delta <= 45      -> credit delta   (one dropped 20s beat still counts)
    delta > 45            -> credit 0        (hidden/away/idle)
A new visit's first ping always credits 0, so away/between-visit gaps never count.
There is NO elapsed-gap visit counting - views come only from new visit rows.
A visit's source is fixed when the row is created; later pings never change it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.constants import SESSION_STATUS_COMPLETED
from app.models import (
    AssessmentRun,
    AssessmentViewSession,
    AssessmentViewVisit,
    InterviewSession,
    User,
)
from app.models.assessment_view import VISIT_CLIENT_SOURCES, VISIT_SOURCE_UNKNOWN

# Expected client heartbeat interval is 20s; MAX_CREDIT (45s) tolerates one
# dropped beat while ignoring hidden/idle gaps.
HEARTBEAT_INTERVAL_SECONDS = 20
MAX_CREDIT_SECONDS = 45
# Fallback visit_id for a version-skewed old client that pings without one during
# a rollout: it produces exactly ONE implicit visit per session (never repeats).
LEGACY_CLIENT_VISIT_ID = "__legacy_client__"


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
    visit_id: str | None,
    source: str | None = None,
    now: datetime | None = None,
) -> AssessmentViewSession | None:
    """Record one heartbeat for the owning student's visit and return the summary
    row (or None when nothing is credited). Runs in one short transaction.

    Only the OWNING student's active viewing counts (admins/others never credit).
    The session must be COMPLETED. ``visit_id`` is an opaque client id; a blank/
    missing one maps to a single implicit legacy visit (rollout tolerance).
    ``source`` is used only if this ping creates the visit; anything outside the
    client enum (or missing) is stored as "unknown".
    """
    now = now or _now()
    vid = (visit_id or "").strip() or LEGACY_CLIENT_VISIT_ID
    src = source if source in VISIT_CLIENT_SOURCES else VISIT_SOURCE_UNKNOWN
    session = db.get(InterviewSession, run.session_id)
    if session is None:
        return None
    # Owner-only: admins/others viewing never credit time.
    if session.student_id is None or user.student_id != session.student_id:
        return None
    if session.status != SESSION_STATUS_COMPLETED:
        return None

    try:
        summary = _apply_ping(
            db, session=session, run=run, user=user, vid=vid, src=src, now=now
        )
        db.commit()
        return summary
    except IntegrityError:
        # A concurrent duplicate first ping for the same (session, visit_id) won
        # the unique constraint; retry as an existing-visit heartbeat.
        db.rollback()
        summary = _apply_ping(
            db, session=session, run=run, user=user, vid=vid, src=src, now=now
        )
        db.commit()
        return summary


def _apply_ping(
    db: Session,
    *,
    session: InterviewSession,
    run: AssessmentRun,
    user: User,
    vid: str,
    src: str,
    now: datetime,
) -> AssessmentViewSession:
    """Find-or-create the visit + update the summary (no commit). Raises
    IntegrityError if a concurrent insert created the same visit first."""
    visit = db.execute(
        select(AssessmentViewVisit).where(
            AssessmentViewVisit.interview_session_id == session.id,
            AssessmentViewVisit.visit_id == vid,
        )
    ).scalar_one_or_none()
    summary = get_for_session(db, session.id)

    if visit is None:
        # NEW visit: create it, credit 0, and bump the summary view_count by one.
        visit = AssessmentViewVisit(
            visit_id=vid,
            interview_session_id=session.id,
            assessment_run_id=run.id,
            user_id=user.id,
            student_id=session.student_id,
            case_id=session.case_id,
            source=src,
            started_at=now,
            last_heartbeat_at=now,
            active_seconds=0,
        )
        db.add(visit)
        db.flush()  # surfaces the unique-constraint race as IntegrityError
        if summary is None:
            summary = AssessmentViewSession(
                interview_session_id=session.id,
                assessment_run_id=run.id,
                user_id=user.id,
                student_id=session.student_id,
                case_id=session.case_id,
                active_seconds=0,
                view_count=1,
                first_viewed_at=now,
                last_heartbeat_at=now,
                last_viewed_at=now,
            )
            db.add(summary)
        else:
            summary.view_count += 1
            summary.last_heartbeat_at = now
            summary.last_viewed_at = now
            summary.assessment_run_id = run.id
        return summary

    # EXISTING visit: credit bounded elapsed time to both the visit and summary.
    # Its source is deliberately left untouched (fixed at creation).
    delta = (now - _as_utc(visit.last_heartbeat_at)).total_seconds()
    credit = int(delta) if 0 <= delta <= MAX_CREDIT_SECONDS else 0
    visit.active_seconds += credit
    visit.last_heartbeat_at = now
    if summary is None:
        # Defensive: summary missing though a visit exists - rebuild from the visit.
        summary = AssessmentViewSession(
            interview_session_id=session.id,
            assessment_run_id=run.id,
            user_id=user.id,
            student_id=session.student_id,
            case_id=session.case_id,
            active_seconds=credit,
            view_count=1,
            first_viewed_at=visit.started_at,
            last_heartbeat_at=now,
            last_viewed_at=now,
        )
        db.add(summary)
    else:
        summary.active_seconds += credit
        summary.last_heartbeat_at = now
        summary.last_viewed_at = now
        summary.assessment_run_id = run.id
    return summary


def get_for_session(db: Session, session_id: str) -> AssessmentViewSession | None:
    """The single timing row for an interview session (admin display)."""
    return db.execute(
        select(AssessmentViewSession).where(
            AssessmentViewSession.interview_session_id == session_id
        )
    ).scalar_one_or_none()


def map_for_sessions(db: Session, session_ids: list[str]) -> dict[str, AssessmentViewSession]:
    """Batched {interview_session_id: row} for a page of sessions (one query, no
    N+1). Sessions never viewed are simply absent from the map."""
    if not session_ids:
        return {}
    rows = db.execute(
        select(AssessmentViewSession).where(
            AssessmentViewSession.interview_session_id.in_(session_ids)
        )
    ).scalars().all()
    return {r.interview_session_id: r for r in rows}


def visits_for_sessions(
    db: Session, session_ids: list[str]
) -> dict[str, list[AssessmentViewVisit]]:
    """Batched {interview_session_id: [visits oldest-first]} (one query, no N+1).
    The list order defines the admin-facing visit number (1-based)."""
    if not session_ids:
        return {}
    rows = db.execute(
        select(AssessmentViewVisit)
        .where(AssessmentViewVisit.interview_session_id.in_(session_ids))
        .order_by(AssessmentViewVisit.started_at.asc(), AssessmentViewVisit.created_at.asc())
    ).scalars().all()
    out: dict[str, list[AssessmentViewVisit]] = {}
    for v in rows:
        out.setdefault(v.interview_session_id, []).append(v)
    return out


def delete_for_sessions(db: Session, session_ids: list[str]) -> int:
    """Explicitly remove BOTH visit rows and summary rows for the given sessions
    BEFORE the sessions themselves are deleted (belt-and-suspenders alongside ON
    DELETE CASCADE, so a purge is never blocked and never orphans a timing row).
    Caller owns commit. Returns the number of summary rows removed."""
    if not session_ids:
        return 0
    visits = list(
        db.execute(
            select(AssessmentViewVisit).where(
                AssessmentViewVisit.interview_session_id.in_(session_ids)
            )
        ).scalars().all()
    )
    for v in visits:
        db.delete(v)
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
