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
    OPENAI_MAX_TOKENS_RANGE,
    OPENAI_MODEL_ALLOWLIST,
    OPENAI_TIMEOUT_RANGE,
)
from app.core.exceptions import ValidationFailedError
from app.database.connection import get_session_factory
from app.models import ApiCredential, ConfigurationHistory, SystemSetting

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


def mock_ai_enabled(db: Session | None = None) -> bool:
    """Effective simulated-provider flag. Precedence: runtime DB override
    (set by the load-test controller for a Simulated-AI run) -> startup env.
    Reused by the OpenAI client so a Simulated-AI load test spends no provider
    credits while still exercising the real application path."""
    own = db is None
    db = db or _open_session()
    try:
        override = _get_setting(db, "mock_ai")
        return bool(override) if override is not None else bool(get_settings().mock_ai)
    finally:
        if own:
            db.close()


def set_mock_ai(db: Session, *, enabled: bool, admin_email: str) -> None:
    _upsert_setting(db, "mock_ai", bool(enabled), "bool", "load_test",
                    APPLY_IMMEDIATE, admin_email)
    db.flush(); _bump()


def clear_mock_ai(db: Session) -> None:
    row = db.execute(select(SystemSetting).where(SystemSetting.key == "mock_ai")).scalar_one_or_none()
    if row is not None:
        db.delete(row)
        db.flush(); _bump()


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
            plaintext = crypto.decrypt_secret(row.encrypted_secret)
        except crypto.EncryptionUnavailableError:
            return env_fallback  # unreadable token -> safe fallback, never crash
        # A12: opportunistically migrate a legacy (unsalted v1) token to the new
        # salted PBKDF2 (v2) format. Best-effort - a failure here must never
        # break the read, so we roll back and keep serving the decrypted value.
        if crypto.is_legacy_token(row.encrypted_secret):
            try:
                row.encrypted_secret = crypto.encrypt_secret(plaintext)
                db.commit()
            except Exception:
                db.rollback()
        return plaintext
    return env_fallback


def credential_status(db: Session) -> list[dict]:
    s = get_settings()
    env_keys = {"openai": s.openai_api_key}
    # Part 2: whether the server can store an encrypted DB credential at all. When
    # false, the UI disables "Replace Key" and explains that CONFIG_ENCRYPTION_KEY
    # must be set. Never exposes the key itself.
    secure_storage = crypto.encryption_available()
    rows = {r.service: r for r in db.execute(select(ApiCredential)).scalars().all()}
    out = []
    for service in ("openai",):
        row = rows.get(service)
        if row and row.encrypted_secret:
            out.append({
                "service": service,
                "configured": True,
                "source": "database",  # effective source: encrypted DB override
                "masked_value": row.masked_value,
                "last_test_status": row.last_test_status,
                "last_test_message": row.last_test_message,
                "last_tested_at": row.last_tested_at.isoformat() if row.last_tested_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "updated_by": row.updated_by or None,
                "status": "configured",
                "secure_storage_available": secure_storage,
            })
        else:
            env_key = env_keys.get(service, "")
            out.append({
                "service": service,
                "configured": bool(env_key),
                "source": "environment" if env_key else "none",
                "masked_value": crypto.mask_secret(env_key) if env_key else None,
                "last_test_status": "never",
                "last_test_message": "",
                "last_tested_at": None,
                "updated_at": None,
                "updated_by": None,
                "status": "configured" if env_key else "not_configured",
                "secure_storage_available": secure_storage,
            })
    return out


def set_credential(db: Session, *, service: str, new_key: str, admin_email: str) -> dict:
    service = service.lower().strip()
    if service not in ("openai",):
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


def ai_configuration(db: Session) -> dict:
    oa = openai_runtime(db)
    return {
        "openai": {
            "configured": bool(oa.api_key),
            "model": oa.model,
            "timeout_seconds": oa.timeout_seconds,
            "max_output_tokens": oa.max_output_tokens,
            "status": "configured" if oa.api_key else "not_configured",
            "model_allowlist": list(OPENAI_MODEL_ALLOWLIST),
        },
    }


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
    from app.core.constants import PROMPT_VERSION
    return json.dumps({
        "openai_model": oa.model,
        "openai_max_output_tokens": oa.max_output_tokens,
        "prompt_version": PROMPT_VERSION,
        "captured_at": _now().isoformat(),
    })


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
