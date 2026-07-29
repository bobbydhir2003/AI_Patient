"""Real, truthful system-health checks for the technical System Dashboard.

Design rule: never fabricate a healthy/connected status. Every value is either
a real measurement, a real configuration value, or an honest "unknown /
unavailable / not configured" state. No secrets are ever returned.
"""
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import (
    APP_VERSION,
    STANDARD_CASE_IDS,
    STORAGE_WARNING_PERCENT,
)
from app.database.connection import get_engine
from app.patient_engine import case_loader
from app.repositories.audit_repository import AuditRepository
from app.schemas.system_schema import (
    ActivityOut,
    AiConfigurationOut,
    AlertOut,
    AudioQueueHealthOut,
    BackendHealthOut,
    ConversationSettingsOut,
    CredentialStatusOut,
    DatabaseHealthOut,
    ElevenLabsConfigOut,
    OpenAIConfigOut,
    ServiceHealthOut,
    StorageHealthOut,
    SystemOverviewOut,
    VoiceRowOut,
)
from app.voice.audio_cache import get_audio_cache
from app.voice.voice_profile_loader import load_voice_profile


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask_secret(value: str, keep_end: int = 4) -> str:
    """Mask a secret/id, revealing only a short head and tail. Never the middle."""
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= keep_end + 3:
        return "•" * len(value)
    head = value[: min(4, len(value) - keep_end)]
    return f"{head}••••{value[-keep_end:]}"


# ----------------------------- health checks -----------------------------
def check_database(db: Session) -> DatabaseHealthOut:
    checked_at = _now_iso()
    try:
        t0 = time.perf_counter()
        db.execute(text("SELECT 1"))
        latency = int((time.perf_counter() - t0) * 1000)
    except Exception:
        return DatabaseHealthOut(status="unavailable", checked_at=checked_at)

    try:
        db_type = get_engine().dialect.name
    except Exception:
        db_type = ""

    migration_version: str | None = None
    try:
        row = db.execute(text("SELECT version_num FROM alembic_version")).first()
        migration_version = row[0] if row else None
    except Exception:
        migration_version = None  # table not present (e.g. auto-created schema)

    return DatabaseHealthOut(
        status="connected",
        db_type=db_type,
        latency_ms=latency,
        migration_version=migration_version,
        checked_at=checked_at,
    )


def check_openai(db=None) -> ServiceHealthOut:
    from app.services import runtime_config_service as rc

    rt = rc.openai_runtime(db)
    configured = bool(rt.api_key)
    return ServiceHealthOut(
        service="openai",
        configured=configured,
        status="configured" if configured else "not_configured",
        model=rt.model,  # reflects any runtime override
        streaming_enabled=rt.streaming_enabled,
        last_success_at=None,  # not recorded in this deployment
        last_error=None,
        checked_at=_now_iso(),
    )


def check_elevenlabs(db=None) -> ServiceHealthOut:
    from app.services import runtime_config_service as rc

    rt = rc.elevenlabs_runtime(db)
    configured = bool(rt.api_key) and rt.enabled
    return ServiceHealthOut(
        service="elevenlabs",
        configured=configured,
        status="configured" if configured else "not_configured",
        model=rt.model,  # reflects any runtime override
        streaming_enabled=get_settings().elevenlabs_streaming_input_enabled,
        last_success_at=None,
        last_error=None,
        checked_at=_now_iso(),
    )


def check_audio_queue() -> AudioQueueHealthOut:
    """Patient audio is synthesized synchronously per request (streamed straight
    to the browser). There is no persistent, inspectable job queue, so we report
    that honestly instead of inventing counts."""
    return AudioQueueHealthOut(
        available=False,
        status="unavailable",
        message="No persistent audio queue: patient audio is streamed synchronously per request.",
        checked_at=_now_iso(),
    )


