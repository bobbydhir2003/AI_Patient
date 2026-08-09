"""SSE streaming variant of the interview exchange (feature-flagged).

Event protocol (text/event-stream):

    event: speech    data: {"emotion", "pace", ...}       early delivery labels
    event: sentence  data: {"index", "text"}              approved sentence
    event: final     data: TurnResponse-shaped JSON       one committed turn
    event: error     data: {"code", "message"}            nothing was spoken

Transcript authority rules (documented decision - "Option A"):
- Normal completion: ONE patient turn containing the full approved text.
- Client disconnect (student interruption) after >=1 sentence was emitted:
  the sentences already sent to the client are committed as the patient turn
  (validation_status="interrupted"). That is exactly the text the student saw
  and could hear, so transcript and assessment match the student's experience.
- Any failure/disconnect BEFORE the first sentence: nothing is persisted (the
  student keeps the question, same as the stable path's failure behavior).

Database rule: no transaction spans the OpenAI/TTS stream. One short session
reads the context up front; one short session commits at the end.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace

from app.core.constants import PROMPT_VERSION, ROLE_PATIENT, ROLE_STUDENT
from app.core.exceptions import (
    CaseSessionMismatchError,
    PatientEngineError,
    SessionLockedError,
    SessionNotFoundError,
)
from app.core.logging import get_logger
from app.patient_engine import EngineResult
from app.patient_engine.openai_client import OpenAIPatientClient
from app.patient_engine.streaming_engine import (
    FirstSentenceRejectedError,
    StreamCompleted,
    StreamSentence,
    StreamSpeech,
    stream_patient_response,
)
from app.repositories.session_repository import SessionRepository
from app.repositories.transcript_repository import TranscriptRepository
from app.schemas.interview_schema import StudentMessageRequest

logger = get_logger(__name__)


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _speech_out(speech: dict | None) -> dict | None:
    if not speech:
        return None
    return {
        "emotion": speech.get("emotion", "neutral"),
        "pace": speech.get("pace", "normal"),
        "energy": speech.get("energy", "normal"),
        "hesitation": speech.get("hesitation", "none"),
        "pauseBeforeMs": speech.get("pause_before_ms", 150),
    }


@dataclass
class _TurnContext:
    case_id: str
    question: str
    turns: list  # plain snapshots (role/content) - no live ORM objects
    disclosed_fact_ids: set[str]
    active_topic: str | None
    turn_number: int
    replay: dict | None = None  # pre-built final event data for idempotent replays
    session_status: str = "active"
    newly_disclosed: set[str] = field(default_factory=set)
    # Multi-participant routing (single-speaker cases keep "patient"/"").
    speaker_id: str = "patient"
    speaker_label: str = ""
    engine_speaker: str | None = None  # what to frame the prompt as (None = unchanged)


def _load_context(session_factory, session_id: str, payload: StudentMessageRequest) -> _TurnContext:
    """Short transaction #1: validate, idempotency check, snapshot context."""
    db = session_factory()
    try:
        session_repo = SessionRepository(db)
        transcript_repo = TranscriptRepository(db)
        session = session_repo.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        if session.locked:
            raise SessionLockedError(session_id)
        if payload.case_id != session.case_id:
            raise CaseSessionMismatchError(payload.case_id, session.case_id)

        question = payload.text.strip()

        if payload.client_turn_id:
            existing_student = transcript_repo.get_by_client_turn_id(
                session_id, payload.client_turn_id
            )
            if existing_student is not None:
                existing_patient = transcript_repo.get_by_index(
                    session_id, existing_student.turn_index + 1
                )
                if existing_patient is not None and existing_patient.role == ROLE_PATIENT:
                    logger.info(
                        "turn_replayed session_id=%s client_turn_id=%s turn=%d (stream)",
                        session_id, payload.client_turn_id, existing_student.turn_index,
                    )
                    return _TurnContext(
                        case_id=session.case_id,
                        question=question,
                        turns=[],
                        disclosed_fact_ids=set(),
                        active_topic=None,
                        turn_number=existing_student.turn_index,
                        replay={
                            "turnId": existing_patient.id,
                            "patientText": existing_patient.content,
                            "status": "completed",
                            "sessionStatus": session.status,
                            "speech": None,
                        },
                    )

        prior_turns = transcript_repo.list_turns(session_id)
        snapshots = [
            SimpleNamespace(
                role=t.role, content=t.content,
                speaker_id=getattr(t, "speaker_id", "patient"),
            )
            for t in prior_turns
        ]
        # ---- Speaker routing (multi-participant cases only) ----
        from app.patient_engine import case_loader, speaker_router

        case = case_loader.load_case(session.case_id)
        routing = speaker_router.resolve_for_case(case, question, snapshots)
        # Live audio streams one voice at a time; a "both" turn routes to the
        # primary historian (mother) in the streaming path. The non-streaming
        # path returns a true two-segment joint response.
        chosen = "mother" if routing.speaker == speaker_router.SPEAKER_BOTH else routing.speaker
        eff_sid, label, _vk = speaker_router.participant_meta(case, chosen)
        engine_speaker = chosen if speaker_router.is_multi_participant(case) else None

        return _TurnContext(
            case_id=session.case_id,
            question=question,
            turns=snapshots,
            disclosed_fact_ids=set(session_repo.get_disclosed_fact_ids(session)),
            active_topic=session.active_topic,
            turn_number=len(prior_turns),
            session_status=session.status,
            speaker_id=eff_sid,
            speaker_label=label,
            engine_speaker=engine_speaker,
        )
    finally:
        db.close()


