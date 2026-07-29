from sqlalchemy.orm import Session

from app.core.exceptions import (
    SessionNotFoundError,
    TranscriptEmptyError,
    TranscriptLockedError,
)
from app.core.logging import get_logger
from app.models import InterviewSession
from app.patient_engine.case_loader import load_case
from app.repositories.session_repository import SessionRepository
from app.repositories.student_repository import StudentRepository
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


def create_session(db: Session, payload: SessionCreateRequest) -> SessionResponse:
    import json as _json

    case = load_case(payload.case_id)  # raises CaseNotFoundError for unknown ids
    student = StudentRepository(db).get_or_create(payload.student_name.strip(), payload.student_id.strip())
    capabilities = ["standard_interview"]
    if case.case_category == "referral":
        capabilities.append("advanced_referral")  # future referral assessment pipeline
    session = SessionRepository(db).create(
        student_id=student.id,
        case_id=payload.case_id,
        case_category=case.case_category,
        assessment_capabilities=_json.dumps(capabilities),
        protected_reference_version="1.0",
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


def append_turn(db: Session, session_id: str, payload: TurnCreateRequest) -> TurnOut:
    """Idempotent single-turn append (canonical transcript endpoint).

    The normal chat flow persists both turns atomically inside the exchange
    endpoint; this endpoint exists for recovery/retry paths and future tooling.
    Repeating the same session_id + client_turn_id returns the existing turn.
    """
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
            payload.speaker,
            payload.content.strip(),
            client_turn_id=payload.client_turn_id,
            source=payload.source,
        )
        db.commit()
        logger.info(
            "turn_saved session_id=%s client_turn_id=%s speaker=%s turn_index=%d",
            session_id, payload.client_turn_id, payload.speaker, existing.turn_index,
        )
    return _turn_out(existing)
