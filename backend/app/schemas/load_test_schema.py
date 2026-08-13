"""Schemas for the Load & Capacity Testing admin API (Priority J).

Provider modes and test types are validated against explicit allowlists so the
API can never launch an unknown / paid mode by accident. Real-provider modes
require `confirmRealProvider = true` (enforced in the service).
"""
from pydantic import Field

from app.models.load_test_job import (
    PROVIDER_REAL_OPENAI,
    PROVIDER_REAL_OPENAI_TTS,
    PROVIDER_SIMULATED,
)
from app.schemas.base import CamelModel

TEST_TYPES = (
    "smoke", "concurrent", "ramp", "spike", "stress", "soak", "ai_traffic", "tts_traffic",
    # Realistic voice-capacity test: drives the SSE streaming interview
    # endpoint and issues ONE sequential TTS request per emitted sentence -
    # not a single bulk /messages call - so a passing run actually reflects
    # real students using voice, not just a bulk-message smoke test.
    "streaming_voice",
)
PROVIDER_MODES = (PROVIDER_SIMULATED, PROVIDER_REAL_OPENAI, PROVIDER_REAL_OPENAI_TTS)


class LoadTestCreateRequest(CamelModel):
    test_type: str = Field(default="smoke")
    provider_mode: str = Field(default=PROVIDER_SIMULATED)
    target_users: int = Field(ge=1)
    ramp_seconds: int = Field(default=0, ge=0)
    duration_seconds: int = Field(ge=1)
    # Required (must be true) for any real-provider mode; ignored for simulated.
    confirm_real_provider: bool = Field(default=False)


class LoadTestJobOut(CamelModel):
    id: str
    created_by: str
    environment: str
    test_type: str
    provider_mode: str
    target_users: int
    ramp_seconds: int
    duration_seconds: int
    status: str
    worker_identifier: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None


class LoadTestRecentOut(CamelModel):
    jobs: list[LoadTestJobOut]