def _commit_turn(
    session_factory,
    session_id: str,
    payload: StudentMessageRequest,
    ctx: _TurnContext,
    result: EngineResult,
    *,
    interrupted: bool,
) -> dict:
    """Short transaction #2: persist EXACTLY ONE student + ONE patient turn."""
    db = session_factory()
    try:
        session_repo = SessionRepository(db)
        transcript_repo = TranscriptRepository(db)
        session = session_repo.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)

        # Race-safe idempotency re-check inside the committing transaction.
        if payload.client_turn_id:
            existing_student = transcript_repo.get_by_client_turn_id(
                session_id, payload.client_turn_id
            )
            if existing_student is not None:
                existing_patient = transcript_repo.get_by_index(
                    session_id, existing_student.turn_index + 1
                )
                if existing_patient is not None and existing_patient.role == ROLE_PATIENT:
                    return {
                        "turnId": existing_patient.id,
                        "patientText": existing_patient.content,
                        "status": "completed",
                        "sessionStatus": session.status,
                        "speech": None,
                    }

        transcript_repo.append_turn(
            session_id, ROLE_STUDENT, ctx.question,
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
            validation_status="interrupted" if interrupted else result.validation_status,
            speaker_id=ctx.speaker_id,
            speaker_label=ctx.speaker_label,
        )
        session_repo.add_disclosed_fact_ids(session, result.newly_disclosed_fact_ids)
        session_repo.set_active_topic(session, result.active_topic)
        db.commit()

        # Record real per-session OpenAI usage for this committed turn (once).
        # Replays return early above, so a turn is never counted twice; an
        # interrupted stream carries result.usage=None and is skipped (no fake).
        from app.services import usage_recorder

        usage_recorder.record_openai_usage(
            db, session_id, session.student_id, ctx.case_id, result.usage
        )
        logger.info(
            "turn_completed session_id=%s client_turn_id=%s case_id=%s turn=%d "
            "openai_called=True model=%s response_type=%s response_validated=%s "
            "response_saved=True streamed=True interrupted=%s",
            session_id, payload.client_turn_id or "-", ctx.case_id,
            patient_turn.turn_index, result.model_name, result.response_type,
            result.validation_status, interrupted,
        )
        return {
            "turnId": patient_turn.id,
            "patientText": patient_turn.content,
            "status": "interrupted" if interrupted else "completed",
            "sessionStatus": session.status,
            "speech": _speech_out(result.speech),
            "speakerId": ctx.speaker_id,
            "speakerLabel": ctx.speaker_label,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _partial_result(sentences: list[str], active_topic: str | None) -> EngineResult:
    """EngineResult for an interrupted/partial stream: exactly the approved
    sentences that were emitted to the client, no fact-disclosure accounting
    (conservative: unconfirmed fact usage is never marked disclosed). The
    session's active topic is preserved unchanged."""
    from app.core.config import get_settings

    return EngineResult(
        text=" ".join(sentences).strip(),
        topics=[],
        active_topic=active_topic,
        response_type="clinical_answer",
        model_name=get_settings().openai_model,
        prompt_version=PROMPT_VERSION,
        used_fact_ids=[],
        newly_disclosed_fact_ids=set(),
        validation_status="interrupted",
        speech=None,
    )


def stream_student_message(
    session_factory,
    session_id: str,
    payload: StudentMessageRequest,
    client: OpenAIPatientClient | None = None,
) -> Iterator[bytes]:
    """Validate + snapshot context EAGERLY (so 404/409 errors surface as
    normal JSON errors before any bytes stream), then return the SSE
    generator for the actual exchange.

    The interview concurrency slot (same global, Redis-backed guard the
    non-streaming path uses - core/concurrency.interview_slot) is also
    reserved HERE, synchronously, before any StreamingResponse is
    constructed: a ServiceOverloadedError raised here propagates as a normal
    503 JSON error, exactly like the non-streaming path, instead of surfacing
    mid-stream after headers were already sent. The slot is released inside
    _stream_events once generation ends (success, error, or client
    disconnect)."""
    from app.core.config import get_settings

    debug = bool(get_settings().debug)
    t0 = time.monotonic()
    correlation_id = payload.client_turn_id or "-"

    # ---- Short transaction #1: context snapshot (fast; then closed).
    # Raises SessionNotFound/SessionLocked/CaseSessionMismatch BEFORE the
    # streaming response starts.
    ctx = _load_context(session_factory, session_id, payload)
    if debug:
        logger.info(
            "stream_timing mark=context_loaded ms=%.0f turn=%s session=%s",
            (time.monotonic() - t0) * 1000, correlation_id, session_id,
        )

    slot = None
    if ctx.replay is None:  # an idempotent replay never calls OpenAI - no slot needed
        from app.core.concurrency import interview_slot

        slot = interview_slot()
        slot.__enter__()  # raises ServiceOverloadedError (503) before any streaming begins

    return _stream_events(
        session_factory, session_id, payload, ctx, client, t0, debug, correlation_id, slot
    )


def _stream_events(
    session_factory,
    session_id: str,
    payload: StudentMessageRequest,
    ctx: _TurnContext,
    client: OpenAIPatientClient | None,
    t0: float,
    debug: bool,
    correlation_id: str,
    slot=None,
) -> Iterator[bytes]:
    if ctx.replay is not None:
        yield _sse("final", ctx.replay)
        return

    emitted_sentences: list[str] = []
    committed = False

    def _commit_partial_if_needed(reason: str) -> dict | None:
        nonlocal committed
        if committed or not emitted_sentences:
            return None
        try:
            data = _commit_turn(
                session_factory, session_id, payload, ctx,
                _partial_result(emitted_sentences, ctx.active_topic),
                interrupted=True,
            )
            committed = True
            logger.info(
                "stream_partial_committed session_id=%s turn=%s sentences=%d reason=%s",
                session_id, correlation_id, len(emitted_sentences), reason,
            )
            return data
        except Exception:
            logger.exception(
                "stream_partial_commit_failed session_id=%s turn=%s", session_id, correlation_id
            )
            return None

    try:
        # Tell the frontend who is speaking BEFORE any sentence, so it can label
        # the message and pick the matching voice for playback.
        if ctx.speaker_id != "patient":
            yield _sse("speaker", {"speakerId": ctx.speaker_id, "speakerLabel": ctx.speaker_label})

        engine_stream = stream_patient_response(
            case_id=ctx.case_id,
            question=ctx.question,
            turns=ctx.turns,
            disclosed_fact_ids=ctx.disclosed_fact_ids,
            active_topic=ctx.active_topic,
            client=client,
            correlation_id=correlation_id,
            speaker_id=ctx.engine_speaker,
        )
        final: StreamCompleted | None = None
        try:
            for event in engine_stream:
                if isinstance(event, StreamSpeech):
                    yield _sse("speech", _speech_out(event.speech) or {})
                elif isinstance(event, StreamSentence):
                    if debug and event.index == 0:
                        logger.info(
                            "stream_timing mark=first_sentence_emitted ms=%.0f turn=%s",
                            (time.monotonic() - t0) * 1000, correlation_id,
                        )
                    emitted_sentences.append(event.text)
                    yield _sse("sentence", {"index": event.index, "text": event.text})
                elif isinstance(event, StreamCompleted):
                    final = event
        finally:
            engine_stream.close()

        if final is None:
            raise PatientEngineError("The streamed response ended without completing.")

        # ---- Short transaction #2: one authoritative commit ----
        data = _commit_turn(
            session_factory, session_id, payload, ctx, final.result, interrupted=False
        )
        committed = True
        if debug:
            logger.info(
                "stream_timing mark=db_commit_complete ms=%.0f turn=%s "
                "elevenlabs_chars=%d openai_requests=1 tts_sessions=per-sentence-http",
                (time.monotonic() - t0) * 1000, correlation_id, len(final.result.text),
            )
        yield _sse("final", data)

    except GeneratorExit:
        # Client disconnected (student interruption / navigation). Persist the
        # sentences the student already received; never regenerate or resume.
        _commit_partial_if_needed("client_disconnect")
        raise
    except FirstSentenceRejectedError:
        logger.warning(
            "stream_failed_before_speech session_id=%s turn=%s code=first_sentence_rejected",
            session_id, correlation_id,
        )
        yield _sse("error", {
            "code": "stream_first_sentence_rejected",
            "message": "The streamed response could not be validated.",
        })
    except PatientEngineError as exc:
        if emitted_sentences:
            # Sentences were already sent (and possibly spoken): commit exactly
            # that text so the transcript matches what the student experienced.
            data = _commit_partial_if_needed("generation_error")
            if data is None:
                data = {
                    "turnId": "",
                    "patientText": " ".join(emitted_sentences).strip(),
                    "status": "interrupted",
                    "sessionStatus": ctx.session_status,
                    "speech": None,
                }
            yield _sse("final", data)
        else:
            logger.warning(
                "stream_failed session_id=%s turn=%s error=%s",
                session_id, correlation_id, exc.code,
            )
            yield _sse("error", {
                "code": "PATIENT_RESPONSE_UNAVAILABLE",
                "message": "The patient response could not be generated. Please try again.",
            })
    finally:
        # Symmetric with the acquire in stream_student_message: released on
        # every exit path (normal completion, a caught error above, an
        # uncaught exception, or GeneratorExit from a client disconnect).
        if slot is not None:
            slot.__exit__(None, None, None)