def check_storage() -> StorageHealthOut:
    checked_at = _now_iso()
    try:
        usage = shutil.disk_usage(str(Path(__file__).resolve().parent))
        percent = round(usage.used / usage.total * 100, 1) if usage.total else None
    except Exception:
        return StorageHealthOut(status="unavailable", checked_at=checked_at)

    cache = get_audio_cache()
    status = "warning" if (percent is not None and percent >= STORAGE_WARNING_PERCENT) else "healthy"
    return StorageHealthOut(
        status=status,
        used_bytes=usage.used,
        total_bytes=usage.total,
        free_bytes=usage.free,
        percent_used=percent,
        audio_cache_entries=len(cache),
        audio_cache_max_entries=cache.max_entries,
        audio_cache_bytes=cache.nbytes(),
        checked_at=checked_at,
    )


# ----------------------------- voices -----------------------------
def _patient_voice_row(case_id: str) -> VoiceRowOut:
    case = case_loader.load_case(case_id)
    resolved = load_voice_profile(case_id)
    reason_to_status = {
        "": "active",
        "missing_voice_id": "not_configured",
        "voice_profile_disabled": "disabled",
        "unsupported_provider": "unavailable",
        "missing_api_key": "unavailable",
        "elevenlabs_disabled": "unavailable",
    }
    status = reason_to_status.get(resolved.reason, "unavailable")
    masked = mask_secret(resolved.profile.voice_id) if status == "active" else None
    return VoiceRowOut(
        case_id=case_id,
        speaker_id="patient",
        patient_name=case.full_name or case.display_name,
        speaker_label=f"{case.display_name} (Patient)",
        image=case.image,
        voice_name=None,  # ElevenLabs display name requires a live API call
        masked_voice_id=masked,
        model=resolved.model_id if status == "active" else None,
        status=status,
        reason=resolved.reason,
    )


def list_voices(db=None) -> list[VoiceRowOut]:
    """Voice rows via the runtime service, so dashboard reflects overrides."""
    from app.services import runtime_config_service as rc

    if db is None:
        return []
    out: list[VoiceRowOut] = []
    for v in rc.list_voice_rows(db):
        out.append(
            VoiceRowOut(
                case_id=v["case_id"],
                speaker_id=v["speaker_id"],
                patient_name=v["patient_name"],
                speaker_label=v["speaker_label"],
                image=v["image"],
                voice_name=v["voice_name"],
                masked_voice_id=v["masked_voice_id"],
                model=v["model"],
                status=v["status"],
                reason=v["source"],
            )
        )
    return out


# ----------------------------- ai configuration -----------------------------
def get_ai_configuration(db=None) -> AiConfigurationOut:
    """Active AI config via the runtime service (reflects runtime overrides)."""
    from app.services import runtime_config_service as rc

    if db is None:
        d = {"openai": {}, "elevenlabs": {}, "conversation": {}}
    else:
        d = rc.ai_configuration(db)
    o, e, c = d["openai"], d["elevenlabs"], d["conversation"]
    openai = OpenAIConfigOut(
        configured=o.get("configured", False),
        model=o.get("model", ""),
        streaming_enabled=o.get("streaming_enabled", False),
        timeout_seconds=o.get("timeout_seconds"),
        max_output_tokens=o.get("max_output_tokens"),
        status=o.get("status", "not_configured"),
    )
    elevenlabs = ElevenLabsConfigOut(
        configured=e.get("configured", False),
        enabled=e.get("enabled", False),
        model=e.get("model", ""),
        output_format=e.get("output_format", ""),
        timeout_seconds=e.get("timeout_seconds"),
        status=e.get("status", "not_configured"),
    )
    conversation = ConversationSettingsOut(
        sentence_level_streaming=c.get("sentence_level_streaming", "Disabled"),
        patient_streaming=c.get("patient_streaming", "Disabled"),
        disclosure_control=c.get("disclosure_control", "Enabled (built-in)"),
        motivational_interviewing=c.get("motivational_interviewing", "Standard (built-in)"),
        age_appropriate_language=c.get("age_appropriate_language", "Enabled (built-in)"),
        caregiver_routing=c.get("caregiver_routing", "Not implemented"),
        max_patient_response_chars=c.get("max_patient_response_chars", 900),
    )
    return AiConfigurationOut(openai=openai, elevenlabs=elevenlabs, conversation=conversation)


