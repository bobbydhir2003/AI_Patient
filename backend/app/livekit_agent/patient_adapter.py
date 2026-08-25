"""Adapter: lets the LiveKit POC agent reuse EXACTLY the production patient-
response and TTS pipeline - no second prompt system, no second ElevenLabs
client, no bypass of either distributed concurrency semaphore.

Every function here calls the SAME modules app/services/interview_service.py
and app/api/voice.py already call:
    generate_patient_response   app/patient_engine/__init__.py
    interview_slot / tts_slot   app/core/concurrency.py (Redis-backed)
    ElevenLabsClient            app/voice/elevenlabs_client.py
    load_voice_profile          app/voice/voice_profile_loader.py
    TranscriptRepository        app/repositories/transcript_repository.py

The ONE deliberate difference from the production /synthesize endpoint: this
adapter requests RAW PCM from ElevenLabs (output_format="pcm_16000") instead
of the configured MP3 default, because LiveKit's AudioSource/AudioFrame API
consumes raw PCM samples directly - this avoids adding any audio-decoding
dependency (no ffmpeg/pydub/av) for the POC. This does not change the
production default (settings.elevenlabs_output_format is untouched; this
module passes its own explicit output_format on each call).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from app.core.concurrency import interview_slot, tts_slot
from app.core.constants import PROMPT_VERSION, ROLE_PATIENT, ROLE_STUDENT
from app.core.logging import get_logger
from app.patient_engine import generate_patient_response
from app.repositories.session_repository import SessionRepository
from app.repositories.transcript_repository import TranscriptRepository
from app.voice.elevenlabs_client import get_elevenlabs_client
from app.voice.speech_style_mapper import map_speech_style
from app.voice.voice_profile_loader import load_voice_profile

logger = get_logger("app.livekit_agent")

LIVEKIT_PCM_SAMPLE_RATE = 16000
LIVEKIT_PCM_OUTPUT_FORMAT = f"pcm_{LIVEKIT_PCM_SAMPLE_RATE}"

# Diagnostic-only stage callback for real-device latency validation (see
# worker.py's _run_turn, which records a monotonic timestamp per stage name
# and logs the breakdown once per turn - never patient text/audio/secrets).
# Optional and additive: omitting it (the default) changes nothing.
StageCallback = Callable[[str], None]


class LiveKitPocSessionNotFoundError(Exception):
    """Raised when the POC agent is given a session id that does not exist.
    Distinct from the HTTP-facing SessionNotFoundError - this module has no
    HTTP context of its own (it is called from worker.py, not a request)."""


@dataclass(frozen=True)
class PocTurnResult:
    student_turn_id: str
    patient_turn_id: str
    patient_text: str
    replayed: bool  # True if this was an idempotent replay, not a fresh generation


def generate_and_persist_turn(
    db: Session,
    *,
    session_id: str,
    case_id: str,
    question: str,
    client_turn_id: str,
    on_stage: StageCallback | None = None,
) -> PocTurnResult:
    """Single-speaker reuse of interview_service.send_student_message's core
    steps. Deliberately does NOT reimplement multi-participant speaker
    routing (Camden/mother) - the POC targets one case with a single primary
    speaker (see worker.py). Idempotent on client_turn_id, identically to
    production: a duplicate/retried call for the same logical turn replays
    the existing turn instead of generating (and billing) a second time.
    """
    session_repo = SessionRepository(db)
    transcript_repo = TranscriptRepository(db)

    session = session_repo.get(session_id)
    if session is None:
        raise LiveKitPocSessionNotFoundError(session_id)

    existing_student = transcript_repo.get_by_client_turn_id(session_id, client_turn_id)
    if existing_student is not None:
        existing_patient = transcript_repo.get_by_index(session_id, existing_student.turn_index + 1)
        if existing_patient is not None and existing_patient.role == ROLE_PATIENT:
            logger.info(
                "livekit_poc_turn_replayed session_id=%s client_turn_id=%s", session_id, client_turn_id,
            )
            return PocTurnResult(
                student_turn_id=existing_student.id,
                patient_turn_id=existing_patient.id,
                patient_text=existing_patient.content,
                replayed=True,
            )

    prior_turns = transcript_repo.list_turns(session_id)

    # SAME distributed OpenAI semaphore production uses (core/concurrency.py,
    # Redis-backed) - a fleet-wide cap, not a per-process one, so this agent
    # process counts against the exact same limit as every FastAPI worker.
    if on_stage:
        on_stage("openai_slot_wait_start")
    with interview_slot():
        # "openai_slot_acquired" and "openai_request_start" are the same
        # instant from here - generate_patient_response is called immediately
        # upon acquiring the slot. Separating them further would require
        # instrumenting app/patient_engine/ itself, which is production code
        # and out of scope for this POC-only diagnostic.
        if on_stage:
            on_stage("openai_slot_acquired")
        result = generate_patient_response(
            case_id=case_id,
            question=question,
            turns=prior_turns,
            disclosed_fact_ids=session_repo.get_disclosed_fact_ids(session),
            active_topic=session.active_topic,
        )
        if on_stage:
            on_stage("openai_response_complete")

    student_turn = transcript_repo.append_turn(
        session_id, ROLE_STUDENT, question,
        client_turn_id=client_turn_id, source="speech",
    )
    patient_turn = transcript_repo.append_turn(
        session_id, ROLE_PATIENT, result.text,
        client_turn_id=f"{client_turn_id}:patient", source="openai",
        model_name=result.model_name, prompt_version=PROMPT_VERSION,
        facts_used=result.used_fact_ids, response_type=result.response_type,
        validation_status=result.validation_status,
    )
    session_repo.add_disclosed_fact_ids(session, result.newly_disclosed_fact_ids)
    session_repo.set_active_topic(session, result.active_topic)
    db.commit()

    logger.info(
        "livekit_poc_turn_completed session_id=%s client_turn_id=%s case_id=%s",
        session_id, client_turn_id, case_id,
    )
    return PocTurnResult(
        student_turn_id=student_turn.id,
        patient_turn_id=patient_turn.id,
        patient_text=result.text,
        replayed=False,
    )


def synthesize_patient_audio_pcm(
    *, case_id: str, text: str, on_stage: StageCallback | None = None
) -> bytes | None:
    """SAME ElevenLabs client + SAME distributed TTS semaphore
    (app/core/concurrency.py tts_slot, Redis-backed) production's
    /synthesize endpoint uses (app/api/voice.py) - only the output format
    (raw PCM vs MP3) and transport (returned bytes vs HTTP StreamingResponse)
    differ. Returns None if the case has no configured ElevenLabs voice or no
    TTS capacity slot was available - mirrors VoiceNotAvailableError's two
    trigger conditions in api/voice.py. The caller (worker.py) decides what
    to do with None; this module never falls back to browser TTS itself -
    that would defeat the POC's purpose (see the mobile/LiveKit audits).
    """
    resolved = load_voice_profile(case_id)
    if not resolved.available:
        logger.warning("livekit_poc_voice_unavailable case_id=%s reason=%s", case_id, resolved.reason)
        return None

    mapped = map_speech_style(resolved.profile, None)
    voice_settings = mapped.to_elevenlabs()

    if on_stage:
        on_stage("tts_slot_wait_start")
    slot = tts_slot().acquire()
    if not slot.ok:
        logger.warning("livekit_poc_tts_no_capacity case_id=%s", case_id)
        return None
    try:
        # As with the OpenAI stages above, "tts_slot_acquired" and
        # "tts_request_start" are effectively the same instant here (only a
        # cheap get_elevenlabs_client() call sits between them).
        if on_stage:
            on_stage("tts_slot_acquired")
        client = get_elevenlabs_client()
        chunks = list(
            client.stream_speech(
                text=text,
                voice_id=resolved.profile.voice_id,
                model_id=resolved.model_id,
                voice_settings=voice_settings,
                output_format=LIVEKIT_PCM_OUTPUT_FORMAT,
            )
        )
        pcm = b"".join(chunks)
        if on_stage:
            on_stage("tts_response_complete")
        logger.info(
            "livekit_poc_tts_complete case_id=%s bytes=%d format=%s",
            case_id, len(pcm), LIVEKIT_PCM_OUTPUT_FORMAT,
        )
        return pcm
    finally:
        slot.release()
