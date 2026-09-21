"""Global Pre/Post experience survey service.

Business rule (updated): a student completes ONE survey package GLOBALLY, not one
per case. The FIRST case whose Pre stage successfully reaches REDCap (or is
skipped when REDCap is unconfigured) becomes that student's permanent SURVEY
OWNER CASE (``Student.survey_owner_case_id``). Pre and Post are the two STAGES of
that single owning package. Every OTHER case is gated to "already submitted /
skip" and can never establish a second package.

Global survey state per student:
  - NOT_STARTED : ``survey_owner_case_id`` is NULL (no Pre has succeeded yet).
  - IN_PROGRESS : an owner exists but ``survey_completed_at`` is NULL (Pre done,
                  Post pending for the owning case).
  - COMPLETED   : the owning case's Post completed (``survey_completed_at`` set).

The per-(student, case) ``survey_receipts`` row (UNIQUE(student_id, case_id))
remains the authoritative lifecycle/linkage record; the ``Student`` columns are a
denormalized owner pointer used for O(1) gating and as the row locked
(SELECT ... FOR UPDATE) to serialise the ownership claim so two cases can never
both win. Ownership is claimed BEFORE the REDCap call and RELEASED again if that
first Pre fails, so a failed attempt never permanently locks the student.

Responsibilities:
- Resolve BOTH the student and the case SERVER-SIDE from the authenticated,
  ownership-checked session (require_session_access). The frontend never
  supplies the NUID, student identity, or case identity - it only sends answers.
- The REDCap primary ``record_id`` is a generated random UUID (uuid4().hex),
  created ONCE per (student, case) package and reused for Pre, Post, retries and
  any later session for the same case. It is NOT the NUID/email/session id.
- The student's NUID (Student.student_number) is sent SEPARATELY in the ``nuid``
  field, and the case number in the ``case_id`` field (see constants.REDCAP_CASE_ID),
  both resolved server-side. NUID stays required research identity: refuse
  (NuidMissingError) rather than send a blank nuid.
- Enforce the case-level lifecycle: create the ONE receipt on first Pre (or
  Post), advance it Pre -> IN_PROGRESS, Post -> COMPLETED, and reject any further
  submission once COMPLETED (SurveyAlreadyCompletedError -> 409).
- Route each import to the correct per-case REDCap instance so the 4 cases,
  which reuse the same variable names on the same record_id, never overwrite one
  another (see _case_instance_fields + docs/REDCAP.md).
- Store NO survey answers locally: only sync/lifecycle metadata (the answers
  live exclusively in REDCap).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import REDCAP_CASE_ID
from app.core.exceptions import (
    NuidMissingError,
    RedcapCaseUnsupportedError,
    SurveyAlreadyCompletedError,
    SurveyOwnedByOtherCaseError,
    SurveyPreRequiredError,
    SurveySyncError,
)
from app.core.logging import get_logger
from app.models import InterviewSession, Student, SurveyReceipt
from app.models.survey_receipt import (
    SURVEY_OVERALL_COMPLETED,
    SURVEY_OVERALL_IN_PROGRESS,
    SURVEY_OVERALL_NOT_STARTED,
    SURVEY_PHASE_POST,
    SURVEY_PHASE_PRE,
    SURVEY_SYNC_DONE,
    SURVEY_SYNC_FAILED,
    SURVEY_SYNC_PENDING,
    SURVEY_SYNC_SKIPPED,
    SURVEY_SYNC_SYNCED,
)
from app.patient_engine import case_loader
from app.schemas.survey_schema import (
    PostSurveyIn,
    PreSurveyIn,
    SurveyStatusOut,
    SurveySubmitResult,
)
from app.services import redcap_client
from app.services.redcap_client import REDCAP_COMPLETE, RedcapError, RedcapNotConfiguredError

logger = get_logger(__name__)

_INSTRUMENT_BY_PHASE = {
    SURVEY_PHASE_PRE: "pre_experience_survey",
    SURVEY_PHASE_POST: "post_experience_survey",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_record_id() -> str:
    """A fresh, collision-resistant REDCap primary record_id. Generated ONCE per
    (student, case) package (in _get_or_create_receipt) and then reused; matches
    the codebase UUID convention (uuid4().hex, 32 chars)."""
    return uuid.uuid4().hex


def _resolve_nuid(session: InterviewSession) -> str:
    """NUID for this encounter's student, resolved server-side. Sent to REDCap in
    the separate ``nuid`` field (never the primary record_id). Raises if blank."""
    nuid = (getattr(session.student, "student_number", "") or "").strip()
    if not nuid:
        raise NuidMissingError()
    return nuid


def _redcap_case_number(case_id: str) -> int | None:
    """The REDCap ``case_id`` number for this case slug, or None if the case has
    no survey mapping. Soft lookup for status/display (never raises)."""
    return REDCAP_CASE_ID.get(case_id)


def _resolve_case_id(case_id: str) -> int:
    """The REDCap ``case_id`` number for a submission, resolved server-side from
    the session's case. Fails loudly for an unmapped case rather than sending a
    wrong/blank value."""
    number = _redcap_case_number(case_id)
    if number is None:
        raise RedcapCaseUnsupportedError(case_id)
    return number


def _case_name(case_id: str) -> str:
    """Human-facing case name (e.g. "Carly") for the already-completed message.
    Falls back to the raw case_id if the case cannot be loaded."""
    try:
        return case_loader.load_case(case_id).display_name
    except Exception:
        return case_id


def _get_receipt(db: Session, student_id: str, case_id: str) -> SurveyReceipt | None:
    return db.execute(
        select(SurveyReceipt).where(
            SurveyReceipt.student_id == student_id,
            SurveyReceipt.case_id == case_id,
        )
    ).scalar_one_or_none()


def _get_or_create_receipt(
    db: Session, session: InterviewSession
) -> SurveyReceipt:
    """Return the ONE (student, case) receipt, creating it if absent.

    On creation, a fresh random REDCap ``record_id`` (uuid4().hex) is generated
    exactly ONCE and persisted; every later Pre/Post/retry/session for this
    (student, case) reuses that stored id (this method returns the existing row
    unchanged when it is already present, so the id is never regenerated).

    Concurrency-safe: the create is attempted inside a SAVEPOINT so that if a
    concurrent request (or a double-click) already inserted the row, the
    UNIQUE(student_id, case_id) violation rolls back only the savepoint (never
    the outer transaction) and we re-read the winner. This is what makes a
    second interview session, a double-click, and two concurrent requests all
    converge on the SAME receipt (and the SAME record_id) instead of creating
    duplicates.
    """
    receipt = _get_receipt(db, session.student_id, session.case_id)
    if receipt is not None:
        # Keep a breadcrumb of the most recent session to touch the package.
        receipt.latest_session_id = session.id
        return receipt

    receipt = SurveyReceipt(
        student_id=session.student_id,
        case_id=session.case_id,
        latest_session_id=session.id,
        redcap_record_id=_new_record_id(),
        pre_sync_status=SURVEY_SYNC_PENDING,
        post_sync_status=SURVEY_SYNC_PENDING,
        overall_status=SURVEY_OVERALL_IN_PROGRESS,
    )
    try:
        with db.begin_nested():
            db.add(receipt)
    except IntegrityError:
        # A concurrent insert won the race; adopt the existing row.
        existing = _get_receipt(db, session.student_id, session.case_id)
        if existing is None:  # pragma: no cover - defensive; unique-violation implies a row
            raise
        existing.latest_session_id = session.id
        return existing
    return receipt


# ---------------------------------------------------------------------------
# Global (one-package-per-student) ownership.
# ---------------------------------------------------------------------------
def _global_survey_status(student: Student) -> str:
    """The student's global survey lifecycle, derived from the denormalized owner
    pointer: NOT_STARTED (no owner) / COMPLETED (owner + completed_at) /
    IN_PROGRESS (owner, not yet completed)."""
    if student.survey_owner_case_id is None:
        return SURVEY_OVERALL_NOT_STARTED
    if student.survey_completed_at is not None:
        return SURVEY_OVERALL_COMPLETED
    return SURVEY_OVERALL_IN_PROGRESS


def _claim_ownership(db: Session, session: InterviewSession) -> str:
    """Atomically claim the student's single global survey-owner slot for this
    session's case, BEFORE any REDCap call.

    Serialised by a row lock on the Student row (SELECT ... FOR UPDATE): two
    concurrent first-Pre submissions for different cases can never both win - one
    sets the owner, the other observes it and is rejected. Returns:
      - "owner"           : this case owns the package now (receipt ensured).
      - "owned_elsewhere" : a different case already owns it -> caller rejects.

    Commits so the lock is released (and the claim persisted) BEFORE the network
    REDCap import - we never hold a DB transaction open across external I/O.
    """
    student = db.execute(
        select(Student).where(Student.id == session.student_id).with_for_update()
    ).scalar_one()
    owner = student.survey_owner_case_id
    if owner is not None and owner != session.case_id:
        db.commit()  # release the lock; nothing changed
        return "owned_elsewhere"
    if owner is None:
        student.survey_owner_case_id = session.case_id
    # Ensure the (student, case) receipt exists within the same critical section.
    _get_or_create_receipt(db, session)
    db.commit()
    return "owner"


def _release_ownership_if_unestablished(db: Session, session: InterviewSession) -> None:
    """Undo an ownership claim when this case's Pre FAILED to establish the
    package, so a failed FIRST attempt never permanently locks the student to a
    case (retry can then re-claim, same or different case).

    Safe/conservative: releases ONLY if this case still holds the slot, the
    package is not completed, and Pre never actually reached REDCap. If Pre did
    succeed (established), ownership is kept."""
    student = db.execute(
        select(Student).where(Student.id == session.student_id).with_for_update()
    ).scalar_one()
    if (
        student.survey_owner_case_id != session.case_id
        or student.survey_completed_at is not None
    ):
        db.commit()
        return
    receipt = _get_receipt(db, session.student_id, session.case_id)
    if receipt is not None and receipt.pre_sync_status in SURVEY_SYNC_DONE:
        db.commit()  # Pre actually established the package; keep ownership
        return
    student.survey_owner_case_id = None
    db.commit()


def _finalize_completion(student: Student, receipt: SurveyReceipt) -> None:
    """Mirror the owning package's completion onto the student-level pointer.
    Only ever SETS the timestamp once (COMPLETED is terminal), so the global
    state can never downgrade."""
    if (
        receipt.overall_status == SURVEY_OVERALL_COMPLETED
        and student.survey_completed_at is None
    ):
        student.survey_completed_at = _now()


def get_survey_status(db: Session, session: InterviewSession) -> SurveyStatusOut:
    nuid = (getattr(session.student, "student_number", "") or "").strip()
    case_number = _redcap_case_number(session.case_id)
    # Global (one-package-per-student) fields, resolved server-side from the
    # authenticated session's owner. The frontend gates every survey screen on
    # these: a non-owning case shows the "already submitted / skip" state.
    student = session.student
    owner_case_id = student.survey_owner_case_id
    global_status = _global_survey_status(student)
    is_owner_case = owner_case_id is not None and owner_case_id == session.case_id
    global_completed = global_status == SURVEY_OVERALL_COMPLETED
    receipt = _get_receipt(db, session.student_id, session.case_id)
    if receipt is None:
        return SurveyStatusOut(
            session_id=session.id,
            case_name=_case_name(session.case_id),
            case_number=case_number,
            nuid=nuid,
            nuid_on_file=bool(nuid),
            overall_status=SURVEY_OVERALL_NOT_STARTED,
            pre_submitted=False,
            post_submitted=False,
            global_survey_status=global_status,
            survey_owner_case_id=owner_case_id,
            is_survey_owner_case=is_owner_case,
            global_survey_completed=global_completed,
        )
    return SurveyStatusOut(
        session_id=session.id,
        case_name=_case_name(session.case_id),
        case_number=case_number,
        nuid=nuid,
        nuid_on_file=bool(nuid),
        overall_status=receipt.overall_status,
        pre_submitted=receipt.pre_sync_status in SURVEY_SYNC_DONE,
        post_submitted=receipt.post_sync_status in SURVEY_SYNC_DONE,
        pre_sync_status=receipt.pre_sync_status,
        post_sync_status=receipt.post_sync_status,
        global_survey_status=global_status,
        survey_owner_case_id=owner_case_id,
        is_survey_owner_case=is_owner_case,
        global_survey_completed=global_completed,
    )


def _case_instance_fields(case_id: str) -> dict[str, object]:
    """REDCap routing fields that keep each case in its OWN per-case event -
    see docs/REDCAP.md. This longitudinal behavior is preserved as-is; Pre and
    Post for a case still share the same record_id + event (disjoint fields).

    Longitudinal design (required for multi-case): one event per case; Pre and
    Post both write into that SAME per-case event, so they share one case-level
    instance and only ever touch disjoint fields (overwriteBehavior=normal). The
    event unique-name convention is ``{case_id}_arm_1`` (REDCap derives this from
    an event labelled e.g. "Carly" in Arm 1). When redcap_longitudinal is False
    (a flat single-case project) no routing is added.
    """
    settings = get_settings()
    if not settings.redcap_longitudinal:
        return {}
    return {"redcap_event_name": f"{case_id}_arm_1"}


def _build_fields(
    record_id: str,
    nuid: str,
    case_number: int,
    case_slug: str,
    phase: str,
    answers: dict[str, object],
) -> dict[str, object]:
    """Assemble the exact REDCap field payload: the generated ``record_id`` +
    separate ``nuid`` and ``case_id`` identity fields + per-case instance routing
    + answers + the instrument completion flag (2 = Complete). ``record_id``,
    ``nuid`` and ``case_id`` are all resolved server-side; the client only
    supplies ``answers``."""
    instrument = _INSTRUMENT_BY_PHASE[phase]
    fields: dict[str, object] = {
        "record_id": record_id,
        "nuid": nuid,
        "case_id": str(case_number),
    }
    fields.update(_case_instance_fields(case_slug))
    for key, value in answers.items():
        # Sanitise open-ended text; leave Likert integers as-is.
        fields[key] = value.strip() if isinstance(value, str) else value
    fields[f"{instrument}_complete"] = REDCAP_COMPLETE
    return fields


def _stage_status(receipt: SurveyReceipt, phase: str) -> str:
    return receipt.pre_sync_status if phase == SURVEY_PHASE_PRE else receipt.post_sync_status


def _recompute_overall(receipt: SurveyReceipt) -> None:
    """Defensive completion calculation: the package is COMPLETED if and ONLY if
    BOTH stages are successful (synced or skipped) - never "Post succeeded =>
    completed". Only ever PROMOTES to COMPLETED (COMPLETED is terminal and is
    already guarded against re-entry upstream), so it can never downgrade a
    finished package."""
    pre_done = receipt.pre_sync_status in SURVEY_SYNC_DONE
    post_done = receipt.post_sync_status in SURVEY_SYNC_DONE
    if pre_done and post_done:
        receipt.overall_status = SURVEY_OVERALL_COMPLETED


def _mark_stage(receipt: SurveyReceipt, phase: str, status: str) -> None:
    """Record a stage's sync outcome, then recompute the case-level lifecycle
    from BOTH stages. A DONE Pre alone leaves the package IN_PROGRESS; only
    pre_done AND post_done makes it COMPLETED; a FAILED stage advances nothing."""
    done_at = _now() if status in SURVEY_SYNC_DONE else None
    if phase == SURVEY_PHASE_PRE:
        receipt.pre_sync_status = status
        receipt.pre_synced_at = done_at
    else:
        receipt.post_sync_status = status
        receipt.post_synced_at = done_at
    _recompute_overall(receipt)


def _submit(
    db: Session,
    session: InterviewSession,
    phase: str,
    answers: dict[str, object],
) -> SurveySubmitResult:
    student = session.student
    case_id = session.case_id

    # (0) GLOBAL non-owner gate: the student's single survey package already
    # belongs to a DIFFERENT case, so this case never collects Pre or Post. This
    # is the backend backstop for the frontend "already submitted / skip" gate;
    # checked first so a non-owner submission has zero side effect (no receipt,
    # no REDCap call). A completed owner also lands here for other cases.
    if student.survey_owner_case_id is not None and student.survey_owner_case_id != case_id:
        raise SurveyOwnedByOtherCaseError()

    # (1) A completed package (for THIS owning case) is immutable. Checked before
    # resolving the NUID / touching latest_session_id so even a stale/racing
    # client cannot alter the authoritative receipt (and can never reach REDCap).
    existing = _get_receipt(db, session.student_id, case_id)
    if existing is not None and existing.overall_status == SURVEY_OVERALL_COMPLETED:
        raise SurveyAlreadyCompletedError(_case_name(case_id))

    # Identity fields, resolved server-side (never from the client). NUID stays
    # required research identity; an unmapped case fails loudly. Both are checked
    # before any receipt is created / ownership claimed so a blank NUID / bad case
    # has zero side effect (no phantom receipt, no owner change, no REDCap call).
    nuid = _resolve_nuid(session)
    case_number = _resolve_case_id(case_id)

    # Resolve the receipt + ownership.
    #
    # Ordering gate (Pre + Interview + Post = one package): Post can never reach
    # REDCap, nor complete the package, until Pre has SUCCESSFULLY established it
    # for the OWNING case. For Post this is a PURE READ that must NOT create a
    # receipt or change ownership. Pre atomically claims the single global owner
    # slot BEFORE any REDCap call (serialised by a Student row lock), so two cases
    # can never both establish a package.
    if phase == SURVEY_PHASE_POST:
        receipt = _get_receipt(db, session.student_id, case_id)
        if (
            student.survey_owner_case_id != case_id
            or receipt is None
            or receipt.pre_sync_status not in SURVEY_SYNC_DONE
        ):
            raise SurveyPreRequiredError()
        receipt.latest_session_id = session.id
    else:
        claim = _claim_ownership(db, session)
        if claim == "owned_elsewhere":
            raise SurveyOwnedByOtherCaseError()
        receipt = _get_receipt(db, session.student_id, case_id)
        receipt.latest_session_id = session.id

    # The generated REDCap primary record_id for this package, persisted on the
    # receipt at creation. Reused verbatim for Pre, Post, retries and every later
    # session for this owning case - never regenerated per submission.
    record_id = receipt.redcap_record_id

    # Hard gate: once the package is COMPLETED, NOTHING may reopen it.
    if receipt.overall_status == SURVEY_OVERALL_COMPLETED:
        raise SurveyAlreadyCompletedError(_case_name(case_id))

    # This stage already reached REDCap (or was skipped): do NOT re-import.
    # Makes double-clicks / refreshes / resumes safe and never duplicates a
    # REDCap write. For Pre this is the "already done, continue to the interview"
    # case; the package stays IN_PROGRESS until Post completes.
    if _stage_status(receipt, phase) in SURVEY_SYNC_DONE:
        db.commit()
        return SurveySubmitResult(
            phase=phase,
            sync_status=_stage_status(receipt, phase),
            overall_status=receipt.overall_status,
            already_submitted=True,
        )

    fields = _build_fields(record_id, nuid, case_number, case_id, phase, answers)

    if not redcap_client.is_configured():
        _mark_stage(receipt, phase, SURVEY_SYNC_SKIPPED)
        _finalize_completion(student, receipt)
        db.commit()
        logger.info(
            "survey_submit_skipped_unconfigured session_id=%s case_id=%s phase=%s",
            session.id, case_id, phase,
        )
        return SurveySubmitResult(
            phase=phase, sync_status=SURVEY_SYNC_SKIPPED, overall_status=receipt.overall_status
        )

    try:
        redcap_client.import_record(fields)
    except RedcapNotConfiguredError:
        _mark_stage(receipt, phase, SURVEY_SYNC_SKIPPED)
        _finalize_completion(student, receipt)
        db.commit()
        return SurveySubmitResult(
            phase=phase, sync_status=SURVEY_SYNC_SKIPPED, overall_status=receipt.overall_status
        )
    except RedcapError:
        # Never advance the lifecycle on failure: mark the stage FAILED (safe,
        # traceable, retryable) and surface a clean error. The student's answers
        # stay in the frontend for resubmission; assessment is unaffected.
        _mark_stage(receipt, phase, SURVEY_SYNC_FAILED)
        db.commit()
        # A FAILED FIRST Pre must not permanently lock the student to this case:
        # release the ownership claim so retry (same or another case) is possible.
        if phase == SURVEY_PHASE_PRE:
            _release_ownership_if_unestablished(db, session)
        logger.warning(
            "survey_submit_failed session_id=%s case_id=%s phase=%s",
            session.id, case_id, phase,
        )
        raise SurveySyncError()

    _mark_stage(receipt, phase, SURVEY_SYNC_SYNCED)
    _finalize_completion(student, receipt)
    db.commit()
    logger.info(
        "survey_submit_ok session_id=%s case_id=%s phase=%s overall=%s global=%s",
        session.id, case_id, phase, receipt.overall_status, _global_survey_status(student),
    )
    return SurveySubmitResult(
        phase=phase, sync_status=SURVEY_SYNC_SYNCED, overall_status=receipt.overall_status
    )


def submit_pre(db: Session, session: InterviewSession, payload: PreSurveyIn) -> SurveySubmitResult:
    return _submit(db, session, SURVEY_PHASE_PRE, payload.model_dump())


def submit_post(db: Session, session: InterviewSession, payload: PostSurveyIn) -> SurveySubmitResult:
    return _submit(db, session, SURVEY_PHASE_POST, payload.model_dump())
