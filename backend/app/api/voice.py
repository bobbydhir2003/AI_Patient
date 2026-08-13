"""Patient voice endpoints.

POST /api/voice/synthesize     -> streams ElevenLabs audio for approved patient text
GET  /api/voice/status/{case}  -> whether the realistic voice is available for a case

The ElevenLabs API key never leaves the backend. Voice IDs come only from the
backend case files - the frontend cannot supply or override them.

Database rule for /synthesize (Priority 4 in the scalability audit): no DB
connection may remain checked out while waiting on the ElevenLabs network call
or while streaming audio back to the browser. Mirrors the pattern already used
by the SSE interview-streaming endpoint (app/services/interview_stream_service.py):
one short-lived session resolves auth + the approved text + voice config and is
closed BEFORE the TTS slot is acquired or the provider is called; a second
short-lived session (opened AFTER a real provider response is confirmed)
records usage. Neither session's lifetime overlaps the network/streaming work.
"""
import time
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import ROLE_PATIENT
from app.core.exceptions import ValidationFailedError, VoiceNotAvailableError
from app.core.logging import get_logger
from app.database.connection import get_db_factory
from app.core.rate_limit import rate_limit
from app.dependencies.auth import get_current_user, load_user_from_token, user_can_access_session
from app.models import ConversationTurn, InterviewSession, User
from app.schemas.voice_schema import VoiceStatusOut, VoiceSynthesizeRequest
from app.voice.audio_cache import get_audio_cache, make_cache_key
from app.voice.elevenlabs_client import ElevenLabsClient, get_elevenlabs_client, media_type_for
from app.voice.speech_style_mapper import map_speech_style
from app.voice.voice_profile_loader import load_voice_profile

logger = get_logger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])

_voice_rate_limit = rate_limit("voice", lambda s: s.voice_rate_limit)


@router.get("/status/{case_id}", response_model=VoiceStatusOut, dependencies=[Depends(get_current_user)])
def voice_status(case_id: str) -> VoiceStatusOut:
    """Student-safe availability check (no voice IDs, no key material). For a
    caregiver-primary case (Camden) the sole spoken voice is the caregiver, so
    availability reflects the mother's voice, never the unused child voice."""
    from app.patient_engine import case_loader
    from app.voice.voice_profile_loader import case_file_speaker

    resolved = load_voice_profile(case_id, case_file_speaker(case_loader.load_case(case_id)))  # 404 for unknown cases
    return VoiceStatusOut(
        case_id=case_id,
        available=resolved.available,
        provider="elevenlabs" if resolved.available else "browser",
        fallback_rate=max(0.5, min(1.5, resolved.profile.fallback_rate)),
    )


def _resolve_approved_text(
    db: Session, payload: VoiceSynthesizeRequest, current_user: User
) -> str:
    """Return the exact approved patient text to synthesize.

    A5 - server-side text authority + ownership. This endpoint is ONLY for
    interview playback. Two modes, and in BOTH the caller must be authenticated
    AND own (or admin) the referenced session, so the backend can never be used
    as a general, anonymous ElevenLabs proxy:

    1. Committed playback (session_id + turn_id): the SAVED patient turn is
       authoritative - its stored content is synthesized, the request body's
       `text` is ignored, and only PATIENT turns are voiced (never a student
       turn). This is the normal, preferred path.

    2. Live sentence streaming (session_id only): during a streamed reply the
       model's sentences are spoken as they arrive, BEFORE the turn is committed,
       so there is no turn_id yet. The sentence text is accepted but ONLY within
       a session the caller owns, strictly length-capped, and rate-limited. A
       request with no session reference at all is rejected.

    The separate admin voice-preview flow lives in the admin router and is
    protected by admin permissions.
    """
    settings = get_settings()

    if not payload.session_id:
        # No session context => would be an arbitrary-text proxy. Rejected.
        raise ValidationFailedError("A patient interview session is required for playback.")

    # Ownership first: never reveal whether another user's session/turn exists.
    session = db.get(InterviewSession, payload.session_id)
    if (
        session is None
        or not user_can_access_session(current_user, session)
        or session.case_id != payload.case_id
    ):
        raise ValidationFailedError("The requested text does not match a saved patient response.")

    if payload.turn_id:
        # Mode 1: authoritative stored patient turn.
        turn = db.get(ConversationTurn, payload.turn_id)
        if turn is None or turn.session_id != payload.session_id or turn.role != ROLE_PATIENT:
            raise ValidationFailedError("The requested text does not match a saved patient response.")
        stored = turn.content.strip()
        if not stored:
            raise ValidationFailedError("There is no patient text to speak.")
        if len(stored) > settings.elevenlabs_max_text_chars:
            raise ValidationFailedError("The requested text is too long to synthesize.")
        return stored  # exactly what the transcript shows

    # Mode 2: live streaming sentence within an owned session (length-capped).
    text = payload.text.strip()
    if not text:
        raise ValidationFailedError("There is no patient text to speak.")
    if len(text) > settings.elevenlabs_max_text_chars:
        raise ValidationFailedError("The requested text is too long to synthesize.")
    return text


