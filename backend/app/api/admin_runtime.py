"""Editable runtime configuration endpoints.

Access tiers (enforced here, not just in the UI):
  - admin        : view/edit AI settings and patient voices, run connection tests
  - super_admin  : replace/remove API keys, restore configuration versions
"""
from collections.abc import Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.constants import (
    AUDIT_AI_CONFIG_UPDATED,
    AUDIT_CREDENTIAL_REMOVED,
    AUDIT_CREDENTIAL_REPLACED,
    AUDIT_CREDENTIAL_TESTED,
    AUDIT_VOICE_RESTORED,
    AUDIT_VOICE_UPDATED,
)
from app.core.exceptions import ValidationFailedError, VoiceNotAvailableError
from app.core.logging import get_logger
from app.database.connection import get_db
from app.dependencies.auth import require_admin, require_super_admin
from app.models import User
from app.repositories.audit_repository import AuditRepository
from app.schemas.runtime_schema import (
    ApplyResult,
    ConversationPatchIn,
    CredentialListOut,
    CredentialReplaceIn,
    CredentialStatusOut,
    ElevenLabsConfigPatchIn,
    HistoryListOut,
    OpenAIConfigPatchIn,
    TestResultOut,
    VoiceListOut,
    VoicePatchIn,
    VoiceRowOut,
)
from app.services import runtime_config_service as rc
from app.voice.audio_cache import get_audio_cache, make_cache_key
from app.voice.elevenlabs_client import ElevenLabsClient, get_elevenlabs_client, media_type_for
from app.voice.speech_style_mapper import map_speech_style

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/runtime", tags=["admin-runtime"])


def _audit(db, admin, action, record_type, record_id, description):
    AuditRepository(db).record(
        admin_user_id=admin.id, admin_email=admin.email, action_type=action,
        record_type=record_type, record_id=record_id, description=description,
    )


# ------------------------------ credentials (super_admin to write) ------------
@router.get("/credentials", response_model=CredentialListOut, dependencies=[Depends(require_admin)])
def get_credentials(db: Session = Depends(get_db)) -> CredentialListOut:
    return CredentialListOut(
        credentials=[CredentialStatusOut.model_validate(c) for c in rc.credential_status(db)]
    )


def _replace_credential(service, payload, admin, db) -> ApplyResult:
    rc.set_credential(db, service=service, new_key=payload.key, admin_email=admin.email)
    _audit(db, admin, AUDIT_CREDENTIAL_REPLACED, "credential", service,
           f"Replaced {service} API key")
    db.commit()
    return ApplyResult(success=True, apply_mode="immediate",
                       message=f"{service} key stored securely. Applies to the next request.")


@router.post("/credentials/openai", response_model=ApplyResult)
def replace_openai_key(payload: CredentialReplaceIn, admin: User = Depends(require_super_admin),
                       db: Session = Depends(get_db)) -> ApplyResult:
    return _replace_credential("openai", payload, admin, db)


@router.post("/credentials/elevenlabs", response_model=ApplyResult)
def replace_elevenlabs_key(payload: CredentialReplaceIn, admin: User = Depends(require_super_admin),
                           db: Session = Depends(get_db)) -> ApplyResult:
    return _replace_credential("elevenlabs", payload, admin, db)


@router.delete("/credentials/{service}", response_model=ApplyResult)
def remove_key(service: str, admin: User = Depends(require_super_admin),
               db: Session = Depends(get_db)) -> ApplyResult:
    rc.remove_credential(db, service=service, admin_email=admin.email)
    _audit(db, admin, AUDIT_CREDENTIAL_REMOVED, "credential", service, f"Removed {service} API key")
    db.commit()
    return ApplyResult(success=True, apply_mode="immediate", message=f"{service} key removed.")


def _test_credential(service, admin, db) -> TestResultOut:
    creds = {c["service"]: c for c in rc.credential_status(db)}
    if not creds.get(service, {}).get("configured"):
        return TestResultOut(service=service, status="not_configured", message="No key configured.")
    # Decrypt/resolve the ACTIVE key (never returned to the client) and test it.
    if service == "openai":
        key = rc._active_key(db, "openai", "")
        status, msg = rc.test_openai_key(key)
    else:
        key = rc._active_key(db, "elevenlabs", "")
        status, msg = rc.test_elevenlabs_key(key)
    rc.record_test_result(db, service=service, status=status, message=msg)
    _audit(db, admin, AUDIT_CREDENTIAL_TESTED, "credential", service,
           f"Tested {service} connection: {status}")
    db.commit()
    return TestResultOut(service=service, status=status, message=msg)


