import time

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import PROMPT_VERSION, ROLE_PATIENT, ROLE_STUDENT
from app.core.exceptions import (
    CaseSessionMismatchError,
    SessionLockedError,
    SessionNotFoundError,
)
from app.core.logging import get_logger
from app.patient_engine import generate_patient_response
from app.patient_engine.openai_client import OpenAIPatientClient
from app.repositories.session_repository import SessionRepository
from app.repositories.transcript_repository import TranscriptRepository
from app.schemas.interview_schema import SpeechStyleOut, StudentMessageRequest, TurnResponse

logger = get_logger(__name__)


def send_student_message(
    db: Session,
    session_id: str,
    payload: StudentMessageRequest,
    client: OpenAIPatientClient | None = None,
) -> TurnResponse:
    session_repo = SessionRepository(db)
    transcript_repo = TranscriptRepository(db)

    session = session_repo.get(session_id)
    if session is None:
        raise SessionNotFoundError(session_id)
    if session.locked:
        raise SessionLockedError(session_id)
    # Cross-case isolation guard: the UI's case must match the session's case.
    if payload.case_id != session.case_id:
        raise CaseSessionMismatchError(payload.case_id, session.case_id)

    question = payload.text.strip()

    # Idempotency: if this client_turn_id was already saved (retry, voice
    # callback, reconnect), return the existing exchange without regenerating.
    if payload.client_turn_id:
        existing_student = transcript_repo.get_by_client_turn_id(session_id, payload.client_turn_id)
        if existing_student is not None:
            existing_patient = transcript_repo.get_by_index(
                session_id, existing_student.turn_index + 1
            )
            if existing_patient is not None and existing_patient.role == ROLE_PATIENT:
                logger.info(
                    "turn_replayed session_id=%s client_turn_id=%s turn=%d",
                    session_id, payload.client_turn_id, existing_student.turn_index,
                )
                return TurnResponse(
                    turn_id=existing_patient.id,
                    patient_text=existing_patient.content,
                    status="completed",
                    session_status=session.status,
                )

    prior_turns = transcript_repo.list_turns(session_id)
    turn_number = len(prior_turns)

    # Generate FIRST. If generation fails, nothing is persisted and the student
    # keeps their question to retry (no fake patient replies, ever).
    # Dev-only timing around the OpenAI stage (the previously uninstrumented
    # largest stage; correlation id = client_turn_id).
    t_generate = time.monotonic()
    try:
        result = generate_patient_response(
            case_id=session.case_id,
            question=question,
            turns=prior_turns,
            disclosed_fact_ids=session_repo.get_disclosed_fact_ids(session),
            active_topic=session.active_topic,
            client=client,
        )
    except Exception:
        db.rollback()
        logger.error(
            "turn_failed session_id=%s case_id=%s turn=%d openai_called=True "
            "response_saved=False error_code=PATIENT_RESPONSE_UNAVAILABLE",
            session.id,
            session.case_id,
            turn_number,
        )
        raise

    if get_settings().debug:
        logger.info(
            "interview_timing mark=openai_generate_ms value=%.0f turn=%s case_id=%s",
            (time.monotonic() - t_generate) * 1000,
            payload.client_turn_id or "-",
            session.case_id,
        )

    student_turn = transcript_repo.append_turn(
        session_id, ROLE_STUDENT, question,
        client_turn_id=payload.client_turn_id or None,
        source=payload.source,
    )
    patient_turn = transcript_repo.append_turn(
        session_id,
        ROLE_PATIENT,
        result.text,
        client_turn_id=(payload.client_turn_id + ":patient") if payload.client_turn_id else None,
        source="openai",
        model_name=result.model_name,
        prompt_version=PROMPT_VERSION,
        facts_used=result.used_fact_ids,
        response_type=result.response_type,
        validation_status=result.validation_status,
    )
    session_repo.add_disclosed_fact_ids(session, result.newly_disclosed_fact_ids)
    session_repo.set_active_topic(session, result.active_topic)
    db.commit()

    logger.info(
        "turn_completed session_id=%s client_turn_id=%s case_id=%s turn=%d topic=%s "
        "openai_called=True model=%s response_type=%s response_validated=%s response_saved=True",
        session.id,
        payload.client_turn_id or "-",
        session.case_id,
        patient_turn.turn_index,
        ",".join(result.topics),
        result.model_name,
        result.response_type,
        result.validation_status,
    )

    return TurnResponse(
        turn_id=patient_turn.id,
        patient_text=patient_turn.content,
        status="completed",
        session_status=session.status,
        # Delivery-only speech labels for TTS. Not stored in the transcript and
        # never part of assessment input.
        speech=SpeechStyleOut(**result.speech) if result.speech else None,
    )
