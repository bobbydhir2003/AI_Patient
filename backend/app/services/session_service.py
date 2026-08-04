from sqlalchemy.orm import Session

from app.core.constants import ADMIN_ROLES, ROLE_STUDENT
from app.core.exceptions import (
    ForbiddenError,
    SessionNotFoundError,
    TranscriptEmptyError,
    TranscriptLockedError,
)
from app.core.logging import get_logger
from app.models import InterviewSession, Student, User
from app.patient_engine.case_loader import load_case
from app.repositories.session_repository import SessionRepository
from app.repositories.transcript_repository import TranscriptRepository
from app.schemas.interview_schema import MessageOut, TurnCreateRequest, TurnOut
from app.schemas.session_schema import SessionCreateRequest, SessionResponse

logger = get_logger(__name__)


def _to_response(db: Session, session: InterviewSession) -> SessionResponse:
    import json as _json

    turns = TranscriptRepository(db).list_turns(session.id)
    return SessionResponse(
        session_id=session.id,
        case_id=session.case_id,
        case_category=session.case_category,
        assessment_capabilities=_json.loads(session.assessment_capabilities or '["standard_interview"]'),
        protected_reference_version=session.protected_reference_version,
        student_name=session.student.name,
        student_id=session.student.student_number,
        status=session.status,
        locked=session.locked,
        started_at=session.started_at,
        completed_at=session.completed_at,
        messages=[
            MessageOut(
                id=t.id, sender=t.role, text=t.content, timestamp=t.created_at,
                speaker_id=getattr(t, "speaker_id", "patient") or "patient",
                speaker_label=getattr(t, "speaker_label", "") or "",
            )
            for t in turns
        ],
    )


def _turn_out(turn) -> TurnOut:
    return TurnOut(
        id=turn.id,
        session_id=turn.session_id,
        client_turn_id=turn.client_turn_id,
        speaker=turn.role,
        content=turn.content,
        source=turn.source,
        turn_index=turn.turn_index,
        created_at=turn.created_at,
    )


def create_session(
    db: Session, payload: SessionCreateRequest, current_user: User
) -> SessionResponse:
    import json as _json

    case = load_case(payload.case_id)  # raises CaseNotFoundError for unknown ids
    # A3: the session owner is ALWAYS the authenticated account's linked profile.
    # Any student_name / student_id in the request body is display-only and is
    # never used to decide ownership, so a caller cannot create a session under
    # another identity.
    is_admin = current_user.role in ADMIN_ROLES
    student = current_user.student if current_user.student_id else None
    if student is None:
        if is_admin:
            # Provision a one-time practice profile so an admin/professor can run
            # the full simulator. It is flagged is_practice=True and therefore is
            # excluded from the student roster and all class analytics.
            student = Student(
                name=current_user.full_name or "Administrator",
                student_number="",
                email=current_user.email,
                is_practice=True,
            )
            db.add(student)
            db.flush()
            current_user.student_id = student.id
        else:
            raise ForbiddenError("This account is not linked to a student profile.")
    capabilities = ["standard_interview"]
    if case.case_category == "referral":
        capabilities.append("advanced_referral")  # future referral assessment pipeline
    session = SessionRepository(db).create(
        student_id=student.id,
        case_id=payload.case_id,
        case_category=case.case_category,
        assessment_capabilities=_json.dumps(capabilities),
        protected_reference_version="1.0",
        # Admin/professor sessions are practice ("admin_test") and never counted
        # in student completion stats or academic analytics.
        is_practice=is_admin,
    )
    # Freeze the active (non-secret) config this interview should keep using, so
    # an admin editing the model/voice mid-way cannot alter an in-progress
    # session. New sessions capture the latest config here.
    try:
        from app.services import runtime_config_service
        session.config_snapshot = runtime_config_service.session_snapshot(db, payload.case_id)
    except Exception:  # snapshot is best-effort; never block starting an interview
        logger.warning("config_snapshot_failed case_id=%s", payload.case_id)
    db.commit()
    # B2: register the session in the live-activity registry (in-memory only).
    try:
        from app.core.telemetry import get_telemetry

        get_telemetry().live.start_session(
            session_id=session.id,
            student_name=student.name,
            student_number=student.student_number,
            case_id=session.case_id,
            case_name=getattr(case, "display_name", session.case_id) or session.case_id,
        )
    except Exception:  # telemetry must never block starting an interview
        pass
    return _to_response(db, session)


