"""Adapter: lets the LiveKit POC agent reuse EXACTLY the production patient-
response and TTS pipeline - no second prompt system, no second ElevenLabs
client, no bypass of either distributed concurrency semaphore.

Every function here calls the SAME modules app/services/interview_service.py
and app/api/voice.py already call:
    generate_patient_response   app/patient_engine/__init__.py
    speaker_router              app/patient_engine/speaker_router.py
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

Speaker/voice routing parity: this module resolves the SAME
speaker_router.resolve_for_case() decision interview_service.py's
send_student_message() uses, and the SAME speaker_router.participant_meta()
voice_key lookup - not a second, Camden-specific branch. A caregiver-primary
case (Camden today; any future case with the same case-file flag
automatically) resolves to its caregiver's voice_key ("caregiver"), and a
plain single-speaker case resolves to "patient" exactly as before this fix.
The LiveKit audio pipeline still only ever publishes ONE speaker's audio for
a given turn (no dual-track "both" support - see generate_and_persist_turn),
matching the persistent worker's existing single-speaker-per-turn design.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from app.core.concurrency import interview_slot, tts_slot
from app.core.constants import PROMPT_VERSION, ROLE_PATIENT, ROLE_STUDENT
from app.core.logging import get_logger
from app.patient_engine import case_loader, generate_patient_response, speaker_router
from app.repositories.session_repository import SessionRepository
from app.repositories.transcript_repository import TranscriptRepository
from app.voice.audio_cache import get_audio_cache, make_cache_key
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
    # The resolved participant's voice_key ("patient" or "caregiver" today -
    # see speaker_router.participant_meta) - carried forward so the caller
    # (worker.py) can pass it to synthesize_patient_audio_pcm without
    # re-deriving speaker routing from the original question a second time.
    voice_key: str
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
    """Reuses interview_service.send_student_message's core steps, INCLUDING
    its speaker_router.resolve_for_case() routing decision - so a
    caregiver-primary case (Camden today) generates the caregiver's response
    here exactly as it does in legacy mode, not the case-agnostic default.
    The one deliberate scope limit kept from the original POC: this module
    only ever publishes ONE speaker's audio per turn, so a (currently
    unreachable - see below) SPEAKER_BOTH resolution degrades to the case's
    primary/default speaker rather than generating two responses - LiveKit's
    single continuous AudioSource has no dual-track model, and adding one
    is out of scope for this fix (see worker.py's PocAgentSession docstring).
    Idempotent on client_turn_id, identically to production: a
    duplicate/retried call for the same logical turn replays the existing
    turn instead of generating (and billing) a second time.
    """
    session_repo = SessionRepository(db)
    transcript_repo = TranscriptRepository(db)

    session = session_repo.get(session_id)
    if session is None:
        raise LiveKitPocSessionNotFoundError(session_id)

    case = case_loader.load_case(case_id)

    existing_student = transcript_repo.get_by_client_turn_id(session_id, client_turn_id)
    if existing_student is not None:
        existing_patient = transcript_repo.get_by_index(session_id, existing_student.turn_index + 1)
        if existing_patient is not None and existing_patient.role == ROLE_PATIENT:
            logger.info(
                "livekit_poc_turn_replayed session_id=%s client_turn_id=%s", session_id, client_turn_id,
            )
            # Reuse the ALREADY-persisted speaker decision (not a fresh
            # routing call) - the authoritative record of who spoke this turn
            # is the saved turn itself, not a re-derivation from the original
            # question text.
            _, _, voice_key = speaker_router.participant_meta(
                case, existing_patient.speaker_id or "patient"
            )
            return PocTurnResult(
                student_turn_id=existing_student.id,
                patient_turn_id=existing_patient.id,
                patient_text=existing_patient.content,
                voice_key=voice_key,
                replayed=True,
            )

    prior_turns = transcript_repo.list_turns(session_id)

    # SAME deterministic routing decision send_student_message() makes,
    # BEFORE any model call (see speaker_router.py's module docstring) - a
    # single-speaker case always resolves to "patient" (no behavior change),
    # a caregiver-primary case (Camden) always resolves to its locked primary
    # (the mother), and the generic dynamic router is preserved unchanged for
    # any future multi-participant case that isn't caregiver-locked.
    routing = speaker_router.resolve_for_case(case, question, prior_turns)
    resolved_speaker = routing.speaker
    if resolved_speaker == speaker_router.SPEAKER_BOTH:
        resolved_speaker = (
            getattr(case, "primary_speaker", "")
            or getattr(case, "default_speaker", "")
            or speaker_router.SPEAKER_MOTHER
        )
        logger.warning(
            "livekit_poc_speaker_both_unsupported case_id=%s resolved_to=%s",
            case_id, resolved_speaker,
        )

    # None for single-speaker cases -> identical prompt/behavior to before
    # this fix (parity with interview_service.py's send_student_message).
    engine_speaker = resolved_speaker if speaker_router.is_multi_participant(case) else None

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
            speaker_id=engine_speaker,
        )
        if on_stage:
            on_stage("openai_response_complete")

    eff_speaker_id, speaker_label, voice_key = speaker_router.participant_meta(case, resolved_speaker)

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
        speaker_id=eff_speaker_id, speaker_label=speaker_label,
    )
    session_repo.add_disclosed_fact_ids(session, result.newly_disclosed_fact_ids)
    session_repo.set_active_topic(session, result.active_topic)
    db.commit()

    logger.info(
        "livekit_poc_turn_completed session_id=%s client_turn_id=%s case_id=%s speaker_id=%s voice_key=%s",
        session_id, client_turn_id, case_id, eff_speaker_id, voice_key,
    )
    return PocTurnResult(
        student_turn_id=student_turn.id,
        patient_turn_id=patient_turn.id,
        patient_text=result.text,
        voice_key=voice_key,
        replayed=False,
    )


# Phase 5B: splits on whitespace following a sentence-ending punctuation
# mark. Deliberately simple (Step 2: smallest architecture that gives a
# REAL, provable text-to-audio boundary, not a timer-based approximation) -
# known limitation: does not special-case abbreviations ("Dr. Smith" would
# split into two "sentences"). This only affects sentence GROUPING for the
# purpose of TTS/publish/spoken-tracking granularity - never the actual
# generated text content, which is unaffected either way.
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")


def split_into_sentences(text: str) -> list[str]:
    """Phase 5B (Step 2): the ONE function establishing sentence-level
    granularity for spoken-content tracking - worker.py calls
    synthesize_patient_audio_pcm/publishes audio once PER element of this
    list, instead of once for the whole response, so each element gets a
    genuine "did this sentence's audio finish publishing" boundary. Never
    returns an empty list for non-empty input - text with no
    sentence-ending punctuation at all (e.g. a short "Okay.") still comes
    back as a single-element list."""
    stripped = text.strip()
    if not stripped:
        return []
    parts = [p.strip() for p in _SENTENCE_BOUNDARY_RE.split(stripped) if p.strip()]
    return parts or [stripped]


def finalize_partial_patient_delivery(
    db: Session, *, patient_turn_id: str, spoken_text: str, reason: str
) -> None:
    """Phase 5B (Step 4/9): the ONE corrective UPDATE - called ONLY when a
    patient turn's audio delivery was cut short (student interruption, or a
    mid-response TTS failure) AFTER generate_and_persist_turn already
    committed the FULL generated text (see that function's docstring - its
    own insert/commit timing and idempotency are completely unchanged by
    this function's existence). Rewrites the ALREADY-persisted row's
    content down to ONLY the text whose audio genuinely finished publishing
    (Step 5) - never the full generated text, never a time-based guess.
    `reason` becomes the row's validation_status (Step 11: reuses the
    EXISTING column, no schema migration) - e.g. "interrupted" or
    "delivery_failed", so admin/debugging tooling can distinguish a
    delivery-truncated turn from a normally-completed one. A no-op (never
    raises) if the turn id no longer exists."""
    repo = TranscriptRepository(db)
    updated = repo.mark_delivery_status(patient_turn_id, content=spoken_text, validation_status=reason)
    if updated:
        db.commit()


def resolve_backchannel_voice_key(case_id: str) -> str | None:
    """Phase 6 (Step 11): resolves the voice a patient BACKCHANNEL should
    use, WITHOUT the student's actual question text (a backchannel plays
    during a HOLD pause, before the question is even complete). Only
    returns a value when the case's speaker is deterministically knowable
    in advance:
      - single-speaker cases (e.g. Carly): always "patient".
      - caregiver-primary-locked multi-speaker cases (e.g. Camden):
        speaker_router.resolve_for_case's OWN caregiver-primary-lock branch
        already ignores the question/turns entirely and always returns the
        SAME primary speaker - safe to call with empty question/turns.
      - any OTHER multi-participant case (none exist in this codebase
        today, but the generic dynamic router in speaker_router.route()
        genuinely depends on question content) - returns None. Never
        guesses; the caller skips the backchannel entirely rather than
        risk the wrong patient's voice (Step 11's explicit requirement)."""
    case = case_loader.load_case(case_id)
    if speaker_router.is_multi_participant(case) and not getattr(case, "caregiver_primary_only", False):
        return None
    routing = speaker_router.resolve_for_case(case, "", [])
    _, _, voice_key = speaker_router.participant_meta(case, routing.speaker)
    return voice_key


def synthesize_backchannel_audio_pcm(*, case_id: str, voice_key: str, phrase: str) -> bytes | None:
    """Phase 6 (Step 10): the SAME voice-resolution/TTS path
    synthesize_patient_audio_pcm uses below, with ONE addition - a
    process-level, bounded LRU cache (app.voice.audio_cache, the SAME
    infrastructure the legacy /synthesize endpoint already uses) keyed on
    (voice_id, model_id, phrase, settings, format). Safe to share across
    ANY session/interview process-wide: unlike everything else this module
    ever caches, a backchannel phrase's audio is a PURE function of
    voice+text - no session-derived content, no student data. First
    request for a given voice+phrase pays the real ElevenLabs latency
    (cold start); every later request, from any session, is instant.

    Returns None (never raises) on ANY failure - unavailable voice, no TTS
    capacity, provider error - matching synthesize_patient_audio_pcm's own
    contract. The caller treats None exactly like "skip this backchannel"
    (Step 20: fail-open, never anything more disruptive)."""
    resolved = load_voice_profile(case_id, speaker_id=voice_key)
    if not resolved.available:
        logger.warning(
            "patient_backchannel_voice_unavailable case_id=%s voice_key=%s reason=%s",
            case_id, voice_key, resolved.reason,
        )
        return None

    mapped = map_speech_style(resolved.profile, None)
    voice_settings = mapped.to_elevenlabs()
    cache_key = make_cache_key(
        resolved.profile.voice_id, resolved.model_id, phrase, voice_settings, LIVEKIT_PCM_OUTPUT_FORMAT,
    )
    cache = get_audio_cache()
    cached = cache.get(cache_key)
    if cached is not None:
        logger.info(
            "patient_backchannel_cache_hit case_id=%s voice_key=%s phrase=%r bytes=%d",
            case_id, voice_key, phrase, len(cached),
        )
        return cached

    slot = tts_slot().acquire()
    if not slot.ok:
        logger.warning("patient_backchannel_tts_no_capacity case_id=%s voice_key=%s", case_id, voice_key)
        return None
    try:
        client = get_elevenlabs_client()
        chunks = list(
            client.stream_speech(
                text=phrase,
                voice_id=resolved.profile.voice_id,
                model_id=resolved.model_id,
                voice_settings=voice_settings,
                output_format=LIVEKIT_PCM_OUTPUT_FORMAT,
            )
        )
        pcm = b"".join(chunks)
        cache.put(cache_key, pcm)
        logger.info(
            "patient_backchannel_cache_miss case_id=%s voice_key=%s phrase=%r bytes=%d",
            case_id, voice_key, phrase, len(pcm),
        )
        return pcm
    except Exception:
        logger.exception(
            "patient_backchannel_synthesis_failed case_id=%s voice_key=%s phrase=%r",
            case_id, voice_key, phrase,
        )
        return None
    finally:
        slot.release()


def synthesize_patient_audio_pcm(
    *, case_id: str, text: str, voice_key: str = "patient", on_stage: StageCallback | None = None
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

    `voice_key` is the resolved participant's voice key ("patient" or
    "caregiver" today - see speaker_router.participant_meta), NOT the
    conversational speaker_id ("mother") - they are deliberately kept
    distinct (see generate_and_persist_turn, the caller's only caller via
    PocTurnResult.voice_key). Defaults to "patient" for any caller that
    doesn't yet resolve a speaker (kept only as a safety default; both real
    call sites in this codebase now always pass an explicit voice_key).
    load_voice_profile's own case-file-speaker gate (see
    voice_profile_loader.py) is what actually enforces that a caregiver-
    primary case's child voice_key can never be requested - passing the
    wrong voice_key here simply reports "unavailable", exactly as it did
    before this function accepted the parameter.
    """
    resolved = load_voice_profile(case_id, speaker_id=voice_key)
    if not resolved.available:
        logger.warning(
            "livekit_poc_voice_unavailable case_id=%s voice_key=%s reason=%s",
            case_id, voice_key, resolved.reason,
        )
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
            "livekit_poc_tts_complete case_id=%s voice_key=%s bytes=%d format=%s",
            case_id, voice_key, len(pcm), LIVEKIT_PCM_OUTPUT_FORMAT,
        )
        return pcm
    finally:
        slot.release()
