from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.base import CamelModel


class MessageOut(CamelModel):
    id: str
    sender: str  # student | patient
    text: str
    timestamp: datetime


class StudentMessageRequest(CamelModel):
    text: str = Field(min_length=1, max_length=2000)
    # Route/UI case id; must match the session's case (cross-case isolation guard).
    case_id: str = Field(min_length=1, max_length=50)
    # Frontend-generated id making the exchange idempotent across retries.
    client_turn_id: str = Field(default="", max_length=64)
    source: str = Field(default="typed", pattern="^(typed|speech)$")


class TurnCreateRequest(CamelModel):
    client_turn_id: str = Field(min_length=1, max_length=64)
    speaker: str = Field(pattern="^(student|patient)$")
    content: str = Field(min_length=1, max_length=4000)
    source: str = Field(default="typed", pattern="^(typed|speech|openai|system)$")


class TurnOut(CamelModel):
    id: str
    session_id: str
    client_turn_id: str | None
    speaker: str
    content: str
    source: str | None
    turn_index: int
    created_at: datetime


# Controlled speech-performance enums. These labels only shape HOW the approved
# text sounds (delivery); they can never change WHAT the patient reveals.
SPEECH_EMOTIONS = (
    "neutral", "warm", "relieved", "worried", "anxious",
    "frustrated", "guarded", "sad", "tearful", "confused",
)
SPEECH_PACES = ("very_slow", "slow", "normal", "fast")
SPEECH_ENERGIES = ("low", "normal", "high")
SPEECH_HESITATIONS = ("none", "mild", "moderate")


class PatientSpeech(BaseModel):
    """Optional speech-performance metadata from the model (internal only).
    Values outside the controlled enums are normalized to defaults later."""

    emotion: str = "neutral"
    pace: str = "normal"
    energy: str = "normal"
    hesitation: str = "none"
    pause_before_ms: int = 150


class PatientReply(BaseModel):
    """Structured output required from the OpenAI model (internal only)."""

    patient_text: str = Field(min_length=1)
    used_fact_ids: list[str] = Field(default_factory=list)
    response_type: Literal[
        "greeting", "small_talk", "clinical_answer", "follow_up_answer", "uncertain", "out_of_scope"
    ]
    supported: bool = True
    speech: PatientSpeech | None = None


# JSON schema handed to the OpenAI Responses API (strict mode).
# Strict mode requires every property to be listed in "required"; optionality
# for "speech" is expressed via the nullable type ["object", "null"].
PATIENT_REPLY_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "patient_text": {"type": "string"},
        "used_fact_ids": {"type": "array", "items": {"type": "string"}},
        "response_type": {
            "type": "string",
            "enum": ["greeting", "small_talk", "clinical_answer", "follow_up_answer", "uncertain", "out_of_scope"],
        },
        "supported": {"type": "boolean"},
        "speech": {
            "type": ["object", "null"],
            "properties": {
                "emotion": {"type": "string", "enum": list(SPEECH_EMOTIONS)},
                "pace": {"type": "string", "enum": list(SPEECH_PACES)},
                "energy": {"type": "string", "enum": list(SPEECH_ENERGIES)},
                "hesitation": {"type": "string", "enum": list(SPEECH_HESITATIONS)},
                "pause_before_ms": {"type": "integer"},
            },
            "required": ["emotion", "pace", "energy", "hesitation", "pause_before_ms"],
            "additionalProperties": False,
        },
    },
    "required": ["patient_text", "used_fact_ids", "response_type", "supported", "speech"],
    "additionalProperties": False,
}


class SpeechStyleOut(CamelModel):
    """Normalized speech-performance labels sent to the frontend (safe enums
    only - never raw numeric ElevenLabs settings)."""

    emotion: str = "neutral"
    pace: str = "normal"
    energy: str = "normal"
    hesitation: str = "none"
    pause_before_ms: int = 150


class InterviewConfigOut(CamelModel):
    """Student-safe interview feature flags (never keys or internal config)."""

    streaming_enabled: bool = False
    sentence_pipelining_enabled: bool = False


class TurnResponse(CamelModel):
    """What the frontend receives. Internal fact IDs / prompts are never exposed."""

    turn_id: str
    patient_text: str
    status: str  # "completed"
    session_status: str
    # Controlled delivery metadata for TTS; None on idempotent replays or when
    # the model omitted it (the case's default voice profile applies).
    speech: SpeechStyleOut | None = None