@dataclass
class _SynthContext:
    """Everything the provider call + audio stream needs, as PLAIN values (no
    ORM objects tied to a session) - captured before the short-lived context
    session is closed."""

    text: str
    voice_id: str
    model_id: str
    voice_settings: dict
    output_format: str
    media_type: str
    pause_before_ms: int
    cache_key: str
    case_id: str
    session_id: str | None
    student_id: str | None
    speech: dict | None


def _load_synth_context(
    session_factory, request: Request, payload: VoiceSynthesizeRequest
) -> _SynthContext:
    """Short transaction: authenticate, authorize, resolve the approved text
    + voice config, and copy everything downstream needs into plain values.
    Closed BEFORE the TTS slot is acquired or ElevenLabs is called - no DB
    connection is held during provider network I/O or audio streaming."""
    from app.dependencies.auth import _extract_bearer_token
    from app.patient_engine import case_loader

    token = _extract_bearer_token(request)
    db = session_factory()
    try:
        current_user = load_user_from_token(db, token)

        # Multi-participant voice selection: the stored turn's speaker decides
        # which voice speaks. Camden -> child ("patient") voice; the mother ->
        # "caregiver" voice. A missing caregiver voice reports unavailable so
        # the frontend falls back to a browser adult voice (never Camden's
        # child voice). Caregiver-primary cases (Camden): EVERY patient turn is
        # the caregiver, so always use the caregiver voice - for committed
        # playback AND for the live streamed sentences that are spoken before
        # the turn is committed (no turn_id yet). This guarantees the child's
        # voice is never used.
        speaker_key = "patient"
        case = case_loader.load_case(payload.case_id)  # 404 for unknown cases
        if getattr(case, "caregiver_primary_only", False):
            speaker_key = "caregiver"
        elif payload.session_id and payload.turn_id:
            turn = db.get(ConversationTurn, payload.turn_id)
            if turn is not None and getattr(turn, "speaker_id", "patient") == "mother":
                speaker_key = "caregiver"

        resolved = load_voice_profile(payload.case_id, speaker_key)  # 404 for unknown cases
        if not resolved.available:
            logger.info(
                "voice_unavailable case_id=%s speaker=%s reason=%s",
                payload.case_id, speaker_key, resolved.reason,
            )
            raise VoiceNotAvailableError()

        text = _resolve_approved_text(db, payload, current_user)

        session = db.get(InterviewSession, payload.session_id) if payload.session_id else None
        student_id = session.student_id if session else None
    finally:
        db.close()

    settings = get_settings()
    speech = payload.speech_style.model_dump() if payload.speech_style else None
    mapped = map_speech_style(resolved.profile, speech)  # validates + clamps
    voice_settings = mapped.to_elevenlabs()
    output_format = settings.elevenlabs_output_format
    cache_key = make_cache_key(
        resolved.profile.voice_id, resolved.model_id, text, voice_settings, output_format
    )
    return _SynthContext(
        text=text,
        voice_id=resolved.profile.voice_id,
        model_id=resolved.model_id,
        voice_settings=voice_settings,
        output_format=output_format,
        media_type=media_type_for(output_format),
        pause_before_ms=mapped.pause_before_ms,
        cache_key=cache_key,
        case_id=payload.case_id,
        session_id=payload.session_id or None,
        student_id=student_id,
        speech=speech,
    )