@router.post("/credentials/openai/test", response_model=TestResultOut, dependencies=[Depends(require_admin)])
def test_openai(admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> TestResultOut:
    return _test_credential("openai", admin, db)


@router.post("/credentials/elevenlabs/test", response_model=TestResultOut, dependencies=[Depends(require_admin)])
def test_elevenlabs(admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> TestResultOut:
    return _test_credential("elevenlabs", admin, db)


# ------------------------------ AI configuration (admin) ----------------------
@router.get("/ai-configuration", dependencies=[Depends(require_admin)])
def get_ai_config(db: Session = Depends(get_db)) -> dict:
    return rc.ai_configuration(db)


@router.patch("/ai-configuration/openai", response_model=ApplyResult)
def patch_openai(payload: OpenAIConfigPatchIn, admin: User = Depends(require_admin),
                 db: Session = Depends(get_db)) -> ApplyResult:
    patch = payload.model_dump(exclude_none=True)
    rc.set_openai_config(db, admin_email=admin.email, patch=patch)
    _audit(db, admin, AUDIT_AI_CONFIG_UPDATED, "openai_config", "openai",
           f"Updated OpenAI config: {', '.join(patch.keys())}")
    db.commit()
    mode = "new_sessions" if ("model" in patch or "max_output_tokens" in patch) else "immediate"
    return ApplyResult(success=True, apply_mode=mode,
                       message="Saved. Model/token changes apply to new interview sessions.")


@router.patch("/ai-configuration/elevenlabs", response_model=ApplyResult)
def patch_elevenlabs(payload: ElevenLabsConfigPatchIn, admin: User = Depends(require_admin),
                     db: Session = Depends(get_db)) -> ApplyResult:
    patch = payload.model_dump(exclude_none=True)
    rc.set_elevenlabs_config(db, admin_email=admin.email, patch=patch)
    _audit(db, admin, AUDIT_AI_CONFIG_UPDATED, "elevenlabs_config", "elevenlabs",
           f"Updated ElevenLabs config: {', '.join(patch.keys())}")
    db.commit()
    return ApplyResult(success=True, apply_mode="immediate", message="Saved. Applies to the next TTS request.")


@router.patch("/ai-configuration/conversation", response_model=ApplyResult)
def patch_conversation(payload: ConversationPatchIn, admin: User = Depends(require_admin),
                       db: Session = Depends(get_db)) -> ApplyResult:
    patch = payload.model_dump(exclude_none=True)
    rc.set_conversation_config(db, admin_email=admin.email, patch=patch)
    _audit(db, admin, AUDIT_AI_CONFIG_UPDATED, "conversation_config", "conversation",
           f"Updated conversation config: {', '.join(patch.keys())}")
    db.commit()
    return ApplyResult(success=True, apply_mode="new_sessions", message="Saved. Applies to new interview sessions.")


# ------------------------------ patient voices (admin) ------------------------
def _voice_out(d: dict) -> VoiceRowOut:
    d = dict(d)
    d.pop("voice_id", None)  # never expose the full voice id
    return VoiceRowOut.model_validate(d)


@router.get("/voices", response_model=VoiceListOut, dependencies=[Depends(require_admin)])
def list_voices(db: Session = Depends(get_db)) -> VoiceListOut:
    return VoiceListOut(voices=[_voice_out(v) for v in rc.list_voice_rows(db)])


@router.get("/voices/{case_id}/{speaker_id}", response_model=VoiceRowOut, dependencies=[Depends(require_admin)])
def get_voice(case_id: str, speaker_id: str, db: Session = Depends(get_db)) -> VoiceRowOut:
    return _voice_out(rc.get_voice_row(db, case_id, speaker_id))


@router.patch("/voices/{case_id}/{speaker_id}", response_model=VoiceRowOut)
def patch_voice(case_id: str, speaker_id: str, payload: VoicePatchIn,
                admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> VoiceRowOut:
    patch = payload.model_dump(exclude_none=True)
    expected = patch.pop("expected_updated_at", None)
    row = rc.set_voice(db, case_id=case_id, speaker_id=speaker_id, patch=patch,
                       admin_email=admin.email, expected_updated_at=expected)
    _audit(db, admin, AUDIT_VOICE_UPDATED, "voice", f"{case_id}:{speaker_id}",
           f"Updated voice for {case_id}/{speaker_id}")
    db.commit()
    return _voice_out(row)


@router.post("/voices/{case_id}/{speaker_id}/restore", response_model=VoiceRowOut)
def restore_voice(case_id: str, speaker_id: str, admin: User = Depends(require_admin),
                  db: Session = Depends(get_db)) -> VoiceRowOut:
    row = rc.restore_voice(db, case_id=case_id, speaker_id=speaker_id, admin_email=admin.email)
    _audit(db, admin, AUDIT_VOICE_RESTORED, "voice", f"{case_id}:{speaker_id}",
           f"Restored default voice for {case_id}/{speaker_id}")
    db.commit()
    return _voice_out(row)


@router.post("/voices/{case_id}/{speaker_id}/preview", dependencies=[Depends(require_admin)])
def preview_voice(case_id: str, speaker_id: str, payload: VoicePatchIn,
                  db: Session = Depends(get_db),
                  client: ElevenLabsClient = Depends(get_elevenlabs_client)) -> StreamingResponse:
    """Synthesize an UNSAVED voice configuration for audition. Does not save."""
    current = rc.get_voice_row(db, case_id, speaker_id)  # validates the speaker (400 if unknown)
    voice_id = (payload.voice_id or current.get("voice_id") or "").strip()
    if not voice_id:
        raise VoiceNotAvailableError()
    el = rc.elevenlabs_runtime(db)
    if not el.api_key or not el.enabled:
        raise VoiceNotAvailableError()

    sample = (payload.preview_text or current.get("preview_text") or "Hello.").strip()[:255]
    # Build voice settings from the unsaved values (fall back to current).
    prof_like = type("P", (), {
        "stability": payload.stability if payload.stability is not None else current["stability"],
        "similarity_boost": payload.similarity_boost if payload.similarity_boost is not None else current["similarity_boost"],
        "style": payload.style if payload.style is not None else current["style"],
        "speed": payload.speed if payload.speed is not None else current["speed"],
        "speaker_boost": payload.speaker_boost if payload.speaker_boost is not None else current["speaker_boost"],
        "fallback_rate": 1.0, "pause_style": "natural", "default_emotion": "neutral",
    })()
    mapped = map_speech_style(prof_like, None)
    voice_settings = mapped.to_elevenlabs()
    model_id = (payload.model_id or current.get("model") or el.model)
    fmt = el.output_format
    media_type = media_type_for(fmt)
    cache = get_audio_cache()
    key = make_cache_key(voice_id, model_id, sample, voice_settings, fmt)
    headers = {"Cache-Control": "no-store", "X-Pause-Before-Ms": str(mapped.pause_before_ms)}

    cached = cache.get(key)
    if cached is not None:
        return StreamingResponse(iter([cached]), media_type=media_type, headers=headers)

    upstream = client.stream_speech(text=sample, voice_id=voice_id, model_id=model_id,
                                    voice_settings=voice_settings, output_format=fmt)
    first = next(upstream)

    def gen() -> Iterator[bytes]:
        buf = [first]
        yield first
        try:
            for c in upstream:
                buf.append(c)
                yield c
        except Exception:
            return
        cache.put(key, b"".join(buf))

    return StreamingResponse(gen(), media_type=media_type, headers=headers)


# ------------------------------ history (admin) -------------------------------
@router.get("/history", response_model=HistoryListOut, dependencies=[Depends(require_admin)])
def get_history(db: Session = Depends(get_db)) -> HistoryListOut:
    from app.schemas.runtime_schema import HistoryItemOut
    return HistoryListOut(history=[HistoryItemOut.model_validate(h) for h in rc.list_history(db)])
