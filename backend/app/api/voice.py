"""Patient voice endpoints.

POST /api/voice/synthesize     -> streams ElevenLabs audio for approved patient text
GET  /api/voice/status/{case}  -> whether the realistic voice is available for a case

The ElevenLabs API key never leaves the backend. Voice IDs come only from the
backend case files - the frontend cannot supply or override them.
"""
import time
from collections.abc import Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import ROLE_PATIENT
from app.core.exceptions import ValidationFailedError, VoiceNotAvailableError
from app.core.logging import get_logger
from app.database.connection import get_db
from app.core.rate_limit import rate_limit
from app.dependencies.auth import get_current_user, user_can_access_session
from app.models import ConversationTurn, InterviewSession, User
from app.schemas.voice_schema import VoiceStatusOut, VoiceSynthesizeRequest
from app.voice.audio_cache import get_audio_cache, make_cache_key
from app.voice.elevenlabs_client import ElevenLabsClient, get_elevenlabs_client, media_type_for
from app.voice.speech_style_mapper import map_speech_style
from app.voice.voice_profile_loader import load_voice_profile

logger = get_logger(__name__)

# A1/A5: every voice route requires an authenticated account. The paid
# synthesis endpoint additionally enforces resource ownership and only ever
# synthesizes an authoritative stored patient turn (never arbitrary text).
router = APIRouter(prefix="/voice", tags=["voice"], dependencies=[Depends(get_current_user)])

_voice_rate_limit = rate_limit("voice", lambda s: s.voice_rate_limit)


@router.get("/status/{case_id}", response_model=VoiceStatusOut)
def voice_status(case_id: str) -> VoiceStatusOut:
    """Student-safe availability check (no voice IDs, no key material)."""
    resolved = load_voice_profile(case_id)  # 404 for unknown cases
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


@router.post("/synthesize", dependencies=[Depends(_voice_rate_limit)])
def synthesize(
    payload: VoiceSynthesizeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    client: ElevenLabsClient = Depends(get_elevenlabs_client),
) -> StreamingResponse:
    t_received = time.monotonic()  # backend request received (monotonic)
    settings = get_settings()

    # Multi-participant voice selection: the stored turn's speaker decides which
    # voice speaks. Camden -> child ("patient") voice; the mother -> "caregiver"
    # voice. A missing caregiver voice reports unavailable so the frontend falls
    # back to a browser adult voice (never Camden's child voice).
    speaker_key = "patient"
    if payload.session_id and payload.turn_id:
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
    speech = payload.speech_style.model_dump() if payload.speech_style else None
    mapped = map_speech_style(resolved.profile, speech)  # validates + clamps
    voice_settings = mapped.to_elevenlabs()
    output_format = settings.elevenlabs_output_format
    media_type = media_type_for(output_format)

    cache = get_audio_cache()
    cache_key = make_cache_key(
        resolved.profile.voice_id, resolved.model_id, text, voice_settings, output_format
    )
    headers = {
        "Cache-Control": "no-store",
        # Client-side pause before playback (clamped server-side).
        "X-Pause-Before-Ms": str(mapped.pause_before_ms),
    }

    cached = cache.get(cache_key)
    if cached is not None:
        logger.info("tts_cache_hit case_id=%s bytes=%d", payload.case_id, len(cached))
        return StreamingResponse(iter([cached]), media_type=media_type, headers=headers)

    # B5/B6: reserve a TTS slot. TTS is best-effort - if the server is at its
    # TTS concurrency limit we DEGRADE to browser text-to-speech (409) rather
    # than failing the interview turn. The slot is held for the stream's life.
    from app.core.concurrency import tts_slot as _tts_slot
    from app.core.telemetry import get_telemetry as _get_tele

    slot = _tts_slot().acquire()
    if not slot.ok:
        logger.info("tts_degraded_no_slot case_id=%s (browser fallback)", payload.case_id)
        raise VoiceNotAvailableError()
    if payload.session_id:
        _get_tele().live.set_status(payload.session_id, "STREAMING_AUDIO")

    logger.info(
        "tts_request_start case_id=%s provider=elevenlabs chars=%d speech=%s",
        payload.case_id, len(text), speech or "default",
    )

    if settings.debug:
        logger.info(
            "tts_timing validate_and_map_ms=%.0f case_id=%s turn=%s",
            (time.monotonic() - t_received) * 1000, payload.case_id,
            payload.correlation_id or "-",
        )

    upstream = client.stream_speech(
        text=text,
        voice_id=resolved.profile.voice_id,
        model_id=resolved.model_id,
        voice_settings=voice_settings,
        output_format=output_format,
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
            (time.monotonic() - t_received) * 1000, payload.case_id,
            payload.correlation_id or "-",
        )

    def audio_stream() -> Iterator[bytes]:
        # Stream chunks to the browser as they arrive; buffer a copy so a
        # completed clip can be cached. A disconnected client stops the
        # generator (GeneratorExit) and nothing is cached. The TTS slot is
        # released here (finally) whether the clip completes, fails or is cut off.
        buffered: list[bytes] = [first_chunk]
        try:
            yield first_chunk
            try:
                for chunk in upstream:
                    buffered.append(chunk)
                    yield chunk
            except Exception:
                # Mid-stream upstream failure: headers already sent; end the body.
                logger.warning("tts_stream_aborted case_id=%s", payload.case_id)
                return
            cache.put(cache_key, b"".join(buffered))
            logger.info("tts_request_complete case_id=%s", payload.case_id)
        finally:
            slot.release()

    return StreamingResponse(audio_stream(), media_type=media_type, headers=headers)
