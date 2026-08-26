"""Request/response schemas for the patient voice (TTS) endpoint."""
from typing import Literal

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


# Allowlisted client voice-diagnostic event names. Validated (not free text) so
# the telemetry endpoint cannot be used to inject arbitrary log lines - an
# unknown event name is simply rejected (422) rather than logged.
VoiceTelemetryEventName = Literal[
    "voice_status_ok",
    "voice_status_failed",
    "voice_status_confirmed_unavailable",
    "tts_requested",
    "tts_request_started",
    "tts_succeeded",
    "tts_fetch_failed",
    "tts_http_success",
    "tts_http_failed",
    "tts_empty_audio",
    "audio_blob_ready",
    "audio_decode_failed",
    "audio_play_started",
    "audio_play_success",
    "audio_play_failed",
    "audio_media_source_failed",
    "audio_source_buffer_failed",
    "audio_progressive_start_timeout",
    "mobile_buffered_first",
    "audio_user_gesture_recovery_offered",
    "audio_user_gesture_recovery_clicked",
    "audio_user_gesture_recovery_success",
    "audio_user_gesture_recovery_failed",
    "browser_fallback_started",
    "tts_capacity_retry_scheduled",
    "tts_capacity_retry_started",
    "tts_capacity_retry_succeeded",
    "tts_capacity_retry_failed",
    "tts_cancelled",
    # Phase 1 LiveKit POC only - never emitted by the production voice path.
    "livekit_room_connecting",
    "livekit_room_connected",
    "livekit_room_disconnected",
    "livekit_room_reconnecting",
    "livekit_room_reconnected",
    "livekit_mic_published",
    "livekit_patient_track_subscribed",
    "livekit_agent_started",
    "livekit_patient_audio_started",
    "livekit_patient_audio_completed",
    "livekit_patient_audio_failed",
    # Phase C1: readiness/timeout diagnosability (real student LiveKit path).
    "livekit_first_turn_sent",
    "livekit_turn_status_received",
    "livekit_turn_status_matched",
    "livekit_turn_status_ignored",
    "livekit_thinking_timeout_started",
    "livekit_thinking_timeout_cancelled",
    "livekit_thinking_timeout_fired",
    "livekit_audio_element_attached",
    "livekit_audio_playing",
    "livekit_audio_play_failed",
    "livekit_engine_error",
]


class VoiceTelemetryEvent(CamelModel):
    """A single client-side voice-diagnostic event (see voiceDiagnostics.ts).

    Deliberately narrow: no field exists for patient text, transcript content,
    audio bytes, or any secret - there is nothing here to add that would leak
    one, even by mistake. Every field is either an enum, a bounded-length
    operational string, or a bounded number.
    """

    event: VoiceTelemetryEventName
    # Reuses VoiceSynthesizeRequest's existing correlation_id convention so a
    # browser event can be joined to the matching tts_request_start/complete
    # backend log line for the SAME turn.
    correlation_id: str = Field(default="", max_length=80)
    case_id: str = Field(default="", max_length=50)
    status: int | None = Field(default=None, ge=0, le=599)
    category: str = Field(default="", max_length=64)
    device_category: str = Field(default="", max_length=16)
    playback_method: str = Field(default="", max_length=32)
    duration_ms: float | None = Field(default=None, ge=0, le=120_000)
    # Phase C1: LiveKitPocEngine's PocState at event time (e.g. "thinking"),
    # and the patient_turn_status payload's status / diagnostic outcome (e.g.
    # "speaking_started", "client_turn_id_mismatch"). Bounded operational
    # strings only - never patient content.
    engine_state: str = Field(default="", max_length=32)
    turn_status: str = Field(default="", max_length=32)