@router.post("/synthesize", dependencies=[Depends(_voice_rate_limit)])
def synthesize(
    payload: VoiceSynthesizeRequest,
    request: Request,
    session_factory=Depends(get_db_factory),
    client: ElevenLabsClient = Depends(get_elevenlabs_client),
) -> StreamingResponse:
    t_received = time.monotonic()  # backend request received (monotonic)
    settings = get_settings()

    # ---- Short transaction: auth + authorize + resolve approved text/voice.
    # Closed before any TTS slot is reserved or provider call is made.
    ctx = _load_synth_context(session_factory, request, payload)

    headers = {
        "Cache-Control": "no-store",
        # Client-side pause before playback (clamped server-side).
        "X-Pause-Before-Ms": str(ctx.pause_before_ms),
    }
    cache = get_audio_cache()
    cached = cache.get(ctx.cache_key)
    if cached is not None:
        logger.info("tts_cache_hit case_id=%s bytes=%d", ctx.case_id, len(cached))
        return StreamingResponse(iter([cached]), media_type=ctx.media_type, headers=headers)

    # B5/B6: reserve a TTS slot. TTS is best-effort - if the server is at its
    # TTS concurrency limit we DEGRADE to browser text-to-speech (409) rather
    # than failing the interview turn. The slot is held for the stream's life.
    from app.core.concurrency import tts_slot as _tts_slot
    from app.core.telemetry import get_telemetry as _get_tele

    slot = _tts_slot().acquire()
    if not slot.ok:
        logger.info("tts_browser_fallback case_id=%s reason=no_capacity_slot", ctx.case_id)
        raise VoiceNotAvailableError()
    if ctx.session_id:
        _get_tele().live.set_status(ctx.session_id, "STREAMING_AUDIO")

    logger.info(
        "tts_request_start case_id=%s provider=elevenlabs chars=%d speech=%s",
        ctx.case_id, len(ctx.text), ctx.speech or "default",
    )

    if settings.debug:
        logger.info(
            "tts_timing validate_and_map_ms=%.0f case_id=%s turn=%s",
            (time.monotonic() - t_received) * 1000, ctx.case_id,
            payload.correlation_id or "-",
        )

    upstream = client.stream_speech(
        text=ctx.text,
        voice_id=ctx.voice_id,
        model_id=ctx.model_id,
        voice_settings=ctx.voice_settings,
        output_format=ctx.output_format,
    )
    # Pull the FIRST chunk eagerly: connect/auth/timeout failures raise here,
    # BEFORE response headers are sent, so the frontend gets a clean 502
    # (VoiceSynthesisError) it can catch and fall back on. This reads exactly
    # one chunk - never more - so it does not delay or buffer the stream.
    try:
        first_chunk = next(upstream)
    except BaseException:
        slot.release()  # never leak a TTS slot on an early failure
        raise
    if settings.debug:
        logger.info(
            "tts_timing received_to_first_upstream_chunk_ms=%.0f case_id=%s turn=%s",
            (time.monotonic() - t_received) * 1000, ctx.case_id,
            payload.correlation_id or "-",
        )

    # A real ElevenLabs synthesis request is now confirmed (cache MISS + the
    # provider produced audio past auth/connect). Record character usage ONCE,
    # best-effort - a cache hit returned earlier and is never billed here. A
    # NEW short-lived session is opened here (after the provider call, before
    # streaming begins) - never the context session, which is already closed.
    try:
        from app.services import usage_recorder

        usage_db = session_factory()
        try:
            usage_recorder.record_elevenlabs_usage(
                usage_db,
                session_id=ctx.session_id,
                student_id=ctx.student_id,
                case_id=ctx.case_id,
                characters=len(ctx.text),
                voice_id=ctx.voice_id,
                model_id=ctx.model_id,
            )
        finally:
            usage_db.close()
    except Exception:  # telemetry must never affect playback
        pass

    def audio_stream() -> Iterator[bytes]:
        # Stream chunks to the browser as they arrive; buffer a copy so a
        # completed clip can be cached. A disconnected client stops the
        # generator (GeneratorExit) and nothing is cached. The TTS slot is
        # released here (finally) whether the clip completes, fails or is cut
        # off. No DB session is referenced anywhere in this generator - it
        # runs entirely after both short-lived sessions above were closed.
        buffered: list[bytes] = [first_chunk]
        try:
            yield first_chunk
            try:
                for chunk in upstream:
                    buffered.append(chunk)
                    yield chunk
            except Exception:
                # Mid-stream upstream failure: headers already sent; end the body.
                logger.warning("tts_stream_aborted case_id=%s", ctx.case_id)
                return
            cache.put(ctx.cache_key, b"".join(buffered))
            logger.info("tts_request_complete case_id=%s", ctx.case_id)
        finally:
            slot.release()

    return StreamingResponse(audio_stream(), media_type=ctx.media_type, headers=headers)