def get_credentials_status(db=None) -> list[CredentialStatusOut]:
    """Status only - the full key is never returned or reconstructable.
    Sources from the runtime store (encrypted) with env fallback."""
    from app.services import runtime_config_service as rc

    if db is None:
        return []
    out = []
    for c in rc.credential_status(db):
        out.append(
            CredentialStatusOut(
                service=c["service"],
                configured=c["configured"],
                masked_value=c["masked_value"],
                updated_at=c["updated_at"],
                updated_by=c["updated_by"],
                status=c["status"],
            )
        )
    return out


# ----------------------------- alerts -----------------------------
def build_alerts(
    database: DatabaseHealthOut,
    openai: ServiceHealthOut,
    elevenlabs: ServiceHealthOut,
    storage: StorageHealthOut,
) -> list[AlertOut]:
    """Alerts derived ONLY from the real checks just performed."""
    now = _now_iso()
    alerts: list[AlertOut] = []
    if database.status != "connected":
        alerts.append(AlertOut(id="db-unavailable", severity="critical", service="Database",
                               message="Database health check failed.", detected_at=now))
    if not openai.configured:
        alerts.append(AlertOut(id="openai-not-configured", severity="warning", service="OpenAI",
                               message="OpenAI API key is not configured.", detected_at=now))
    if not elevenlabs.configured:
        alerts.append(AlertOut(id="elevenlabs-not-configured", severity="warning", service="ElevenLabs",
                               message="ElevenLabs is not configured or disabled.", detected_at=now))
    if storage.status == "warning" and storage.percent_used is not None:
        alerts.append(AlertOut(id="storage-high", severity="warning", service="Storage",
                               message=f"Disk usage is {storage.percent_used}% "
                                       f"(threshold {int(STORAGE_WARNING_PERCENT)}%).",
                               detected_at=now))
    elif storage.status == "unavailable":
        alerts.append(AlertOut(id="storage-unavailable", severity="warning", service="Storage",
                               message="Storage check failed.", detected_at=now))
    return alerts


# ----------------------------- activity -----------------------------
def list_activity(db: Session, limit: int = 8) -> list[ActivityOut]:
    rows, _ = AuditRepository(db).list(limit=limit, offset=0)
    out: list[ActivityOut] = []
    for r in rows:
        target = f"{r.record_type}:{r.record_id}".strip(":") if r.record_type else ""
        out.append(
            ActivityOut(
                id=r.id,
                admin=r.admin_email or "system",
                action=r.description or r.action_type,
                target=target,
                result="success",
                timestamp=r.created_at.isoformat() if r.created_at else "",
            )
        )
    return out


# ----------------------------- overview -----------------------------
def build_overview(db: Session, started_perf: float) -> SystemOverviewOut:
    database = check_database(db)
    openai = check_openai(db)
    elevenlabs = check_elevenlabs(db)
    audio_queue = check_audio_queue()
    storage = check_storage()
    ai_config = get_ai_configuration(db)
    credentials = get_credentials_status(db)
    voices = list_voices(db)
    alerts = build_alerts(database, openai, elevenlabs, storage)
    activity = list_activity(db)

    s = get_settings()
    # Real measured server-side processing time for assembling this response.
    response_time_ms = int((time.perf_counter() - started_perf) * 1000)
    backend = BackendHealthOut(
        status="healthy",
        response_time_ms=response_time_ms,
        version=APP_VERSION,
        environment=s.environment,
        checked_at=_now_iso(),
    )

    return SystemOverviewOut(
        generated_at=_now_iso(),
        backend=backend,
        database=database,
        openai=openai,
        elevenlabs=elevenlabs,
        audio_queue=audio_queue,
        storage=storage,
        ai_config=ai_config,
        credentials=credentials,
        voices=voices,
        alerts=alerts,
        activity=activity,
    )
