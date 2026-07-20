"""Request/response schemas for the patient voice (TTS) endpoint."""
from pydantic import Field

from app.schemas.base import CamelModel


class SpeechStyleIn(CamelModel):
    """Controlled speech-performance labels (validated against the mapper's
    enums server-side; invalid values fall back to safe defaults)."""

    emotion: str = "neutral"
    pace: str = "normal"
    energy: str = "normal"
    hesitation: str = "none"
    pause_before_ms: int = 150


class VoiceSynthesizeRequest(CamelModel):
    case_id: str = Field(min_length=1, max_length=50)
    # Approved patient text. Length is validated against settings; when
    # session_id + turn_id are provided, the SAVED patient turn is what gets
    # synthesized, so the endpoint can never voice arbitrary frontend text.
    text: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(default="", max_length=64)
    turn_id: str = Field(default="", max_length=64)
    speech_style: SpeechStyleIn | None = None
    # Correlation id for dev latency logs only (streaming pipeline sends the
    # clientTurnId plus the sentence index). Never persisted, never validated
    # against the transcript.
    correlation_id: str = Field(default="", max_length=80)


class VoiceStatusOut(CamelModel):
    """Student-safe availability signal. Never includes voice IDs or keys."""

    case_id: str
    available: bool
    provider: str  # "elevenlabs" when available, otherwise "browser"
    # Speaking rate for the browser speechSynthesis fallback (not sensitive).
    fallback_rate: float = 0.97
