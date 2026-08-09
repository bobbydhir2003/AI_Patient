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
from app.schemas.interview_schema import (
    SpeechStyleOut,
    StudentMessageRequest,
    TurnResponse,
    TurnSegmentOut,
)

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

    # ---- Speaker routing (multi-participant cases only; single-speaker cases
    # keep the generic 'patient' speaker and behave exactly as before). ----
    from app.patient_engine import case_loader, speaker_router

    case = case_loader.load_case(session.case_id)
    routing = speaker_router.resolve_for_case(case, question, prior_turns)
    if routing.speaker == speaker_router.SPEAKER_BOTH:
        speaker_ids = [speaker_router.SPEAKER_CAMDEN, speaker_router.SPEAKER_MOTHER]
    else:
        speaker_ids = [routing.speaker]
    logger.info(
        "speaker_routed session_id=%s speaker=%s confidence=%.2f",
        session.id, routing.speaker, routing.confidence,
    )

    # Generate FIRST (all segments). If generation fails, nothing is persisted
    # and the student keeps their question to retry (no fake patient replies).
    # B5: an interview slot is reserved first; if the server is at its AI
    # concurrency limit, a controlled ServiceOverloadedError (503) is raised
    # BEFORE any OpenAI call - never a raw provider error.
    from app.core.concurrency import interview_slot
    from app.core.telemetry import get_telemetry

    tele = get_telemetry()
    t_generate = time.monotonic()
    results = []
    with interview_slot():
        tele.live.set_status(session.id, "WAITING_FOR_AI")
        try:
            for sid in speaker_ids:
                # None for single-speaker cases -> unchanged prompt/behavior.
                engine_speaker = sid if speaker_router.is_multi_participant(case) else None
                results.append(
                    generate_patient_response(
                        case_id=session.case_id,
                        question=question,
                        turns=prior_turns,
                        disclosed_fact_ids=session_repo.get_disclosed_fact_ids(session),
                        active_topic=session.active_topic,
                        client=client,
                        speaker_id=engine_speaker,
                    )
                )
        except Exception:
            db.rollback()
            tele.live.set_status(session.id, "INTERVIEWING")
            logger.error(
                "turn_failed session_id=%s case_id=%s turn=%d openai_called=True "
                "response_saved=False error_code=PATIENT_RESPONSE_UNAVAILABLE",
                session.id, session.case_id, turn_number,
            )
            raise
    latency_ms = int((time.monotonic() - t_generate) * 1000)
    tele.live.set_status(session.id, "INTERVIEWING", latency_ms=latency_ms)

    if get_settings().debug:
        logger.info(
            "interview_timing mark=openai_generate_ms value=%.0f turn=%s case_id=%s",
            (time.monotonic() - t_generate) * 1000, payload.client_turn_id or "-", session.case_id,
        )

    student_turn = transcript_repo.append_turn(
        session_id, ROLE_STUDENT, question,
        client_turn_id=payload.client_turn_id or None, source=payload.source,
    )

    segments: list[TurnSegmentOut] = []
    patient_turns = []
    for idx, (sid, result) in enumerate(zip(speaker_ids, results)):
        eff_sid, label, _voice_key = speaker_router.participant_meta(case, sid)
        cti = None
        if payload.client_turn_id:
            cti = payload.client_turn_id + (":patient" if idx == 0 else f":patient:{eff_sid}")
        pt = transcript_repo.append_turn(
            session_id, ROLE_PATIENT, result.text,
            client_turn_id=cti, source="openai",
            model_name=result.model_name, prompt_version=PROMPT_VERSION,
            facts_used=result.used_fact_ids, response_type=result.response_type,
            validation_status=result.validation_status,
            speaker_id=eff_sid, speaker_label=label,
        )
        patient_turns.append(pt)
        segments.append(TurnSegmentOut(
            turn_id=pt.id, speaker_id=eff_sid, speaker_label=label, text=pt.content,
            speech=SpeechStyleOut(**result.speech) if result.speech else None,
        ))
        session_repo.add_disclosed_fact_ids(session, result.newly_disclosed_fact_ids)
        session_repo.set_active_topic(session, result.active_topic)

    db.commit()

    # Record real per-session OpenAI usage AFTER the turn is persisted. One event
    # per real model request (a joint 'both' turn made two requests → two events).
    # Best-effort: never raises, never slows the turn's own transaction.
    from app.services import usage_recorder

    for result in results:
        usage_recorder.record_openai_usage(
            db, session.id, session.student_id, session.case_id, result.usage
        )

    primary = patient_turns[0]
    logger.info(
        "turn_completed session_id=%s client_turn_id=%s case_id=%s turn=%d speaker=%s "
        "segments=%d openai_called=True model=%s response_saved=True",
        session.id, payload.client_turn_id or "-", session.case_id, primary.turn_index,
        routing.speaker, len(segments), results[0].model_name,
    )

    return TurnResponse(
        turn_id=primary.id,
        patient_text=primary.content,
        status="completed",
        session_status=session.status,
        speech=segments[0].speech,
        speaker_id=segments[0].speaker_id,
        speaker_label=segments[0].speaker_label,
        responses=segments if len(segments) > 1 or speaker_router.is_multi_participant(case) else None,
    )
