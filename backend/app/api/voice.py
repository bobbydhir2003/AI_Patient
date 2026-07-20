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
from app.models import ConversationTurn
from app.schemas.voice_schema import VoiceStatusOut, VoiceSynthesizeRequest
from app.voice.audio_cache import get_audio_cache, make_cache_key
from app.voice.elevenlabs_client import ElevenLabsClient, get_elevenlabs_client, media_type_for
from app.voice.speech_style_mapper import map_speech_style
from app.voice.voice_profile_loader import load_voice_profile

logger = get_logger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])


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


def _resolve_approved_text(db: Session, payload: VoiceSynthesizeRequest) -> str:
    """Return the exact approved patient text to synthesize.

    When session_id + turn_id are provided (the normal path), the SAVED patient
    turn is authoritative: the stored content is synthesized, so this endpoint
    can never be used to voice arbitrary text. Without a turn reference, the
    raw text is allowed but strictly length-limited.
    """
    settings = get_settings()
    text = payload.text.strip()
    if not text:
        raise ValidationFailedError("There is no patient text to speak.")
    if len(text) > settings.elevenlabs_max_text_chars:
        raise ValidationFailedError("The requested text is too long to synthesize.")

    if payload.session_id and payload.turn_id:
        turn = db.get(ConversationTurn, payload.turn_id)
        if (
            turn is None
            or turn.session_id != payload.session_id
            or turn.role != ROLE_PATIENT
            or turn.session is None
            or turn.session.case_id != payload.case_id
        ):
            raise ValidationFailedError("The requested text does not match a saved patient response.")
        stored = turn.content.strip()
        if len(stored) > settings.elevenlabs_max_text_chars:
            raise ValidationFailedError("The requested text is too long to synthesize.")
        return stored  # authoritative: exactly what the transcript shows
    return text


@router.post("/synthesize")
def synthesize(
    payload: VoiceSynthesizeRequest,
    db: Session = Depends(get_db),
    client: ElevenLabsClient = Depends(get_elevenlabs_client),
) -> StreamingResponse:
    t_received = time.monotonic()  # backend request received (monotonic)
    settings = get_settings()
    resolved = load_voice_profile(payload.case_id)  # 404 for unknown cases
    if not resolved.available:
        logger.info("voice_unavailable case_id=%s reason=%s", payload.case_id, resolved.reason)
        raise VoiceNotAvailableError()

    text = _resolve_approved_text(db, payload)
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
    first_chunk = next(upstream)
    if settings.debug:
        logger.info(
            "tts_timing received_to_first_upstream_chunk_ms=%.0f case_id=%s turn=%s",
            (time.monotonic() - t_received) * 1000, payload.case_id,
            payload.correlation_id or "-",
        )

    def audio_stream() -> Iterator[bytes]:
        # Stream chunks to the browser as they arrive; buffer a copy so a
        # completed clip can be cached. A disconnected client stops the
        # generator (GeneratorExit) and nothing is cached.
        buffered: list[bytes] = [first_chunk]
        yield first_chunk
        try:
            for chunk in upstream:
                buffered.append(chunk)
                yield chunk
        except Exception:
            # Mid-stream upstream failure: headers already sent; end the body.
            # The browser's decode/ended handling treats it as a failed clip.
            logger.warning("tts_stream_aborted case_id=%s", payload.case_id)
            return
        cache.put(cache_key, b"".join(buffered))
        logger.info("tts_request_complete case_id=%s", payload.case_id)

    return StreamingResponse(audio_stream(), media_type=media_type, headers=headers)
