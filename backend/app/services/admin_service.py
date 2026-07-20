"""Admin panel read + management logic.

All destructive operations run inside a single DB transaction and write an
AuditLog row before commit, so the trail and the change are atomic. Cascade
deletion is performed explicitly (runs -> evidence/domains, then session ->
turns) to guarantee no orphaned rows on either SQLite or PostgreSQL.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core import constants
from app.core.exceptions import (
    AssessmentNotFoundError,
    DeleteConfirmationError,
    SelfDeletionError,
    SessionNotFoundError,
    StudentNotFoundError,
)
from app.core.logging import get_logger
from app.models import (
    AssessmentDomainResult,
    AssessmentEvidence,
    AssessmentRun,
    ConversationTurn,
    InterviewSession,
    Student,
    User,
)
from app.repositories.audit_repository import AuditRepository
from app.schemas.admin import (
    AssessmentLevelCount,
    AuditLogOut,
    DashboardOut,
    NeedsAttentionOut,
    PaginatedAuditLogs,
    PaginatedSessions,
    PaginatedStudents,
    RecentActivityItem,
    RecentSessionItem,
    RecentStudentItem,
    SearchResultsOut,
    SearchSessionHit,
    SearchStudentHit,
    SessionSummaryOut,
    StudentDetailOut,
    StudentListItem,
    TranscriptMessageOut,
)

logger = get_logger(__name__)

_COMPLETED = constants.SESSION_STATUS_COMPLETED
_ARCHIVED = constants.SESSION_STATUS_ARCHIVED
_ACTIVE = constants.SESSION_STATUS_ACTIVE

# Canonical display order for the assessment-level summary. Standard rubric
# levels first, then the advanced-referral levels, so mixed data still renders
# in a sensible order. Any other stored value is appended afterwards.
_LEVEL_ORDER = list(constants.PERFORMANCE_LEVELS) + [
    lvl for lvl in constants.REFERRAL_OVERALL_LEVELS if lvl not in constants.PERFORMANCE_LEVELS
]


# ------------------------------------------------------------------ helpers
def _duration_seconds(session: InterviewSession) -> int | None:
    if session.completed_at is None:
        return None
    return int((session.completed_at - session.started_at).total_seconds())


def _session_counts(db: Session, session_id: str) -> tuple[int, int]:
    total = int(
        db.execute(
            select(func.count(ConversationTurn.id)).where(
                ConversationTurn.session_id == session_id
            )
        ).scalar_one()
    )
    student = int(
        db.execute(
            select(func.count(ConversationTurn.id)).where(
                ConversationTurn.session_id == session_id,
                ConversationTurn.role == "student",
            )
        ).scalar_one()
    )
    return total, student


def _session_has_assessment(db: Session, session_id: str) -> bool:
    return (
        db.execute(
            select(func.count(AssessmentRun.id)).where(AssessmentRun.session_id == session_id)
        ).scalar_one()
        > 0
    )


def _session_summary(db: Session, session: InterviewSession) -> SessionSummaryOut:
    total, student_turns = _session_counts(db, session.id)
    return SessionSummaryOut(
        session_id=session.id,
        student_id=session.student_id,
        student_name=session.student.name if session.student else "",
        case_id=session.case_id,
        case_category=session.case_category,
        status=session.status,
        locked=session.locked,
        turn_count=total,
        student_turn_count=student_turns,
        duration_seconds=_duration_seconds(session),
        has_assessment=_session_has_assessment(db, session.id),
        overall_level=_latest_level_for_session(db, session.id),
        started_at=session.started_at,
        completed_at=session.completed_at,
    )


def session_summary(db: Session, session: InterviewSession) -> SessionSummaryOut:
    """Public builder reused by the student self-service router."""
    return _session_summary(db, session)


def _audit(db: Session, admin: User, *, action, record_type, record_id, description) -> None:
    AuditRepository(db).record(
        admin_user_id=admin.id,
        admin_email=admin.email,
        action_type=action,
        record_type=record_type,
        record_id=record_id,
        description=description,
    )


# ------------------------------------------------------------------ dashboard
def get_dashboard(db: Session) -> DashboardOut:
    total_students = int(db.execute(select(func.count(Student.id))).scalar_one())
    active_students = int(
        db.execute(select(func.count(Student.id)).where(Student.is_active.is_(True))).scalar_one()
    )
    total_sessions = int(db.execute(select(func.count(InterviewSession.id))).scalar_one())
    completed = int(
        db.execute(
            select(func.count(InterviewSession.id)).where(InterviewSession.status == _COMPLETED)
        ).scalar_one()
    )
    archived = int(
        db.execute(
            select(func.count(InterviewSession.id)).where(InterviewSession.status == _ARCHIVED)
        ).scalar_one()
    )
    total_assessments = int(db.execute(select(func.count(AssessmentRun.id))).scalar_one())

    recent_rows = list(
        db.execute(
            select(InterviewSession)
            .order_by(InterviewSession.started_at.desc())
            .limit(8)
        ).scalars().all()
    )
    recent = [
        RecentActivityItem(
            session_id=s.id,
            student_id=s.student_id,
            student_name=s.student.name if s.student else "",
            case_id=s.case_id,
            status=s.status,
            started_at=s.started_at,
            completed_at=s.completed_at,
        )
        for s in recent_rows
    ]

    incomplete = total_sessions - completed - archived
    return DashboardOut(
        total_students=total_students,
        active_students=active_students,
        inactive_students=total_students - active_students,
        total_sessions=total_sessions,
        completed_sessions=completed,
        incomplete_sessions=incomplete,
        archived_sessions=archived,
        total_assessments=total_assessments,
        recent_activity=recent,
        assessment_levels=_assessment_level_summary(db),
        needs_attention=_needs_attention(db, incomplete=incomplete),
        recent_sessions=_recent_sessions_with_level(db, limit=6),
        recent_students=_recent_students(db, limit=4),
    )


def _assessment_level_summary(db: Session) -> list[AssessmentLevelCount]:
    """Distribution of completed assessments by their overall qualitative level.

    Uses the exact `overall_level` values stored by the assessment pipeline
    (never invents scores). Rows without a level yet (pending/failed) are
    omitted from the summary.
    """
    rows = db.execute(
        select(AssessmentRun.overall_level, func.count(AssessmentRun.id))
        .where(AssessmentRun.overall_level.is_not(None))
        .group_by(AssessmentRun.overall_level)
    ).all()
    counts = {level: int(n) for level, n in rows if level}
    ordered: list[AssessmentLevelCount] = [
        AssessmentLevelCount(level=lvl, count=counts.pop(lvl))
        for lvl in _LEVEL_ORDER
        if lvl in counts
    ]
    # Any stored level not in the canonical order (defensive) keeps its count.
    ordered.extend(AssessmentLevelCount(level=lvl, count=n) for lvl, n in counts.items())
    return ordered


def _needs_attention(db: Session, *, incomplete: int) -> NeedsAttentionOut:
    completed_without_assessment = int(
        db.execute(
            select(func.count(InterviewSession.id)).where(
                InterviewSession.status == _COMPLETED,
                ~InterviewSession.id.in_(select(AssessmentRun.session_id)),
            )
        ).scalar_one()
    )
    # Students with 2+ still-active (incomplete) sessions.
    multi = db.execute(
        select(InterviewSession.student_id)
        .where(InterviewSession.status == _ACTIVE)
        .group_by(InterviewSession.student_id)
        .having(func.count(InterviewSession.id) >= 2)
    ).all()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    stale = int(
        db.execute(
            select(func.count(InterviewSession.id)).where(
                InterviewSession.status == _ACTIVE,
                InterviewSession.started_at < cutoff,
            )
        ).scalar_one()
    )
    return NeedsAttentionOut(
        incomplete_sessions=incomplete,
        completed_without_assessment=completed_without_assessment,
        students_multiple_incomplete=len(multi),
        sessions_active_over_24h=stale,
    )


def _latest_level_for_session(db: Session, session_id: str) -> str | None:
    return db.execute(
        select(AssessmentRun.overall_level)
        .where(AssessmentRun.session_id == session_id)
        .order_by(AssessmentRun.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _recent_sessions_with_level(db: Session, *, limit: int = 6) -> list[RecentSessionItem]:
    rows = list(
        db.execute(
            select(InterviewSession)
            .order_by(InterviewSession.started_at.desc())
            .limit(limit)
        ).scalars().all()
    )
    items: list[RecentSessionItem] = []
    for s in rows:
        items.append(
            RecentSessionItem(
                session_id=s.id,
                student_id=s.student_id,
                student_name=s.student.name if s.student else "",
                student_number=s.student.student_number if s.student else "",
                case_id=s.case_id,
                case_category=s.case_category,
                status=s.status,
                has_assessment=_session_has_assessment(db, s.id),
                overall_level=_latest_level_for_session(db, s.id),
                started_at=s.started_at,
                completed_at=s.completed_at,
            )
        )
    return items


def _recent_students(db: Session, *, limit: int = 4) -> list[RecentStudentItem]:
    last_activity = (
        select(
            InterviewSession.student_id.label("sid"),
            func.max(InterviewSession.started_at).label("last"),
        )
        .group_by(InterviewSession.student_id)
        .subquery()
    )
    rows = list(
        db.execute(
            select(Student, last_activity.c.last)
            .join(last_activity, last_activity.c.sid == Student.id)
            .order_by(last_activity.c.last.desc())
            .limit(limit)
        ).all()
    )
    items: list[RecentStudentItem] = []
    for student, last in rows:
        sc, _cc, ac, _last = _student_counts(db, student.id)
        items.append(
            RecentStudentItem(
                id=student.id,
                name=student.name,
                student_number=student.student_number,
                session_count=sc,
                assessment_count=ac,
                last_activity_at=last,
            )
        )
    return items


# ------------------------------------------------------------------ search
def search(db: Session, *, query: str, limit: int = 6) -> SearchResultsOut:
    """Practical global search across students and sessions for the top bar.

    Matches students by name / email / student number, and sessions by id,
    case id, or the owning student's name. Admin-scoped (router enforces auth).
    """
    q = (query or "").strip()
    if not q:
        return SearchResultsOut(students=[], sessions=[])
    like = f"%{q.lower()}%"

    student_rows = list(
        db.execute(
            select(Student)
            .where(
                or_(
                    func.lower(Student.name).like(like),
                    func.lower(Student.email).like(like),
                    func.lower(Student.student_number).like(like),
                )
            )
            .order_by(func.lower(Student.name).asc())
            .limit(limit)
        ).scalars().all()
    )
    students = [
        SearchStudentHit(
            id=s.id,
            name=s.name,
            email=s.email or (s.user.email if s.user else ""),
            student_number=s.student_number,
            is_active=s.is_active,
        )
        for s in student_rows
    ]

    session_rows = list(
        db.execute(
            select(InterviewSession)
            .join(Student, InterviewSession.student_id == Student.id)
            .where(
                or_(
                    func.lower(InterviewSession.id).like(like),
                    func.lower(InterviewSession.case_id).like(like),
                    func.lower(Student.name).like(like),
                )
            )
            .order_by(InterviewSession.started_at.desc())
            .limit(limit)
        ).scalars().all()
    )
    sessions = [
        SearchSessionHit(
            session_id=s.id,
            student_id=s.student_id,
            student_name=s.student.name if s.student else "",
            case_id=s.case_id,
            status=s.status,
            started_at=s.started_at,
        )
        for s in session_rows
    ]
    return SearchResultsOut(students=students, sessions=sessions)


# ------------------------------------------------------------------ students
def list_students(
    db: Session,
    *,
    search: str = "",
    status: str = "all",  # all | active | inactive
    sort: str = "newest",  # newest | oldest | name
    page: int = 1,
    page_size: int = 20,
) -> PaginatedStudents:
    stmt = select(Student)
    if search.strip():
        like = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Student.name).like(like),
                func.lower(Student.email).like(like),
                func.lower(Student.student_number).like(like),
            )
        )
    if status == "active":
        stmt = stmt.where(Student.is_active.is_(True))
    elif status == "inactive":
        stmt = stmt.where(Student.is_active.is_(False))

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())

    if sort == "oldest":
        stmt = stmt.order_by(Student.created_at.asc())
    elif sort == "name":
        stmt = stmt.order_by(func.lower(Student.name).asc())
    else:
        stmt = stmt.order_by(Student.created_at.desc())

    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    stmt = stmt.limit(page_size).offset((page - 1) * page_size)
    students = list(db.execute(stmt).scalars().all())

    items = [_student_list_item(db, s) for s in students]
    return PaginatedStudents(items=items, total=total, page=page, page_size=page_size)


def _student_session_ids(db: Session, student_id: str) -> list[str]:
    return list(
        db.execute(
            select(InterviewSession.id).where(InterviewSession.student_id == student_id)
        ).scalars().all()
    )


def _student_counts(db: Session, student_id: str) -> tuple[int, int, int, datetime | None]:
    session_count = int(
        db.execute(
            select(func.count(InterviewSession.id)).where(
                InterviewSession.student_id == student_id
            )
        ).scalar_one()
    )
    completed_count = int(
        db.execute(
            select(func.count(InterviewSession.id)).where(
                InterviewSession.student_id == student_id,
                InterviewSession.status == _COMPLETED,
            )
        ).scalar_one()
    )
    assessment_count = int(
        db.execute(
            select(func.count(AssessmentRun.id))
            .join(InterviewSession, AssessmentRun.session_id == InterviewSession.id)
            .where(InterviewSession.student_id == student_id)
        ).scalar_one()
    )
    last_activity = db.execute(
        select(func.max(InterviewSession.started_at)).where(
            InterviewSession.student_id == student_id
        )
    ).scalar_one()
    return session_count, completed_count, assessment_count, last_activity


def _student_list_item(db: Session, student: Student) -> StudentListItem:
    sc, cc, ac, last = _student_counts(db, student.id)
    return StudentListItem(
        id=student.id,
        name=student.name,
        email=student.email or (student.user.email if student.user else ""),
        student_number=student.student_number,
        is_active=student.is_active,
        has_account=student.user is not None,
        session_count=sc,
        completed_count=cc,
        assessment_count=ac,
        created_at=student.created_at,
        last_activity_at=last,
    )


def _get_student_or_404(db: Session, student_id: str) -> Student:
    student = db.get(Student, student_id)
    if student is None:
        raise StudentNotFoundError(student_id)
    return student


def get_student(db: Session, student_id: str) -> StudentDetailOut:
    student = _get_student_or_404(db, student_id)
    sc, cc, ac, _ = _student_counts(db, student.id)
    user = student.user
    return StudentDetailOut(
        id=student.id,
        name=student.name,
        email=student.email or (user.email if user else ""),
        student_number=student.student_number,
        is_active=student.is_active,
        has_account=user is not None,
        account_email=user.email if user else None,
        role=user.role if user else None,
        created_at=student.created_at,
        last_login_at=user.last_login_at if user else None,
        session_count=sc,
        completed_count=cc,
        assessment_count=ac,
    )


def list_student_sessions(db: Session, student_id: str) -> list[SessionSummaryOut]:
    _get_student_or_404(db, student_id)
    rows = list(
        db.execute(
            select(InterviewSession)
            .where(InterviewSession.student_id == student_id)
            .order_by(InterviewSession.started_at.desc())
        ).scalars().all()
    )
    return [_session_summary(db, s) for s in rows]


# ------------------------------------------------------------------ sessions
def list_sessions(
    db: Session,
    *,
    case_id: str = "",
    status: str = "all",
    sort: str = "newest",
    page: int = 1,
    page_size: int = 20,
) -> PaginatedSessions:
    stmt = select(InterviewSession)
    if case_id.strip():
        stmt = stmt.where(InterviewSession.case_id == case_id.strip())
    if status and status != "all":
        stmt = stmt.where(InterviewSession.status == status)

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())

    if sort == "oldest":
        stmt = stmt.order_by(InterviewSession.started_at.asc())
    else:
        stmt = stmt.order_by(InterviewSession.started_at.desc())

    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    stmt = stmt.limit(page_size).offset((page - 1) * page_size)
    rows = list(db.execute(stmt).scalars().all())
    return PaginatedSessions(
        items=[_session_summary(db, s) for s in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


def _get_session_or_404(db: Session, session_id: str) -> InterviewSession:
    session = db.get(InterviewSession, session_id)
    if session is None:
        raise SessionNotFoundError(session_id)
    return session


def get_session_summary(db: Session, session_id: str) -> SessionSummaryOut:
    return _session_summary(db, _get_session_or_404(db, session_id))


def get_session_transcript(db: Session, session_id: str) -> list[TranscriptMessageOut]:
    _get_session_or_404(db, session_id)
    rows = list(
        db.execute(
            select(ConversationTurn)
            .where(ConversationTurn.session_id == session_id)
            .order_by(ConversationTurn.turn_index)
        ).scalars().all()
    )
    return [
        TranscriptMessageOut(
            id=t.id,
            session_id=t.session_id,
            speaker=t.role,
            content=t.content,
            source=t.source,
            turn_index=t.turn_index,
            created_at=t.created_at,
        )
        for t in rows
    ]


# ------------------------------------------------------------------ audit log
def list_audit_logs(db: Session, *, page: int = 1, page_size: int = 25) -> PaginatedAuditLogs:
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    rows, total = AuditRepository(db).list(limit=page_size, offset=(page - 1) * page_size)
    return PaginatedAuditLogs(
        items=[AuditLogOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


# ------------------------------------------------------------------ mutations
def set_student_status(db: Session, admin: User, student_id: str, is_active: bool) -> Student:
    student = _get_student_or_404(db, student_id)
    # Guard: an admin cannot deactivate the account they are logged in with.
    if student.user is not None and student.user.id == admin.id:
        raise SelfDeletionError()

    student.is_active = is_active
    if student.user is not None:
        student.user.is_active = is_active
    action = constants.AUDIT_STUDENT_REACTIVATED if is_active else constants.AUDIT_STUDENT_ARCHIVED
    verb = "reactivated" if is_active else "archived"
    _audit(
        db, admin,
        action=action, record_type="student", record_id=student.id,
        description=f"Student '{student.name}' {verb}.",
    )
    db.commit()
    logger.info("student_status_changed student_id=%s is_active=%s by=%s", student.id, is_active, admin.id)
    return student


def _delete_assessment_runs_for_session(db: Session, session_id: str) -> None:
    runs = list(
        db.execute(
            select(AssessmentRun).where(AssessmentRun.session_id == session_id)
        ).scalars().all()
    )
    for run in runs:
        db.delete(run)  # cascades to domain_results and evidence via ORM relationships
    db.flush()


def delete_session(db: Session, admin: User, session_id: str, *, archived_note: bool = False) -> None:
    session = _get_session_or_404(db, session_id)
    # Remove assessments (and their evidence, which references turns) BEFORE the
    # turns so no foreign key is ever left dangling.
    _delete_assessment_runs_for_session(db, session_id)
    db.delete(session)  # cascades to conversation_turns via ORM relationship
    _audit(
        db, admin,
        action=constants.AUDIT_SESSION_DELETED, record_type="session", record_id=session_id,
        description=f"Session '{session_id}' (case {session.case_id}) permanently deleted.",
    )
    db.commit()
    logger.info("session_deleted session_id=%s by=%s", session_id, admin.id)


def archive_session(db: Session, admin: User, session_id: str) -> InterviewSession:
    session = _get_session_or_404(db, session_id)
    session.status = _ARCHIVED
    session.locked = True
    _audit(
        db, admin,
        action=constants.AUDIT_SESSION_ARCHIVED, record_type="session", record_id=session_id,
        description=f"Session '{session_id}' (case {session.case_id}) archived.",
    )
    db.commit()
    logger.info("session_archived session_id=%s by=%s", session_id, admin.id)
    return session


def delete_assessment(db: Session, admin: User, assessment_id: str) -> None:
    run = db.get(AssessmentRun, assessment_id)
    if run is None:
        raise AssessmentNotFoundError(assessment_id)
    db.delete(run)  # cascades domain_results + evidence
    _audit(
        db, admin,
        action=constants.AUDIT_ASSESSMENT_DELETED, record_type="assessment", record_id=assessment_id,
        description=f"Assessment '{assessment_id}' deleted.",
    )
    db.commit()
    logger.info("assessment_deleted assessment_id=%s by=%s", assessment_id, admin.id)


def delete_message(db: Session, admin: User, message_id: str) -> None:
    turn = db.get(ConversationTurn, message_id)
    if turn is None:
        raise SessionNotFoundError(message_id)  # generic 404
    # Evidence rows reference this turn; remove them first to avoid FK orphans.
    ev = list(
        db.execute(
            select(AssessmentEvidence).where(AssessmentEvidence.turn_id == message_id)
        ).scalars().all()
    )
    for e in ev:
        db.delete(e)
    db.flush()
    db.delete(turn)
    _audit(
        db, admin,
        action=constants.AUDIT_MESSAGE_DELETED, record_type="message", record_id=message_id,
        description=f"Transcript message '{message_id}' deleted from session '{turn.session_id}'.",
    )
    db.commit()
    logger.info("message_deleted message_id=%s by=%s", message_id, admin.id)


def delete_student(db: Session, admin: User, student_id: str, *, confirm: str) -> None:
    if (confirm or "").strip().upper() != "DELETE":
        raise DeleteConfirmationError()
    student = _get_student_or_404(db, student_id)
    if student.user is not None and student.user.id == admin.id:
        raise SelfDeletionError()

    session_ids = _student_session_ids(db, student.id)
    # 1. Assessments + evidence + domains for every session.
    for sid in session_ids:
        _delete_assessment_runs_for_session(db, sid)
    # 2. Sessions (cascades their conversation turns).
    for sid in session_ids:
        s = db.get(InterviewSession, sid)
        if s is not None:
            db.delete(s)
    db.flush()
    # 3. Login account, then the student profile itself.
    if student.user is not None:
        db.delete(student.user)
    name = student.name
    db.delete(student)

    _audit(
        db, admin,
        action=constants.AUDIT_STUDENT_DELETED, record_type="student", record_id=student_id,
        description=(
            f"Student '{name}' permanently deleted with {len(session_ids)} session(s) "
            "and all connected transcripts and assessments."
        ),
    )
    db.commit()
    logger.info("student_deleted student_id=%s sessions=%d by=%s", student_id, len(session_ids), admin.id)
