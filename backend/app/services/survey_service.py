"""Case-level Pre/Post experience survey service.

Business rule (see the surveys feature spec): a student may complete ONE survey
package per patient case. Pre and Post are the two STAGES of that single
package, keyed by ``UNIQUE(student_id, case_id)`` - NOT per-session, NOT
per-phase. A brand-new interview session for the same case resolves to the SAME
receipt and can never open a second one.

Responsibilities:
- Resolve BOTH the student and the case SERVER-SIDE from the authenticated,
  ownership-checked session (require_session_access). The frontend never
  supplies the NUID, student identity, or case identity - it only sends answers.
- Resolve the REDCap ``record_id`` = the student's NUID (Student.student_number).
  Refuse (NuidMissingError) rather than send an empty/substitute record_id.
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

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import (
    NuidMissingError,
    SurveyAlreadyCompletedError,
    SurveyPreRequiredError,
    SurveySyncError,
)
from app.core.logging import get_logger
from app.models import InterviewSession, SurveyReceipt
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


def _resolve_record_id(session: InterviewSession) -> str:
    """NUID for this encounter's student, resolved server-side. Raises if blank."""
    nuid = (getattr(session.student, "student_number", "") or "").strip()
    if not nuid:
        raise NuidMissingError()
    return nuid


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
    db: Session, session: InterviewSession, record_id: str
) -> SurveyReceipt:
    """Return the ONE (student, case) receipt, creating it if absent.

    Concurrency-safe: the create is attempted inside a SAVEPOINT so that if a
    concurrent request (or a double-click) already inserted the row, the
    UNIQUE(student_id, case_id) violation rolls back only the savepoint (never
    the outer transaction) and we re-read the winner. This is what makes a
    second interview session, a double-click, and two concurrent requests all
    converge on the SAME receipt instead of creating duplicates.
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
        redcap_record_id=record_id,
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


def get_survey_status(db: Session, session: InterviewSession) -> SurveyStatusOut:
    nuid = (getattr(session.student, "student_number", "") or "").strip()
    receipt = _get_receipt(db, session.student_id, session.case_id)
    if receipt is None:
        return SurveyStatusOut(
            session_id=session.id,
            case_name=_case_name(session.case_id),
            nuid_on_file=bool(nuid),
            overall_status=SURVEY_OVERALL_NOT_STARTED,
            pre_submitted=False,
            post_submitted=False,
        )
    return SurveyStatusOut(
        session_id=session.id,
        case_name=_case_name(session.case_id),
        nuid_on_file=bool(nuid),
        overall_status=receipt.overall_status,
        pre_submitted=receipt.pre_sync_status in SURVEY_SYNC_DONE,
        post_submitted=receipt.post_sync_status in SURVEY_SYNC_DONE,
        pre_sync_status=receipt.pre_sync_status,
        post_sync_status=receipt.post_sync_status,
    )


def _case_instance_fields(case_id: str) -> dict[str, object]:
    """REDCap routing fields that keep each case in its OWN instance so the 4
    cases (which reuse the same variable names on the same record_id=NUID) never
    overwrite one another - see docs/REDCAP.md.

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
    record_id: str, case_id: str, phase: str, answers: dict[str, object]
) -> dict[str, object]:
    """Assemble the exact REDCap field payload: record_id + per-case instance
    routing + answers + the instrument completion flag (2 = Complete)."""
    instrument = _INSTRUMENT_BY_PHASE[phase]
    fields: dict[str, object] = {"record_id": record_id}
    fields.update(_case_instance_fields(case_id))
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
    # A completed package is immutable. Check it before resolving the NUID or
    # touching latest_session_id so even a stale/racing client cannot alter the
    # authoritative receipt (and can never reach REDCap).
    existing = _get_receipt(db, session.student_id, session.case_id)
    if existing is not None and existing.overall_status == SURVEY_OVERALL_COMPLETED:
        raise SurveyAlreadyCompletedError(_case_name(session.case_id))

    record_id = _resolve_record_id(session)

    # Resolve the ONE (student, case) receipt.
    #
    # Ordering gate (Pre + Interview + Post = one package): Post can never reach
    # REDCap, and can never complete the package, until Pre has SUCCESSFULLY
    # completed (synced/skipped). For Post this is a PURE READ that must NOT
    # create a receipt - so a Post-without-Pre is rejected with zero side effect
    # (no phantom receipt, no REDCap call, no status change). Pre uses the
    # create-or-get path (it legitimately starts the package).
    if phase == SURVEY_PHASE_POST:
        receipt = _get_receipt(db, session.student_id, session.case_id)
        if receipt is None or receipt.pre_sync_status not in SURVEY_SYNC_DONE:
            raise SurveyPreRequiredError()
        receipt.latest_session_id = session.id
    else:
        receipt = _get_or_create_receipt(db, session, record_id)

    # Hard case-level gate: once the package is COMPLETED, NOTHING (not a new
    # session id, not a resubmit, not the other phase) may reopen it.
    if receipt.overall_status == SURVEY_OVERALL_COMPLETED:
        raise SurveyAlreadyCompletedError(_case_name(session.case_id))

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

    fields = _build_fields(record_id, session.case_id, phase, answers)

    if not redcap_client.is_configured():
        _mark_stage(receipt, phase, SURVEY_SYNC_SKIPPED)
        db.commit()
        logger.info(
            "survey_submit_skipped_unconfigured session_id=%s case_id=%s phase=%s",
            session.id, session.case_id, phase,
        )
        return SurveySubmitResult(
            phase=phase, sync_status=SURVEY_SYNC_SKIPPED, overall_status=receipt.overall_status
        )

    try:
        redcap_client.import_record(fields)
    except RedcapNotConfiguredError:
        _mark_stage(receipt, phase, SURVEY_SYNC_SKIPPED)
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
        logger.warning(
            "survey_submit_failed session_id=%s case_id=%s phase=%s",
            session.id, session.case_id, phase,
        )
        raise SurveySyncError()

    _mark_stage(receipt, phase, SURVEY_SYNC_SYNCED)
    db.commit()
    logger.info(
        "survey_submit_ok session_id=%s case_id=%s phase=%s overall=%s",
        session.id, session.case_id, phase, receipt.overall_status,
    )
    return SurveySubmitResult(
        phase=phase, sync_status=SURVEY_SYNC_SYNCED, overall_status=receipt.overall_status
    )


def submit_pre(db: Session, session: InterviewSession, payload: PreSurveyIn) -> SurveySubmitResult:
    return _submit(db, session, SURVEY_PHASE_PRE, payload.model_dump())


def submit_post(db: Session, session: InterviewSession, payload: PostSurveyIn) -> SurveySubmitResult:
    return _submit(db, session, SURVEY_PHASE_POST, payload.model_dump())
