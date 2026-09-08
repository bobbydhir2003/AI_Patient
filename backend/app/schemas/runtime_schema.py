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