def get_session(db: Session, session_id: str) -> SessionResponse:
    session = SessionRepository(db).get(session_id)
    if session is None:
        raise SessionNotFoundError(session_id)
    return _to_response(db, session)


def complete_session(db: Session, session_id: str) -> SessionResponse:
    repo = SessionRepository(db)
    session = repo.get(session_id)
    if session is None:
        raise SessionNotFoundError(session_id)
    if session.locked:
        return _to_response(db, session)  # idempotent

    # The backend counts SAVED rows itself - it never trusts a frontend count.
    transcript = TranscriptRepository(db)
    student_turns = transcript.count_nonempty_by_role(session_id, "student")
    patient_turns = transcript.count_nonempty_by_role(session_id, "patient")
    if student_turns < 1 or patient_turns < 1:
        logger.warning(
            "completion_rejected session_id=%s student_turns=%d patient_turns=%d",
            session_id, student_turns, patient_turns,
        )
        raise TranscriptEmptyError()

    repo.complete_and_lock(session)
    db.commit()
    try:
        from app.core.telemetry import get_telemetry

        get_telemetry().live.complete_session(session_id)
    except Exception:
        pass
    logger.info(
        "session_completed session_id=%s backend_turn_count=%d transcript_locked=True",
        session_id, student_turns + patient_turns,
    )
    return _to_response(db, session)


def list_turns(db: Session, session_id: str) -> list[TurnOut]:
    session = SessionRepository(db).get(session_id)
    if session is None:
        raise SessionNotFoundError(session_id)
    return [_turn_out(t) for t in TranscriptRepository(db).list_turns(session_id)]


def append_student_turn(db: Session, session_id: str, payload: TurnCreateRequest) -> TurnOut:
    """Idempotent single-turn append for STUDENT-authored turns (recovery/retry).

    A4 - transcript write integrity:
    - The speaker is enforced server-side as STUDENT. A client that claims
      speaker="patient" is rejected: a student must never be able to fabricate a
      patient reply. Patient turns are persisted only by the trusted generation
      path (interview_service / interview_stream_service).
    - Only student input sources (typed/speech) are accepted here; the
      openai/system sources are reserved for the trusted server path.
    - Writing to a completed/locked session is rejected.
    Repeating the same session_id + client_turn_id returns the existing turn.
    """
    if payload.speaker != ROLE_STUDENT:
        raise ForbiddenError("Only student turns can be submitted here; patient replies are server-generated.")
    if payload.source not in ("typed", "speech"):
        raise ForbiddenError("Unsupported turn source for a student-authored turn.")

    session = SessionRepository(db).get(session_id)
    if session is None:
        raise SessionNotFoundError(session_id)
    if session.locked:
        raise TranscriptLockedError()
    transcript = TranscriptRepository(db)
    existing = transcript.get_by_client_turn_id(session_id, payload.client_turn_id)
    if existing is None:
        existing = transcript.append_turn(
            session_id,
            ROLE_STUDENT,  # server-controlled identity, never trusts the body
            payload.content.strip(),
            client_turn_id=payload.client_turn_id,
            source=payload.source,
        )
        db.commit()
        logger.info(
            "turn_saved session_id=%s client_turn_id=%s speaker=%s turn_index=%d",
            session_id, payload.client_turn_id, ROLE_STUDENT, existing.turn_index,
        )
    return _turn_out(existing)
