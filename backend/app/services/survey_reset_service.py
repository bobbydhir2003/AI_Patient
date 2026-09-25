"""Admin "Survey Resets": survey stage state for every REAL student, plus a bulk
reset across them.

No second reset system: every reset (single or bulk) goes through
student_data_service.apply_survey_reset, and every state/eligibility decision
through student_data_service.survey_state_from, so this page and Student Data
always agree. Resets never delete REDCap history: pre/post rotate the owner
receipt to a new REDCap record_id, "both" releases the package, and every reset
first writes a SurveyReceiptReset snapshot + AuditLog row.

Eligible students = ``Student.is_practice IS FALSE`` (the same real-student rule
as Student Data / dashboard): admin/professor practice profiles are never listed
or bulk-reset. Archived (inactive) real students are included, as in Student
Data's default "All statuses" view.

Query budget (independent of roster size): 4 queries for the roster state
(students, account emails, receipts, grouped reset stats) + 4 grouped
last-activity lookups for the displayed page only. Summary counts need every
student's state, so the (small, column-light) state is computed in memory and the
filtered list paginated from it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import constants
from app.core.exceptions import StudentNotFoundError, SurveyResetNotApplicableError
from app.core.logging import get_logger
from app.models import Student, SurveyReceipt, SurveyReceiptReset, User
from app.models.survey_receipt import (
    SURVEY_RESET_BOTH,
    SURVEY_RESET_POST,
    SURVEY_RESET_PRE,
    SURVEY_SYNC_FAILED,
)
from app.repositories.audit_repository import AuditRepository
from app.schemas.student_data import SurveyStageOut, SurveyStateOut
from app.schemas.survey_resets import (
    BulkSurveyResetOut,
    SurveyResetCaseOption,
    SurveyResetListOut,
    SurveyResetStudentRow,
    SurveyResetSummary,
)
from app.services import student_data_service as sds

logger = get_logger(__name__)

_REAL_STUDENT = Student.is_practice.is_(False)

STATUS_FILTERS = ("all", "completed", "in_progress", "not_started", "reset_before")
NO_CASE = "none"


@dataclass
class _Entry:
    student: Student
    email: str
    state: SurveyStateOut
    pre_status: str
    post_status: str


def _stage_status(stage: SurveyStageOut, *, has_package: bool) -> str:
    if stage.completed:
        return "completed"
    if stage.sync_status == SURVEY_SYNC_FAILED:
        return "failed"
    return "pending" if has_package else "not_started"


def _load_entries(db: Session) -> list[_Entry]:
    students = list(db.execute(select(Student).where(_REAL_STUDENT)).scalars().all())
    emails = dict(
        db.execute(
            select(User.student_id, User.email)
            .join(Student, Student.id == User.student_id)
            .where(_REAL_STUDENT)
        ).all()
    )
    receipts: dict[str, list[SurveyReceipt]] = {}
    for r in db.execute(
        select(SurveyReceipt).join(Student, Student.id == SurveyReceipt.student_id).where(_REAL_STUDENT)
    ).scalars().all():
        receipts.setdefault(r.student_id, []).append(r)
    reset_stats = {
        sid: (int(n), last)
        for sid, n, last in db.execute(
            select(
                SurveyReceiptReset.student_id,
                func.count(SurveyReceiptReset.id),
                func.max(SurveyReceiptReset.reset_at),
            )
            .join(Student, Student.id == SurveyReceiptReset.student_id)
            .where(_REAL_STUDENT)
            .group_by(SurveyReceiptReset.student_id)
        ).all()
    }

    entries = []
    for s in students:
        n, last = reset_stats.get(s.id, (0, None))
        state = sds.survey_state_from(s, receipts.get(s.id, []), reset_count=n, last_reset_at=last)
        has_package = state.case_id is not None
        entries.append(
            _Entry(
                student=s,
                email=s.email or emails.get(s.id) or "",
                state=state,
                pre_status=_stage_status(state.pre, has_package=has_package),
                post_status=_stage_status(state.post, has_package=has_package),
            )
        )
    return entries


def _summary(entries: list[_Entry]) -> SurveyResetSummary:
    return SurveyResetSummary(
        total_students=len(entries),
        pre_completed=sum(1 for e in entries if e.state.pre.completed),
        post_completed=sum(1 for e in entries if e.state.post.completed),
        available_for_reset=sum(
            1 for e in entries
            if e.state.can_reset_pre or e.state.can_reset_post or e.state.can_reset_both
        ),
        pre_resettable=sum(1 for e in entries if e.state.can_reset_pre),
        post_resettable=sum(1 for e in entries if e.state.can_reset_post),
        both_resettable=sum(1 for e in entries if e.state.can_reset_both),
    )


def _matches_status(e: _Entry, status: str) -> bool:
    if status == "completed":
        return e.state.pre.completed and e.state.post.completed
    if status == "not_started":
        return not e.state.can_reset_both
    if status == "in_progress":
        return e.state.can_reset_both and not (e.state.pre.completed and e.state.post.completed)
    if status == "reset_before":
        return e.state.reset_count > 0
    return True


def list_survey_resets(
    db: Session,
    *,
    search: str = "",
    status: str = "all",
    case_id: str = "",
    page: int = 1,
    page_size: int = 10,
) -> SurveyResetListOut:
    entries = _load_entries(db)
    summary = _summary(entries)
    case_options = sorted(
        {
            (e.state.case_id, e.state.case_name or e.state.case_id)
            for e in entries
            if e.state.case_id
        },
        key=lambda c: c[1].lower(),
    )

    q = search.strip().lower()
    rows = [
        e for e in entries
        if (
            not q
            or q in e.student.name.lower()
            or q in e.email.lower()
            or q in (e.student.email or "").lower()
            or q in (e.student.student_number or "").lower()
        )
        and _matches_status(e, status)
        and (
            not case_id
            or (case_id == NO_CASE and e.state.case_id is None)
            or e.state.case_id == case_id
        )
    ]
    rows.sort(key=lambda e: (e.student.name.lower(), e.student.id))

    total = len(rows)
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    page_rows = rows[(page - 1) * page_size : page * page_size]
    activity = sds.last_activity_for(db, [e.student.id for e in page_rows])

    items = [
        SurveyResetStudentRow(
            student_id=e.student.id,
            name=e.student.name,
            email=e.email,
            student_number=e.student.student_number,
            is_active=e.student.is_active,
            survey_case_id=e.state.case_id,
            survey_case_name=e.state.case_name,
            pre_status=e.pre_status,
            post_status=e.post_status,
            pre_completed_at=e.state.pre.completed_at,
            post_completed_at=e.state.post.completed_at,
            can_reset_pre=e.state.can_reset_pre,
            can_reset_post=e.state.can_reset_post,
            can_reset_both=e.state.can_reset_both,
            reset_count=e.state.reset_count,
            last_reset_at=e.state.last_reset_at,
            last_activity_at=activity.get(e.student.id),
        )
        for e in page_rows
    ]
    return SurveyResetListOut(
        summary=summary,
        case_options=[SurveyResetCaseOption(id=cid, name=name) for cid, name in case_options],
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


def _applies(state: SurveyStateOut, scope: str) -> bool:
    if scope == SURVEY_RESET_PRE:
        return state.can_reset_pre
    if scope == SURVEY_RESET_POST:
        return state.can_reset_post
    return state.can_reset_both


def bulk_reset(db: Session, admin: User, *, scope: str) -> BulkSurveyResetOut:
    """Reset ``scope`` for every eligible REAL student it applies to.

    Each student goes through the exact single-student reset (its own row lock,
    snapshot, audit row and commit), so one failure never rolls back or blocks
    the others, and a partially completed run leaves every student in a valid
    state. Students the scope does not apply to are skipped, not errors."""
    if scope not in (SURVEY_RESET_PRE, SURVEY_RESET_POST, SURVEY_RESET_BOTH):
        raise SurveyResetNotApplicableError("Unknown survey reset scope.")
    entries = _load_entries(db)
    targets = [e.student.id for e in entries if _applies(e.state, scope)]
    eligible = len(entries)
    reset = failed = 0
    skipped = eligible - len(targets)
    for student_id in targets:
        try:
            sds.apply_survey_reset(db, admin, student_id, scope=scope)
            reset += 1
        except (SurveyResetNotApplicableError, StudentNotFoundError):
            # State changed since it was read (e.g. a concurrent reset/delete).
            db.rollback()
            skipped += 1
        except Exception:  # noqa: BLE001 - one bad row must not abort the rest
            db.rollback()
            failed += 1
            logger.exception("bulk_survey_reset_failed student_id=%s scope=%s", student_id, scope)

    scope_label = {"pre": "Pre survey", "post": "Post survey", "both": "Pre and Post surveys"}[scope]
    AuditRepository(db).record(
        admin_user_id=admin.id,
        admin_email=admin.email,
        action_type=constants.AUDIT_SURVEY_RESET,
        record_type="survey_reset_bulk",
        record_id=scope,
        description=(
            f"Bulk {scope_label} reset across real students: {reset} reset, "
            f"{skipped} skipped, {failed} failed (of {eligible} eligible)."
        ),
    )
    db.commit()
    logger.info(
        "bulk_survey_reset scope=%s eligible=%s reset=%s skipped=%s failed=%s by=%s",
        scope, eligible, reset, skipped, failed, admin.id,
    )
    noun = "student" if reset == 1 else "students"
    message = f"{scope_label} reset for {reset} {noun}."
    if skipped:
        message += f" {skipped} had nothing to reset."
    if failed:
        message += f" {failed} failed and were left unchanged."
    return BulkSurveyResetOut(
        scope=scope, eligible=eligible, reset=reset, skipped=skipped, failed=failed, message=message
    )
