"""Admin "Student Data": one student-centred view of identity, sessions,
assessments, assessment viewing (per visit), survey state and activity.

Query budget (independent of how many sessions/visits a student has):
  list   -> 1 count + 1 page query (sessions aggregated in a subquery) + 2 grouped
            last-activity lookups restricted to the page's student ids.
  detail -> student, sessions, grouped turn counts, runs, view summaries, visits,
            survey receipts, reset history: a fixed handful of queries, never one
            per session / run / visit / receipt.

Survey model (see survey_service): ONE global survey package per student. The
first case whose Pre succeeds becomes ``Student.survey_owner_case_id``; the
(student, case) ``survey_receipts`` row tracks each stage. Answers live ONLY in
REDCap under the receipt's ``redcap_record_id``; nothing local stores answers.

Reset semantics (reset_survey):
  pre / post -> the OWNER case's receipt: that stage goes back to pending, the
                package back to in_progress, and the receipt gets a NEW REDCap
                record_id so the resubmission can never overwrite the earlier
                answers. Ownership is kept, so the student retakes it in that case.
  both       -> every receipt of the student is removed and ownership released, so
                the next Pre (in any case) starts a brand-new package on a new
                record_id.
  Every reset first writes a SurveyReceiptReset snapshot (old record_id, stage
  statuses and timestamps) plus an AuditLog row, in the same transaction.
  Sessions, transcripts, assessments and assessment-view history are never read
  for writing, let alone modified.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.core import constants
from app.core.exceptions import StudentNotFoundError, SurveyResetNotApplicableError
from app.core.logging import get_logger
from app.models import (
    AssessmentRun,
    AssessmentViewVisit,
    ConversationTurn,
    InterviewSession,
    Student,
    SurveyReceipt,
    SurveyReceiptReset,
    User,
)
from app.models.assessment_view import VISIT_SOURCE_DASHBOARD, VISIT_SOURCE_INITIAL, VISIT_SOURCE_LEGACY
from app.models.survey_receipt import (
    SURVEY_OVERALL_COMPLETED,
    SURVEY_OVERALL_IN_PROGRESS,
    SURVEY_OVERALL_NOT_STARTED,
    SURVEY_RESET_BOTH,
    SURVEY_RESET_POST,
    SURVEY_RESET_PRE,
    SURVEY_SYNC_DONE,
    SURVEY_SYNC_PENDING,
)
from app.repositories.audit_repository import AuditRepository
from app.schemas.student_data import (
    AssessmentVisitOut,
    PaginatedStudentData,
    StudentDataDetailOut,
    StudentDataListItem,
    StudentDataProfile,
    StudentDataSessionRow,
    StudentDataSummary,
    SurveyResetOut,
    SurveyStageOut,
    SurveyStateOut,
    TimelineEventOut,
)
from app.services import assessment_view_service

logger = get_logger(__name__)

_COMPLETED = constants.SESSION_STATUS_COMPLETED
_ACTIVE = constants.SESSION_STATUS_ACTIVE
_ARCHIVED = constants.SESSION_STATUS_ARCHIVED
# Same practice-data exclusion as the rest of the admin area.
_REAL_SESSION = InterviewSession.is_practice.is_(False)
_REAL_STUDENT = Student.is_practice.is_(False)

TIMELINE_LIMIT = 60

_SOURCE_LABELS = {
    VISIT_SOURCE_INITIAL: "Initial after interview",
    VISIT_SOURCE_DASHBOARD: "Student dashboard",
    VISIT_SOURCE_LEGACY: "Historical (before visit tracking)",
}


def _utc(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; normalise so max()/sorting never mixes
    naive and aware values."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _latest(*values: datetime | None) -> datetime | None:
    present = [_utc(v) for v in values if v is not None]
    return max(present) if present else None


def _case_name(case_id: str | None) -> str | None:
    if not case_id:
        return None
    from app.services.survey_service import _case_name as survey_case_name

    return survey_case_name(case_id)


def _get_student_or_404(db: Session, student_id: str) -> Student:
    student = db.get(Student, student_id)
    if student is None:
        raise StudentNotFoundError(student_id)
    return student


# ------------------------------------------------------------------ list
def list_students(
    db: Session,
    *,
    search: str = "",
    status: str = "all",  # all | active | inactive
    sort: str = "name",  # name | recent
    page: int = 1,
    page_size: int = 15,
) -> PaginatedStudentData:
    agg = (
        select(
            InterviewSession.student_id.label("sid"),
            func.count(InterviewSession.id).label("n"),
            func.sum(case((InterviewSession.status == _COMPLETED, 1), else_=0)).label("done"),
            func.sum(case((InterviewSession.status == _ACTIVE, 1), else_=0)).label("open"),
            func.max(InterviewSession.started_at).label("last_start"),
            func.max(InterviewSession.completed_at).label("last_done"),
        )
        .where(_REAL_SESSION)
        .group_by(InterviewSession.student_id)
        .subquery()
    )
    stmt = (
        select(Student, User.email, User.last_login_at, agg)
        .outerjoin(User, User.student_id == Student.id)
        .outerjoin(agg, agg.c.sid == Student.id)
        .where(_REAL_STUDENT)
    )
    q = search.strip().lower()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                func.lower(Student.name).like(like),
                func.lower(Student.email).like(like),
                func.lower(Student.student_number).like(like),
                func.lower(User.email).like(like),
            )
        )
    if status == "active":
        stmt = stmt.where(Student.is_active.is_(True))
    elif status == "inactive":
        stmt = stmt.where(Student.is_active.is_(False))

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())

    if sort == "recent":
        stmt = stmt.order_by(
            func.coalesce(agg.c.last_start, Student.created_at).desc(), Student.id
        )
    else:
        stmt = stmt.order_by(func.lower(Student.name).asc(), Student.id)

    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    rows = db.execute(stmt.limit(page_size).offset((page - 1) * page_size)).all()
    ids = [r.Student.id for r in rows]

    # Two grouped lookups for the page only (never per student).
    view_last: dict[str, datetime] = {}
    survey_last: dict[str, datetime | None] = {}
    if ids:
        for sid, last in db.execute(
            select(AssessmentViewVisit.student_id, func.max(AssessmentViewVisit.last_heartbeat_at))
            .where(AssessmentViewVisit.student_id.in_(ids))
            .group_by(AssessmentViewVisit.student_id)
        ).all():
            view_last[sid] = last
        for sid, pre, post in db.execute(
            select(
                SurveyReceipt.student_id,
                func.max(SurveyReceipt.pre_synced_at),
                func.max(SurveyReceipt.post_synced_at),
            )
            .where(SurveyReceipt.student_id.in_(ids))
            .group_by(SurveyReceipt.student_id)
        ).all():
            survey_last[sid] = _latest(pre, post)

    items = []
    for r in rows:
        s: Student = r.Student
        items.append(
            StudentDataListItem(
                id=s.id,
                name=s.name,
                email=s.email or (r.email or ""),
                student_number=s.student_number,
                is_active=s.is_active,
                has_account=r.email is not None,
                session_count=int(r.n or 0),
                completed_count=int(r.done or 0),
                incomplete_count=int(r.open or 0),
                last_activity_at=_latest(
                    r.last_login_at, r.last_start, r.last_done,
                    view_last.get(s.id), survey_last.get(s.id),
                ),
            )
        )
    return PaginatedStudentData(items=items, total=total, page=page, page_size=page_size)


# ------------------------------------------------------------------ survey state
def _stage(status: str | None, at: datetime | None) -> SurveyStageOut:
    done = status in SURVEY_SYNC_DONE
    return SurveyStageOut(completed=done, sync_status=status, completed_at=at if done else None)


def _primary_receipt(student: Student, receipts: list[SurveyReceipt]) -> SurveyReceipt | None:
    """The receipt whose stages represent the student's survey: the owner case's
    when ownership is set, otherwise the most recently touched one (pre-global
    legacy data or a released failed first attempt)."""
    if not receipts:
        return None
    if student.survey_owner_case_id:
        for r in receipts:
            if r.case_id == student.survey_owner_case_id:
                return r
    return max(receipts, key=lambda r: _utc(r.updated_at) or datetime.min.replace(tzinfo=timezone.utc))


def _survey_state(
    student: Student, receipts: list[SurveyReceipt], resets: list[SurveyReceiptReset]
) -> SurveyStateOut:
    owner = student.survey_owner_case_id
    if owner is None:
        global_status = SURVEY_OVERALL_NOT_STARTED
    elif student.survey_completed_at is not None:
        global_status = SURVEY_OVERALL_COMPLETED
    else:
        global_status = SURVEY_OVERALL_IN_PROGRESS
    primary = _primary_receipt(student, receipts)
    pre = _stage(primary.pre_sync_status, primary.pre_synced_at) if primary else _stage(None, None)
    post = _stage(primary.post_sync_status, primary.post_synced_at) if primary else _stage(None, None)
    owner_receipt = primary if (primary is not None and primary.case_id == owner) else None
    return SurveyStateOut(
        global_status=global_status,
        owner_case_id=owner,
        owner_case_name=_case_name(owner),
        case_id=primary.case_id if primary else None,
        case_name=_case_name(primary.case_id) if primary else None,
        completed_at=student.survey_completed_at,
        pre=pre,
        post=post,
        last_response_at=_latest(
            *(r.pre_synced_at for r in receipts), *(r.post_synced_at for r in receipts)
        ),
        # Stage resets act on the OWNER package only (the one that gates surveys).
        can_reset_pre=owner_receipt is not None and pre.completed,
        can_reset_post=owner_receipt is not None and post.completed,
        can_reset_both=owner is not None or bool(receipts),
        reset_count=len(resets),
        last_reset_at=_latest(*(r.reset_at for r in resets)),
    )


def _load_survey(db: Session, student_id: str) -> tuple[list[SurveyReceipt], list[SurveyReceiptReset]]:
    receipts = list(
        db.execute(select(SurveyReceipt).where(SurveyReceipt.student_id == student_id)).scalars().all()
    )
    resets = list(
        db.execute(
            select(SurveyReceiptReset)
            .where(SurveyReceiptReset.student_id == student_id)
            .order_by(SurveyReceiptReset.reset_at.desc())
        ).scalars().all()
    )
    return receipts, resets


def _session_survey_status(
    case_id: str, owner: str | None, by_case: dict[str, SurveyReceipt]
) -> str:
    r = by_case.get(case_id)
    if r is not None:
        pre_done = r.pre_sync_status in SURVEY_SYNC_DONE
        post_done = r.post_sync_status in SURVEY_SYNC_DONE
        if pre_done and post_done:
            return "completed"
        if pre_done:
            return "pre_completed"
        return "not_completed"
    if owner is not None and owner != case_id:
        return "other_case"
    return "not_completed"


# ------------------------------------------------------------------ detail
def get_student_data(db: Session, student_id: str) -> StudentDataDetailOut:
    student = _get_student_or_404(db, student_id)
    user: User | None = student.user

    sessions = list(
        db.execute(
            select(InterviewSession)
            .where(InterviewSession.student_id == student.id, _REAL_SESSION)
            .order_by(InterviewSession.started_at.desc())
        ).scalars().all()
    )
    ids = [s.id for s in sessions]

    turn_counts: dict[str, tuple[int, int]] = {}
    runs_by_session: dict[str, list[AssessmentRun]] = {}
    if ids:
        for sid, total, student_turns in db.execute(
            select(
                ConversationTurn.session_id,
                func.count(ConversationTurn.id),
                func.sum(case((ConversationTurn.role == "student", 1), else_=0)),
            )
            .where(ConversationTurn.session_id.in_(ids))
            .group_by(ConversationTurn.session_id)
        ).all():
            turn_counts[sid] = (int(total or 0), int(student_turns or 0))
        for run in db.execute(
            select(AssessmentRun)
            .where(AssessmentRun.session_id.in_(ids))
            .order_by(AssessmentRun.created_at.asc())
        ).scalars().all():
            runs_by_session.setdefault(run.session_id, []).append(run)
    view_map = assessment_view_service.map_for_sessions(db, ids)
    visit_map = assessment_view_service.visits_for_sessions(db, ids)
    receipts, resets = _load_survey(db, student.id)
    by_case = {r.case_id: r for r in receipts}
    owner = student.survey_owner_case_id

    rows: list[StudentDataSessionRow] = []
    timeline: list[TimelineEventOut] = []
    total_interview = 0
    completed_durations: list[int] = []
    for s in sessions:
        case_name = _case_name(s.case_id) or s.case_id
        duration = (
            int((_utc(s.completed_at) - _utc(s.started_at)).total_seconds())
            if s.completed_at is not None
            else None
        )
        if duration is not None:
            total_interview += duration
            if s.status == _COMPLETED:
                completed_durations.append(duration)
        total_turns, questions = turn_counts.get(s.id, (0, 0))
        runs = runs_by_session.get(s.id, [])
        latest = runs[-1] if runs else None
        view = view_map.get(s.id)
        visits = visit_map.get(s.id, [])
        rows.append(
            StudentDataSessionRow(
                session_id=s.id,
                case_id=s.case_id,
                case_category=s.case_category,
                status=s.status,
                started_at=s.started_at,
                completed_at=s.completed_at,
                duration_seconds=duration,
                turn_count=total_turns,
                student_question_count=questions,
                has_assessment=latest is not None,
                assessment_id=latest.id if latest else None,
                assessment_status=latest.status if latest else None,
                overall_level=latest.overall_level if latest else None,
                assessment_completed_at=latest.completed_at if latest else None,
                active_viewing_seconds=view.active_seconds if view else None,
                view_count=view.view_count if view else None,
                first_viewed_at=view.first_viewed_at if view else None,
                last_viewed_at=view.last_viewed_at if view else None,
                survey_status=_session_survey_status(s.case_id, owner, by_case),
                visits=[
                    AssessmentVisitOut(
                        visit_number=i,
                        source=v.source,
                        started_at=v.started_at,
                        last_heartbeat_at=v.last_heartbeat_at,
                        active_seconds=v.active_seconds,
                        assessment_deleted=v.assessment_run_id is None,
                    )
                    for i, v in enumerate(visits, start=1)
                ],
            )
        )

        # ---- timeline (derived from stored timestamps only) ----
        timeline.append(TimelineEventOut(
            kind="interview_started", at=s.started_at, title="Interview started",
            detail=f"Case: {case_name}", session_id=s.id,
        ))
        if s.completed_at is not None:
            timeline.append(TimelineEventOut(
                kind="interview_completed", at=s.completed_at, title="Interview completed",
                detail=f"{case_name} — {questions} question{'s' if questions != 1 else ''}",
                session_id=s.id,
            ))
        for run in runs:
            if run.completed_at is not None and run.overall_level:
                timeline.append(TimelineEventOut(
                    kind="assessment_generated", at=run.completed_at,
                    title="AI assessment generated",
                    detail=f"{case_name} — {run.overall_level}", session_id=s.id,
                ))
        for i, v in enumerate(visits, start=1):
            timeline.append(TimelineEventOut(
                kind="assessment_viewed", at=v.started_at, title="Assessment viewed",
                detail=f"{case_name} — Visit {i} ({_SOURCE_LABELS.get(v.source, 'Unknown source')})",
                session_id=s.id,
            ))

    for r in receipts:
        name = _case_name(r.case_id) or r.case_id
        if r.pre_synced_at is not None and r.pre_sync_status in SURVEY_SYNC_DONE:
            timeline.append(TimelineEventOut(
                kind="pre_survey_completed", at=r.pre_synced_at,
                title="Pre survey completed", detail=name,
            ))
        if r.post_synced_at is not None and r.post_sync_status in SURVEY_SYNC_DONE:
            timeline.append(TimelineEventOut(
                kind="post_survey_completed", at=r.post_synced_at,
                title="Post survey completed", detail=name,
            ))
    for x in resets:
        scope = {"pre": "Pre survey", "post": "Post survey"}.get(x.scope, "Both surveys")
        timeline.append(TimelineEventOut(
            kind="survey_reset", at=x.reset_at, title="Survey reset by admin",
            detail=f"{scope} — {_case_name(x.case_id) or x.case_id}"
            + (f" (by {x.reset_by_email})" if x.reset_by_email else ""),
        ))
    timeline.sort(key=lambda e: _utc(e.at), reverse=True)

    total_view = sum(v.active_seconds for v in view_map.values())
    total_visits = sum(v.view_count for v in view_map.values())
    completed = sum(1 for s in sessions if s.status == _COMPLETED)
    summary = StudentDataSummary(
        total_sessions=len(sessions),
        completed_sessions=completed,
        incomplete_sessions=sum(1 for s in sessions if s.status == _ACTIVE),
        archived_sessions=sum(1 for s in sessions if s.status == _ARCHIVED),
        completed_without_assessment=sum(
            1 for s in sessions if s.status == _COMPLETED and s.id not in runs_by_session
        ),
        total_interview_seconds=total_interview,
        average_interview_seconds=(
            round(sum(completed_durations) / len(completed_durations)) if completed_durations else None
        ),
        total_student_questions=sum(q for _t, q in turn_counts.values()),
        total_assessments=sum(len(v) for v in runs_by_session.values()),
        assessed_sessions=len(runs_by_session),
        total_view_seconds=total_view,
        total_visits=total_visits,
        viewed_sessions=len(view_map),
    )

    last_activity = _latest(
        user.last_login_at if user else None,
        *(s.started_at for s in sessions),
        *(s.completed_at for s in sessions),
        *(v.last_heartbeat_at for vs in visit_map.values() for v in vs),
        *(r.pre_synced_at for r in receipts),
        *(r.post_synced_at for r in receipts),
    )
    profile = StudentDataProfile(
        id=student.id,
        name=student.name,
        email=student.email or (user.email if user else ""),
        student_number=student.student_number,
        is_active=student.is_active,
        has_account=user is not None,
        role=user.role if user else None,
        created_at=student.created_at,
        last_login_at=user.last_login_at if user else None,
        last_activity_at=last_activity,
    )
    return StudentDataDetailOut(
        student=profile,
        summary=summary,
        survey=_survey_state(student, receipts, resets),
        sessions=rows,
        timeline=timeline[:TIMELINE_LIMIT],
    )


# ------------------------------------------------------------------ survey reset
def _snapshot(
    receipt: SurveyReceipt, *, scope: str, owner: str | None, admin: User, now: datetime
) -> SurveyReceiptReset:
    return SurveyReceiptReset(
        student_id=receipt.student_id,
        case_id=receipt.case_id,
        scope=scope,
        redcap_record_id=receipt.redcap_record_id,
        pre_sync_status=receipt.pre_sync_status,
        pre_synced_at=receipt.pre_synced_at,
        post_sync_status=receipt.post_sync_status,
        post_synced_at=receipt.post_synced_at,
        overall_status=receipt.overall_status,
        was_owner_case=receipt.case_id == owner,
        latest_session_id=receipt.latest_session_id,
        receipt_created_at=receipt.created_at,
        reset_by_user_id=admin.id,
        reset_by_email=admin.email,
        reset_at=now,
    )


def reset_survey(db: Session, admin: User, student_id: str, *, scope: str) -> SurveyResetOut:
    """Make the student eligible to take the survey again (see module docstring).
    ``student_id`` comes from the route only; one transaction; audited."""
    if scope not in (SURVEY_RESET_PRE, SURVEY_RESET_POST, SURVEY_RESET_BOTH):
        raise SurveyResetNotApplicableError("Unknown survey reset scope.")
    # Same row lock survey_service takes to claim ownership, so a reset can never
    # interleave with a concurrent survey submission for this student.
    student = db.execute(
        select(Student).where(Student.id == student_id).with_for_update()
    ).scalar_one_or_none()
    if student is None:
        raise StudentNotFoundError(student_id)
    receipts = list(
        db.execute(select(SurveyReceipt).where(SurveyReceipt.student_id == student.id)).scalars().all()
    )
    owner = student.survey_owner_case_id
    now = datetime.now(timezone.utc)

    if scope == SURVEY_RESET_BOTH:
        if owner is None and not receipts:
            raise SurveyResetNotApplicableError("This student has no survey to reset.")
        for r in receipts:
            db.add(_snapshot(r, scope=scope, owner=owner, admin=admin, now=now))
            db.delete(r)
        student.survey_owner_case_id = None
        student.survey_completed_at = None
        case_for_log = owner or (receipts[0].case_id if receipts else "")
        old_ids = ", ".join(r.redcap_record_id for r in receipts) or "none"
    else:
        target = next((r for r in receipts if owner and r.case_id == owner), None)
        stage_status = (
            None if target is None
            else target.pre_sync_status if scope == SURVEY_RESET_PRE
            else target.post_sync_status
        )
        if target is None or stage_status not in SURVEY_SYNC_DONE:
            label = "Pre" if scope == SURVEY_RESET_PRE else "Post"
            raise SurveyResetNotApplicableError(
                f"This student has no completed {label} survey to reset."
            )
        db.add(_snapshot(target, scope=scope, owner=owner, admin=admin, now=now))
        old_ids = target.redcap_record_id
        # New REDCap record for the retake: the earlier answers stay untouched
        # under the old record_id (kept in the snapshot above).
        target.redcap_record_id = uuid.uuid4().hex
        if scope == SURVEY_RESET_PRE:
            target.pre_sync_status = SURVEY_SYNC_PENDING
            target.pre_synced_at = None
        else:
            target.post_sync_status = SURVEY_SYNC_PENDING
            target.post_synced_at = None
        target.overall_status = SURVEY_OVERALL_IN_PROGRESS
        student.survey_completed_at = None
        case_for_log = target.case_id

    scope_label = {"pre": "Pre survey", "post": "Post survey", "both": "Pre and Post surveys"}[scope]
    AuditRepository(db).record(
        admin_user_id=admin.id,
        admin_email=admin.email,
        action_type=constants.AUDIT_SURVEY_RESET,
        record_type="student",
        record_id=student.id,
        description=(
            f"{scope_label} reset for student '{student.name}' "
            f"(case {case_for_log or '—'}; previous REDCap record(s): {old_ids})."
        ),
    )
    db.commit()
    logger.info(
        "survey_reset student_id=%s scope=%s case=%s by=%s",
        student.id, scope, case_for_log, admin.id,
    )
    receipts, resets = _load_survey(db, student.id)
    db.refresh(student)
    messages = {
        "pre": "Pre survey reset. The student can complete it again.",
        "post": "Post survey reset. The student can complete it again.",
        "both": "Both surveys reset. The student can complete the survey again.",
    }
    return SurveyResetOut(
        success=True, message=messages[scope], survey=_survey_state(student, receipts, resets)
    )
