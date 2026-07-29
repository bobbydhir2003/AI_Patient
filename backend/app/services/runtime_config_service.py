"""Single source of truth for editable AI / voice / credential configuration.

Resolution priority for every value:
    1. runtime override stored in the database (this service)
    2. environment / Settings fallback
    3. safe application default

Secrets are stored only as Fernet tokens; reads decrypt server-side and callers
receive masked metadata only. Non-secret config is cached in-process and
invalidated on every write (a global version counter), so provider clients can
call the read paths cheaply on each request.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.config import get_settings
from app.core.constants import (
    APPLY_IMMEDIATE,
    APPLY_NEW_SESSIONS,
    ELEVENLABS_FORMAT_ALLOWLIST,
    ELEVENLABS_MODEL_ALLOWLIST,
    ELEVENLABS_TIMEOUT_RANGE,
    MAX_PATIENT_RESPONSE_CHARS,
    OPENAI_MAX_TOKENS_RANGE,
    OPENAI_MODEL_ALLOWLIST,
    OPENAI_TIMEOUT_RANGE,
    VOICE_SIMILARITY_RANGE,
    VOICE_SPEED_RANGE,
    VOICE_STABILITY_RANGE,
    VOICE_STYLE_RANGE,
)
from app.core.exceptions import ValidationFailedError
from app.database.connection import get_session_factory
from app.models import ApiCredential, ConfigurationHistory, PatientVoiceSetting, SystemSetting
from app.patient_engine import case_loader

# --- in-process cache invalidated on any write -----------------------------
_version = 0


def _bump() -> None:
    global _version
    _version += 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _open_session() -> Session:
    return get_session_factory()()


# --- typed setting access ---------------------------------------------------
def _cast(raw: str, value_type: str):
    if value_type == "int":
        return int(raw)
    if value_type == "float":
        return float(raw)
    if value_type == "bool":
        return str(raw).lower() in ("1", "true", "yes", "on")
    return raw


def _get_setting(db: Session, key: str):
    # Defensive: if the runtime tables are not present yet (fresh DB / migration
    # pending), fall back to env/default rather than crash a provider request.
    try:
        row = db.execute(select(SystemSetting).where(SystemSetting.key == key)).scalar_one_or_none()
    except Exception:
        return None
    if row is None:
        return None
    try:
        return _cast(row.value, row.value_type)
    except (ValueError, TypeError):
        return None


# =====================================================================
#  OpenAI
# =====================================================================
@dataclass(frozen=True)
class OpenAIRuntime:
    api_key: str
    model: str
    timeout_seconds: float
    max_output_tokens: int
    patient_max_output_tokens: int | None
    streaming_enabled: bool


def openai_runtime(db: Session | None = None) -> OpenAIRuntime:
    own = db is None
    db = db or _open_session()
    try:
        s = get_settings()
        model = _get_setting(db, "openai_model") or s.openai_model or "gpt-4o-mini"
        timeout = _get_setting(db, "openai_timeout_seconds")
        max_tokens = _get_setting(db, "openai_max_output_tokens")
        streaming = _get_setting(db, "openai_streaming_enabled")
        return OpenAIRuntime(
            api_key=_active_key(db, "openai", s.openai_api_key),
            model=model,
            timeout_seconds=timeout if timeout is not None else s.openai_timeout_seconds,
            max_output_tokens=max_tokens if max_tokens is not None else s.openai_max_output_tokens,
            patient_max_output_tokens=s.openai_patient_max_output_tokens,
            streaming_enabled=(
                streaming if streaming is not None else s.openai_patient_streaming_enabled
            ),
        )
    finally:
        if own:
            db.close()


# =====================================================================
#  ElevenLabs
# =====================================================================
@dataclass(frozen=True)
class ElevenLabsRuntime:
    api_key: str
    enabled: bool
    model: str
    output_format: str
    timeout_seconds: float


def elevenlabs_runtime(db: Session | None = None) -> ElevenLabsRuntime:
    own = db is None
    db = db or _open_session()
    try:
        s = get_settings()
        model = _get_setting(db, "elevenlabs_model") or s.elevenlabs_default_model
        fmt = _get_setting(db, "elevenlabs_output_format") or s.elevenlabs_output_format
        timeout = _get_setting(db, "elevenlabs_timeout_seconds")
        return ElevenLabsRuntime(
            api_key=_active_key(db, "elevenlabs", s.elevenlabs_api_key),
            enabled=s.elevenlabs_enabled,
            model=model,
            output_format=fmt,
            timeout_seconds=timeout if timeout is not None else s.elevenlabs_timeout_seconds,
        )
    finally:
        if own:
            db.close()


# =====================================================================
#  Credentials (encrypted)
# =====================================================================
def _active_key(db: Session, service: str, env_fallback: str) -> str:
    try:
        row = db.execute(
            select(ApiCredential).where(ApiCredential.service == service)
        ).scalar_one_or_none()
    except Exception:
        return env_fallback  # runtime table absent -> env fallback
    if row and row.encrypted_secret and row.is_active:
        try:
            return crypto.decrypt_secret(row.encrypted_secret)
        except crypto.EncryptionUnavailableError:
            return env_fallback  # unreadable token -> safe fallback, never crash
    return env_fallback


def credential_status(db: Session) -> list[dict]:
    s = get_settings()
    env_keys = {"openai": s.openai_api_key, "elevenlabs": s.elevenlabs_api_key}
    rows = {r.service: r for r in db.execute(select(ApiCredential)).scalars().all()}
    out = []
    for service in ("openai", "elevenlabs"):
        row = rows.get(service)
        if row and row.encrypted_secret:
            out.append({
                "service": service,
                "configured": True,
                "source": "runtime",
                "masked_value": row.masked_value,
                "last_test_status": row.last_test_status,
                "last_test_message": row.last_test_message,
                "last_tested_at": row.last_tested_at.isoformat() if row.last_tested_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "updated_by": row.updated_by or None,
                "status": "configured",
            })
        else:
            env_key = env_keys.get(service, "")
            out.append({
                "service": service,
                "configured": bool(env_key),
                "source": "env" if env_key else "none",
                "masked_value": crypto.mask_secret(env_key) if env_key else None,
                "last_test_status": "never",
                "last_test_message": "",
                "last_tested_at": None,
                "updated_at": None,
                "updated_by": None,
                "status": "configured" if env_key else "not_configured",
            })
    return out


def set_credential(db: Session, *, service: str, new_key: str, admin_email: str) -> dict:
    service = service.lower().strip()
    if service not in ("openai", "elevenlabs"):
        raise ValidationFailedError("Unknown credential service.")
    new_key = (new_key or "").strip()
    if len(new_key) < 8:
        raise ValidationFailedError("That key looks too short to be valid.")
    if not crypto.encryption_available():
        raise ValidationFailedError(
            "Secure secret storage is disabled: CONFIG_ENCRYPTION_KEY is not set on the server."
        )
    token = crypto.encrypt_secret(new_key)
    masked = crypto.mask_secret(new_key)
    row = db.execute(
        select(ApiCredential).where(ApiCredential.service == service)
    ).scalar_one_or_none()
    was_configured = bool(row and row.encrypted_secret)
    if row is None:
        row = ApiCredential(service=service)
        db.add(row)
    row.encrypted_secret = token
    row.masked_value = masked
    row.is_active = True
    row.updated_by = admin_email
    row.updated_at = _now()
    _record_history(db, "credential", service, service,
                    "configured" if was_configured else "not_configured",
                    f"replaced -> {masked}", admin_email)
    db.flush()
    _bump()
    return {"service": service, "masked_value": masked, "configured": True}


def remove_credential(db: Session, *, service: str, admin_email: str) -> dict:
    row = db.execute(
        select(ApiCredential).where(ApiCredential.service == service)
    ).scalar_one_or_none()
    if row is None or not row.encrypted_secret:
        raise ValidationFailedError("No stored credential to remove.")
    row.encrypted_secret = ""
    row.masked_value = ""
    row.is_active = False
    row.updated_by = admin_email
    row.updated_at = _now()
    _record_history(db, "credential", service, service, "configured", "removed", admin_email)
    db.flush()
    _bump()
    return {"service": service, "configured": False}


def record_test_result(db: Session, *, service: str, status: str, message: str) -> None:
    row = db.execute(
        select(ApiCredential).where(ApiCredential.service == service)
    ).scalar_one_or_none()
    if row is not None:
        row.last_test_status = status
        row.last_test_message = message[:255]
        row.last_tested_at = _now()
        db.flush()
        _bump()


# =====================================================================
#  Config editing (validated + versioned)
# =====================================================================
def _record_history(db, ctype, key, entity, prev, new, by, reason=""):
    db.add(ConfigurationHistory(
        configuration_type=ctype, configuration_key=key, entity_id=entity,
        previous_value=str(prev)[:2000], new_value=str(new)[:2000],
        changed_by=by, change_reason=reason,
    ))


def _upsert_setting(db, key, value, value_type, category, apply_mode, by, description=""):
    row = db.execute(select(SystemSetting).where(SystemSetting.key == key)).scalar_one_or_none()
    prev = row.value if row else "(default)"
    if row is None:
        row = SystemSetting(key=key, category=category)
        db.add(row)
    row.value = str(value)
    row.value_type = value_type
    row.category = category
    row.apply_mode = apply_mode
    row.description = description
    row.updated_by = by
    row.updated_at = _now()
    _record_history(db, category, key, key, prev, str(value), by)
    return prev


def _in_range(v, rng, label):
    lo, hi = rng
    if not (lo <= v <= hi):
        raise ValidationFailedError(f"{label} must be between {lo} and {hi}.")


def set_openai_config(db: Session, *, admin_email: str, patch: dict) -> None:
    if "model" in patch:
        if patch["model"] not in OPENAI_MODEL_ALLOWLIST:
            raise ValidationFailedError(
                f"Model '{patch['model']}' is not on the approved list: "
                f"{', '.join(OPENAI_MODEL_ALLOWLIST)}."
            )
        _upsert_setting(db, "openai_model", patch["model"], "str", "openai", APPLY_NEW_SESSIONS, admin_email)
    if "timeout_seconds" in patch:
        v = float(patch["timeout_seconds"]); _in_range(v, OPENAI_TIMEOUT_RANGE, "Timeout")
        _upsert_setting(db, "openai_timeout_seconds", v, "float", "openai", APPLY_IMMEDIATE, admin_email)
    if "max_output_tokens" in patch:
        v = int(patch["max_output_tokens"]); _in_range(v, OPENAI_MAX_TOKENS_RANGE, "Max tokens")
        _upsert_setting(db, "openai_max_output_tokens", v, "int", "openai", APPLY_NEW_SESSIONS, admin_email)
    if "streaming_enabled" in patch:
        _upsert_setting(db, "openai_streaming_enabled", bool(patch["streaming_enabled"]), "bool",
                        "openai", APPLY_NEW_SESSIONS, admin_email)
    db.flush(); _bump()


def set_elevenlabs_config(db: Session, *, admin_email: str, patch: dict) -> None:
    if "model" in patch:
        if patch["model"] not in ELEVENLABS_MODEL_ALLOWLIST:
            raise ValidationFailedError(
                f"ElevenLabs model '{patch['model']}' is not supported. Approved: "
                f"{', '.join(ELEVENLABS_MODEL_ALLOWLIST)}."
            )
        _upsert_setting(db, "elevenlabs_model", patch["model"], "str", "elevenlabs", APPLY_IMMEDIATE, admin_email)
    if "output_format" in patch:
        if patch["output_format"] not in ELEVENLABS_FORMAT_ALLOWLIST:
            raise ValidationFailedError(
                f"Output format '{patch['output_format']}' is not supported. Approved: "
                f"{', '.join(ELEVENLABS_FORMAT_ALLOWLIST)}."
            )
        _upsert_setting(db, "elevenlabs_output_format", patch["output_format"], "str", "elevenlabs", APPLY_IMMEDIATE, admin_email)
    if "timeout_seconds" in patch:
        v = float(patch["timeout_seconds"]); _in_range(v, ELEVENLABS_TIMEOUT_RANGE, "Timeout")
        _upsert_setting(db, "elevenlabs_timeout_seconds", v, "float", "elevenlabs", APPLY_IMMEDIATE, admin_email)
    db.flush(); _bump()


def set_conversation_config(db: Session, *, admin_email: str, patch: dict) -> None:
    if "sentence_level_streaming" in patch:
        _upsert_setting(db, "patient_sentence_pipelining_enabled", bool(patch["sentence_level_streaming"]),
                        "bool", "conversation", APPLY_NEW_SESSIONS, admin_email)
    if "patient_streaming" in patch:
        _upsert_setting(db, "openai_streaming_enabled", bool(patch["patient_streaming"]),
                        "bool", "conversation", APPLY_NEW_SESSIONS, admin_email)
    db.flush(); _bump()


def ai_configuration(db: Session) -> dict:
    oa = openai_runtime(db)
    el = elevenlabs_runtime(db)
    sentence = _get_setting(db, "patient_sentence_pipelining_enabled")
    sentence = sentence if sentence is not None else get_settings().patient_sentence_pipelining_enabled
    return {
        "openai": {
            "configured": bool(oa.api_key),
            "model": oa.model,
            "streaming_enabled": oa.streaming_enabled,
            "timeout_seconds": oa.timeout_seconds,
            "max_output_tokens": oa.max_output_tokens,
            "status": "configured" if oa.api_key else "not_configured",
            "model_allowlist": list(OPENAI_MODEL_ALLOWLIST),
        },
        "elevenlabs": {
            "configured": bool(el.api_key) and el.enabled,
            "enabled": el.enabled,
            "model": el.model,
            "output_format": el.output_format,
            "timeout_seconds": el.timeout_seconds,
            "status": "configured" if (el.api_key and el.enabled) else "not_configured",
            "model_allowlist": list(ELEVENLABS_MODEL_ALLOWLIST),
            "format_allowlist": list(ELEVENLABS_FORMAT_ALLOWLIST),
        },
        "conversation": {
            "sentence_level_streaming": "Enabled" if sentence else "Disabled",
            "patient_streaming": "Enabled" if oa.streaming_enabled else "Disabled",
            "disclosure_control": "Enabled (built-in)",
            "motivational_interviewing": "Standard (built-in)",
            "age_appropriate_language": "Enabled (built-in)",
            "caregiver_routing": "Not implemented",
            "max_patient_response_chars": MAX_PATIENT_RESPONSE_CHARS,
        },
    }


# =====================================================================
#  Voices
# =====================================================================
_SPEAKERS = [("camden", "patient"), ("camden", "caregiver"),
             ("carly", "patient"), ("sofia", "patient"), ("jayden", "patient")]
_SPEAKER_LABEL = {("camden", "patient"): "Camden (Patient)", ("camden", "caregiver"): "Mother",
                  ("carly", "patient"): "Carly (Patient)", ("sofia", "patient"): "Sofia (Patient)",
                  ("jayden", "patient"): "Jayden (Patient)"}
PREVIEW_SAMPLES = {
    ("camden", "patient"): "Hi, my name is Camden.",
    ("camden", "caregiver"): "Hi, I'm Camden's mother. I can help answer your questions.",
    ("carly", "patient"): "Hi, I'm Carly. Thank you for meeting with me.",
    ("sofia", "patient"): "Hi, I'm Sofia.",
    ("jayden", "patient"): "Hi, I'm Jayden. I'm ready to get started.",
}


def _voice_override(db, case_id, speaker_id) -> PatientVoiceSetting | None:
    return db.execute(
        select(PatientVoiceSetting).where(
            PatientVoiceSetting.case_id == case_id,
            PatientVoiceSetting.speaker_id == speaker_id,
        )
    ).scalar_one_or_none()


@dataclass(frozen=True)
class ResolvedRuntimeVoice:
    case_id: str
    speaker_id: str
    voice_id: str
    model_id: str
    stability: float
    similarity_boost: float
    style: float
    speed: float
    speaker_boost: bool
    available: bool
    source: str  # runtime | case_file | none


def resolve_voice(db: Session, case_id: str, speaker_id: str = "patient") -> ResolvedRuntimeVoice:
    """Runtime override -> case-file profile -> unavailable."""
    ov = _voice_override(db, case_id, speaker_id)
    el = elevenlabs_runtime(db)
    if ov and ov.is_active and ov.voice_id.strip():
        return ResolvedRuntimeVoice(case_id, speaker_id, ov.voice_id, ov.model_id or el.model,
                                    ov.stability, ov.similarity_boost, ov.style, ov.speed,
                                    ov.speaker_boost, True, "runtime")
    if speaker_id == "patient":
        case = case_loader.load_case(case_id)
        p = case.voice_profile
        if p and p.voice_id.strip() and not p.voice_id.startswith("PASTE_") and p.enabled:
            return ResolvedRuntimeVoice(case_id, speaker_id, p.voice_id, p.model_id or el.model,
                                        p.stability, p.similarity_boost, p.style, p.speed,
                                        p.speaker_boost, True, "case_file")
    return ResolvedRuntimeVoice(case_id, speaker_id, "", el.model, 0.5, 0.75, 0.1, 1.0, True, False, "none")


def _voice_row(db, case_id, speaker_id) -> dict:
    case = case_loader.load_case(case_id)
    rv = resolve_voice(db, case_id, speaker_id)
    ov = _voice_override(db, case_id, speaker_id)
    return {
        "case_id": case_id,
        "speaker_id": speaker_id,
        "patient_name": case.full_name or case.display_name,
        "speaker_label": _SPEAKER_LABEL[(case_id, speaker_id)],
        "image": case.image,
        "display_name": ov.display_name if ov else "",
        "voice_name": (ov.voice_name if ov else "") or None,
        "voice_id": rv.voice_id,  # full id ONLY used server-side; masked before API return
        "masked_voice_id": crypto.mask_secret(rv.voice_id) if rv.available and rv.voice_id else None,
        "model": rv.model_id if rv.available else None,
        "stability": rv.stability,
        "similarity_boost": rv.similarity_boost,
        "style": rv.style,
        "speed": rv.speed,
        "speaker_boost": rv.speaker_boost,
        "preview_text": (ov.preview_text if ov and ov.preview_text else PREVIEW_SAMPLES[(case_id, speaker_id)]),
        "status": "active" if rv.available else "not_configured",
        "source": rv.source,
        "has_override": ov is not None,
        "updated_at": ov.updated_at.isoformat() if ov and ov.updated_at else None,
        "updated_by": (ov.updated_by if ov else None) or None,
    }


def list_voice_rows(db: Session) -> list[dict]:
    return [_voice_row(db, c, s) for c, s in _SPEAKERS]


def get_voice_row(db: Session, case_id: str, speaker_id: str) -> dict:
    if (case_id, speaker_id) not in _SPEAKER_LABEL:
        raise ValidationFailedError("Unknown speaker.")
    return _voice_row(db, case_id, speaker_id)


def set_voice(db: Session, *, case_id: str, speaker_id: str, patch: dict,
              admin_email: str, expected_updated_at: str | None = None) -> dict:
    if (case_id, speaker_id) not in _SPEAKER_LABEL:
        raise ValidationFailedError("Unknown speaker.")
    row = _voice_override(db, case_id, speaker_id)
    # optimistic locking
    if expected_updated_at and row and row.updated_at:
        if row.updated_at.isoformat() != expected_updated_at:
            raise ConfigConflictError()
    for field, rng, label in (
        ("stability", VOICE_STABILITY_RANGE, "Stability"),
        ("similarity_boost", VOICE_SIMILARITY_RANGE, "Similarity boost"),
        ("style", VOICE_STYLE_RANGE, "Style"),
        ("speed", VOICE_SPEED_RANGE, "Speed"),
    ):
        if field in patch and patch[field] is not None:
            _in_range(float(patch[field]), rng, label)
    if "voice_id" in patch and patch["voice_id"] is not None:
        vid = str(patch["voice_id"]).strip()
        if vid and (len(vid) < 8 or " " in vid):
            raise ValidationFailedError("That Voice ID does not look valid.")
    prev = _voice_row(db, case_id, speaker_id)
    if row is None:
        row = PatientVoiceSetting(case_id=case_id, speaker_id=speaker_id)
        db.add(row)
    for f in ("display_name", "voice_id", "voice_name", "model_id", "preview_text"):
        if f in patch and patch[f] is not None:
            setattr(row, f, str(patch[f]))
    for f in ("stability", "similarity_boost", "style", "speed"):
        if f in patch and patch[f] is not None:
            setattr(row, f, float(patch[f]))
    if "speaker_boost" in patch and patch["speaker_boost"] is not None:
        row.speaker_boost = bool(patch["speaker_boost"])
    if "is_active" in patch and patch["is_active"] is not None:
        row.is_active = bool(patch["is_active"])
    row.updated_by = admin_email
    row.updated_at = _now()
    _record_history(db, "voice", f"{case_id}:{speaker_id}", f"{case_id}:{speaker_id}",
                    crypto.mask_secret(prev.get("voice_id") or ""),
                    crypto.mask_secret(row.voice_id or ""), admin_email)
    db.flush(); _bump()
    return _voice_row(db, case_id, speaker_id)


def restore_voice(db: Session, *, case_id: str, speaker_id: str, admin_email: str) -> dict:
    """Remove the runtime override so the case-file default applies again."""
    row = _voice_override(db, case_id, speaker_id)
    if row is None:
        raise ValidationFailedError("No override to restore.")
    db.delete(row)
    _record_history(db, "voice", f"{case_id}:{speaker_id}", f"{case_id}:{speaker_id}",
                    "override", "restored_default", admin_email)
    db.flush(); _bump()
    return _voice_row(db, case_id, speaker_id)


# =====================================================================
#  History + session snapshot
# =====================================================================
def list_history(db: Session, limit: int = 50) -> list[dict]:
    rows = db.execute(
        select(ConfigurationHistory).order_by(ConfigurationHistory.changed_at.desc()).limit(limit)
    ).scalars().all()
    return [{
        "id": r.id, "type": r.configuration_type, "key": r.configuration_key,
        "entity_id": r.entity_id, "previous_value": r.previous_value, "new_value": r.new_value,
        "changed_by": r.changed_by, "changed_at": r.changed_at.isoformat() if r.changed_at else "",
    } for r in rows]


def session_snapshot(db: Session, case_id: str) -> str:
    """Freeze the active config a new interview should keep using. No secrets."""
    oa = openai_runtime(db)
    el = elevenlabs_runtime(db)
    voice = resolve_voice(db, case_id, "patient")
    from app.core.constants import PROMPT_VERSION
    return json.dumps({
        "openai_model": oa.model,
        "openai_max_output_tokens": oa.max_output_tokens,
        "openai_streaming_enabled": oa.streaming_enabled,
        "elevenlabs_model": el.model,
        "elevenlabs_output_format": el.output_format,
        "voice_id_masked": crypto.mask_secret(voice.voice_id) if voice.voice_id else None,
        "voice_source": voice.source,
        "prompt_version": PROMPT_VERSION,
        "captured_at": _now().isoformat(),
    })


class ConfigConflictError(ValidationFailedError):
    """Raised when another admin changed the setting since it was loaded."""

    def __init__(self) -> None:
        super().__init__(
            "This setting was changed by another administrator. "
            "Refresh and review the latest version before saving."
        )


# =====================================================================
#  Real, lightweight provider connection tests
# =====================================================================
def test_openai_key(key: str) -> tuple[str, str]:
    """Validate an OpenAI key with a cheap, non-generative call (models.list).

    Returns (status, sanitized_message). Never raises; never echoes the key."""
    if not key:
        return "not_configured", "No key configured."
    try:
        from openai import OpenAI

        client = OpenAI(api_key=key, timeout=10.0)
        client.models.list()  # cheap metadata call, not a paid generation
        return "success", "Connection succeeded."
    except Exception as exc:  # noqa: BLE001 - sanitize any provider/SDK error
        return "failed", _sanitize_provider_error(exc)


def test_elevenlabs_key(key: str) -> tuple[str, str]:
    """Validate an ElevenLabs key via GET /v1/user (cheap, no synthesis)."""
    if not key:
        return "not_configured", "No key configured."
    try:
        import httpx

        r = httpx.get(
            "https://api.elevenlabs.io/v1/user",
            headers={"xi-api-key": key},
            timeout=10.0,
        )
        if r.status_code == 200:
            return "success", "Connection succeeded."
        if r.status_code in (401, 403):
            return "failed", "Authentication failed (invalid key)."
        return "failed", f"Provider returned status {r.status_code}."
    except Exception as exc:  # noqa: BLE001
        return "failed", _sanitize_provider_error(exc)


def _sanitize_provider_error(exc: Exception) -> str:
    """A short, safe message - never a stack trace, URL with key, or raw body."""
    name = type(exc).__name__
    if "Timeout" in name:
        return "The request timed out."
    if "Connect" in name or "Transport" in name:
        return "Could not reach the provider."
    if "Authentication" in name or "PermissionDenied" in name:
        return "Authentication failed (invalid key)."
    return "The provider rejected the request."
