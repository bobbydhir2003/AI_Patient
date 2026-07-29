"""Student-safe / secret-safe schemas for the technical System Dashboard.

Every value surfaced here comes from a REAL runtime check or real recorded
activity. These models never carry API keys, full voice IDs, connection
strings, or filesystem paths.
"""
from app.schemas.base import CamelModel


class BackendHealthOut(CamelModel):
    status: str  # healthy | degraded | unavailable
    response_time_ms: int | None = None
    version: str = ""
    environment: str = ""
    checked_at: str = ""


class DatabaseHealthOut(CamelModel):
    status: str  # connected | unavailable
    db_type: str = ""  # e.g. sqlite | postgresql
    latency_ms: int | None = None
    migration_version: str | None = None  # None => not available/unknown
    checked_at: str = ""


class ServiceHealthOut(CamelModel):
    """OpenAI / ElevenLabs. `status` is 'configured' or 'not_configured' - never
    'connected' unless a real Test Connection has actually run (deferred)."""

    service: str
    configured: bool
    status: str  # configured | not_configured
    model: str = ""
    streaming_enabled: bool | None = None
    last_success_at: str | None = None  # None => never recorded
    last_error: str | None = None
    checked_at: str = ""


class AudioQueueHealthOut(CamelModel):
    available: bool
    status: str  # ok | warning | unavailable
    pending: int | None = None
    processing: int | None = None
    failed: int | None = None
    message: str = ""
    checked_at: str = ""


class StorageHealthOut(CamelModel):
    status: str  # healthy | warning | unavailable
    used_bytes: int | None = None
    total_bytes: int | None = None
    free_bytes: int | None = None
    percent_used: float | None = None
    audio_cache_entries: int | None = None
    audio_cache_max_entries: int | None = None
    audio_cache_bytes: int | None = None
    checked_at: str = ""


class OpenAIConfigOut(CamelModel):
    configured: bool
    model: str = ""
    streaming_enabled: bool = False
    timeout_seconds: float | None = None
    max_output_tokens: int | None = None
    status: str = ""


class ElevenLabsConfigOut(CamelModel):
    configured: bool
    enabled: bool = False
    model: str = ""
    output_format: str = ""
    timeout_seconds: float | None = None
    status: str = ""


class ConversationSettingsOut(CamelModel):
    """Real, active conversation values. Read-only: these reflect what the
    backend actually uses. Non-toggleable behaviors are marked accordingly."""

    sentence_level_streaming: str  # Enabled | Disabled
    patient_streaming: str  # Enabled | Disabled
    disclosure_control: str
    motivational_interviewing: str
    age_appropriate_language: str
    caregiver_routing: str
    max_patient_response_chars: int


class AiConfigurationOut(CamelModel):
    openai: OpenAIConfigOut
    elevenlabs: ElevenLabsConfigOut
    conversation: ConversationSettingsOut


class VoiceRowOut(CamelModel):
    case_id: str
    speaker_id: str  # patient | caregiver
    patient_name: str
    speaker_label: str
    image: str = ""
    voice_name: str | None = None
    masked_voice_id: str | None = None  # None => not configured
    model: str | None = None
    status: str  # active | not_configured | disabled | unavailable
    reason: str = ""


class CredentialStatusOut(CamelModel):
    """Status only. The full key is NEVER returned. 'updatedAt'/'updatedBy' are
    null because key-change history is not tracked in this deployment."""

    service: str
    configured: bool
    masked_value: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None
    status: str  # configured | not_configured


class AlertOut(CamelModel):
    id: str
    severity: str  # info | warning | critical
    service: str
    message: str
    detected_at: str
    state: str = "active"
    count: int = 1


class ActivityOut(CamelModel):
    id: str
    admin: str
    action: str
    target: str = ""
    result: str = "success"
    timestamp: str


class SystemOverviewOut(CamelModel):
    generated_at: str
    backend: BackendHealthOut
    database: DatabaseHealthOut
    openai: ServiceHealthOut
    elevenlabs: ServiceHealthOut
    audio_queue: AudioQueueHealthOut
    storage: StorageHealthOut
    ai_config: AiConfigurationOut
    credentials: list[CredentialStatusOut]
    voices: list[VoiceRowOut]
    alerts: list[AlertOut]
    activity: list[ActivityOut]


class VoiceListOut(CamelModel):
    voices: list[VoiceRowOut]


class MutationResultOut(CamelModel):
    success: bool
    message: str = ""
