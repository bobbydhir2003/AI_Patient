"""Request/response schemas for runtime configuration editing.

Responses serialize to camelCase for the frontend. No schema here ever carries a
raw secret - only masked metadata.
"""
from pydantic import Field

from app.schemas.base import CamelModel


# ------------------------------ requests ------------------------------
# Inherit CamelModel so the frontend can POST camelCase (voiceId, maxOutputTokens)
# while the Python fields stay snake_case (populate_by_name accepts both).
class CredentialReplaceIn(CamelModel):
    key: str = Field(min_length=1)


class OpenAIConfigPatchIn(CamelModel):
    model: str | None = None
    timeout_seconds: float | None = None
    max_output_tokens: int | None = None
    streaming_enabled: bool | None = None


class ElevenLabsConfigPatchIn(CamelModel):
    enabled: bool | None = None
    model: str | None = None
    output_format: str | None = None
    timeout_seconds: float | None = None


class ConversationPatchIn(CamelModel):
    sentence_level_streaming: bool | None = None
    patient_streaming: bool | None = None


class VoicePatchIn(CamelModel):
    display_name: str | None = None
    voice_id: str | None = None
    voice_name: str | None = None
    model_id: str | None = None
    stability: float | None = None
    similarity_boost: float | None = None
    style: float | None = None
    speed: float | None = None
    speaker_boost: bool | None = None
    preview_text: str | None = None
    is_active: bool | None = None
    expected_updated_at: str | None = None  # optimistic-lock token


# ------------------------------ responses ------------------------------
class ApplyResult(CamelModel):
    success: bool
    apply_mode: str = ""
    message: str = ""


class CredentialStatusOut(CamelModel):
    service: str
    configured: bool
    source: str = "none"
    masked_value: str | None = None
    last_test_status: str = "never"
    last_test_message: str = ""
    last_tested_at: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None
    status: str
    # Part 2: false when CONFIG_ENCRYPTION_KEY is unset -> Replace Key is unavailable.
    secure_storage_available: bool = False


class CredentialListOut(CamelModel):
    credentials: list[CredentialStatusOut]


class TestResultOut(CamelModel):
    service: str
    status: str  # success | failed | not_configured
    message: str = ""


class VoiceRowOut(CamelModel):
    case_id: str
    speaker_id: str
    patient_name: str
    speaker_label: str
    image: str = ""
    display_name: str = ""
    voice_name: str | None = None
    masked_voice_id: str | None = None
    model: str | None = None
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.1
    speed: float = 1.0
    speaker_boost: bool = True
    preview_text: str = ""
    status: str
    source: str = "none"
    has_override: bool = False
    updated_at: str | None = None
    updated_by: str | None = None


class VoiceListOut(CamelModel):
    voices: list[VoiceRowOut]


class HistoryItemOut(CamelModel):
    id: str
    type: str
    key: str = ""
    entity_id: str = ""
    previous_value: str = ""
    new_value: str = ""
    changed_by: str = ""
    changed_at: str = ""


class HistoryListOut(CamelModel):
    history: list[HistoryItemOut]
