"""Technical System Dashboard endpoints (admin-only).

Every route is protected by `require_admin`. Values come from real runtime
checks or real recorded activity; no secrets are ever returned.
"""
import time
from collections.abc import Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import (
    AUDIT_AUDIO_CACHE_CLEARED,
    AUDIT_VOICE_PREVIEWED,
    VOICE_PREVIEW_SAMPLES,
)
from app.core.exceptions import ValidationFailedError, VoiceNotAvailableError
from app.core.logging import get_logger
from app.database.connection import get_db
from app.dependencies.auth import require_admin
from app.models import User
from app.repositories.audit_repository import AuditRepository
from app.schemas.system_schema import (
    MutationResultOut,
    SystemOverviewOut,
    VoiceListOut,
)
from app.services import system_service
from app.voice.audio_cache import get_audio_cache, make_cache_key
from app.voice.elevenlabs_client import ElevenLabsClient, get_elevenlabs_client, media_type_for
from app.voice.speech_style_mapper import map_speech_style
from app.voice.voice_profile_loader import case_file_speaker, load_voice_profile

logger = get_logger(__name__)

router = APIRouter(
    prefix="/admin/system",
    tags=["admin-system"],
    dependencies=[Depends(require_admin)],
)


@router.get("/overview", response_model=SystemOverviewOut)
def system_overview(db: Session = Depends(get_db)) -> SystemOverviewOut:
    started = time.perf_counter()
    return system_service.build_overview(db, started)


@router.get("/voices", response_model=VoiceListOut)
def system_voices(db: Session = Depends(get_db)) -> VoiceListOut:
    return VoiceListOut(voices=system_service.list_voices(db))


@router.post("/voices/{case_id}/preview")
def preview_voice(
    case_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    client: ElevenLabsClient = Depends(get_elevenlabs_client),
) -> StreamingResponse:
    """Synthesize a FIXED, safe sample sentence with the case's real voice.

    Uses only a predefined sample (never free-form text). Returns real audio on
    success or an honest error (voice not available / not configured)."""
    # Preview the case's sole spoken voice: the caregiver (mother) for a
    # caregiver-primary case like Camden, else the patient.
    from app.patient_engine import case_loader

    resolved = load_voice_profile(
        case_id, case_file_speaker(case_loader.load_case(case_id))
    )  # 404 for unknown case ids
    sample = VOICE_PREVIEW_SAMPLES.get(case_id)
    if not sample:
        raise ValidationFailedError("No preview sample is defined for this case.")
    if not resolved.available:
        logger.info("voice_preview_unavailable case_id=%s reason=%s", case_id, resolved.reason)
        raise VoiceNotAvailableError()

    settings = get_settings()
    mapped = map_speech_style(resolved.profile, None)
    voice_settings = mapped.to_elevenlabs()
    output_format = settings.elevenlabs_output_format
    media_type = media_type_for(output_format)
    cache = get_audio_cache()
    cache_key = make_cache_key(
        resolved.profile.voice_id, resolved.model_id, sample, voice_settings, output_format
    )
    headers = {"Cache-Control": "no-store", "X-Pause-Before-Ms": str(mapped.pause_before_ms)}

    AuditRepository(db).record(
        admin_user_id=admin.id,
        admin_email=admin.email,
        action_type=AUDIT_VOICE_PREVIEWED,
        record_type="voice",
        record_id=case_id,
        description=f"Previewed patient voice for {case_id}",
    )
    db.commit()

    cached = cache.get(cache_key)
    if cached is not None:
        return StreamingResponse(iter([cached]), media_type=media_type, headers=headers)

    upstream = client.stream_speech(
        text=sample,
        voice_id=resolved.profile.voice_id,
        model_id=resolved.model_id,
        voice_settings=voice_settings,
        output_format=output_format,
    )
    first_chunk = next(upstream)  # surface connect/auth failures before headers

    def audio_stream() -> Iterator[bytes]:
        buffered: list[bytes] = [first_chunk]
        yield first_chunk
        try:
            for chunk in upstream:
                buffered.append(chunk)
                yield chunk
        except Exception:
            logger.warning("voice_preview_stream_aborted case_id=%s", case_id)
            return
        cache.put(cache_key, b"".join(buffered))

    return StreamingResponse(audio_stream(), media_type=media_type, headers=headers)


@router.post("/audio-cache/clear", response_model=MutationResultOut)
def clear_audio_cache(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> MutationResultOut:
    """Clear the in-memory synthesized-audio cache (safe, non-destructive)."""
    cache = get_audio_cache()
    cleared = len(cache)
    cache.clear()
    AuditRepository(db).record(
        admin_user_id=admin.id,
        admin_email=admin.email,
        action_type=AUDIT_AUDIO_CACHE_CLEARED,
        record_type="system",
        record_id="audio_cache",
        description=f"Cleared audio cache ({cleared} entries)",
    )
    db.commit()
    return MutationResultOut(success=True, message=f"Audio cache cleared ({cleared} entries).")
