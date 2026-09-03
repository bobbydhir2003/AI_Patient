"""Provider-specific OpenAI Realtime wrapper + the pure session-config builder.

This is the ONLY module in the POC realtime path that imports the OpenAI SDK's
realtime client, so every other module (realtime_session.py, worker.py) stays
provider-agnostic and unit-testable without a network connection. The wrapper
deliberately exposes a tiny surface - connect / send(dict) / recv() / close() -
matching the low-level `AsyncOpenAI().realtime` connection in openai>=1.107
(verified against the installed 1.109.1: `client.realtime.connect(model=...)`
returns an async context manager yielding a connection with send/recv/close).

Why the low-level send/recv rather than livekit-plugins-openai's RealtimeModel:
the existing worker (see worker.py's PocAgentSession) deliberately does NOT use
LiveKit's high-level AgentSession/RoomIO voice pipeline - it owns its own turn
state machine, DB persistence, disclosure gating and interruption logic. A
direct Realtime WebSocket lets Realtime be the turn-taking/voice brain while the
PT backend stays authoritative over patient CONTENT, which is the whole point of
this POC (see the approved architecture). It also avoids pulling in
livekit-plugins-openai's `av`/codecs extra and the AgentSession rewrite.

No dependency change: openai 1.109.1 (already pinned via openai>=1.40,<2.0 and
satisfying livekit-plugins-openai's own >=1.107.2 realtime floor) ships the
realtime client used here.
"""
from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator, Protocol

from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.core.config import Settings

logger = get_logger("app.livekit_agent.realtime")

# GA Realtime PCM is 24kHz mono (openai.types.realtime RealtimeAudioFormatsParam
# pins `audio/pcm` rate to Literal[24000]). Both the student audio we send and
# the patient audio Realtime returns therefore use this rate - see
# realtime_session.py's ingest configuration and (Phase D) audio publishing.
REALTIME_PCM_SAMPLE_RATE = 24000
_PCM_FORMAT = {"type": "audio/pcm", "rate": REALTIME_PCM_SAMPLE_RATE}


class RealtimeConnectionLike(Protocol):
    """Minimal connection contract both the real OpenAI connection and the
    test fake satisfy - keeps realtime_session.py decoupled from the SDK."""

    async def send(self, event: dict[str, Any]) -> None: ...
    async def recv(self) -> Any: ...
    async def close(self) -> None: ...


def build_session_update(settings: "Settings") -> dict[str, Any]:
    """Pure builder for the ONE session.update event that makes Realtime the
    turn-taking brain but NOT the content author (Phase A core requirement):

      - turn_detection = semantic_vad  -> Realtime decides WHEN the student is
        done, using the words uttered (replaces Silero+Deepgram+SmartTurn+Phase7
        for the POC engine).
      - create_response = false        -> Realtime NEVER auto-generates or speaks
        a patient answer on end-of-turn; the backend calls response.create
        explicitly in a later phase, only AFTER patient_engine has approved text.
      - interrupt_response = false     -> Realtime does not auto-truncate on
        barge-in; the backend stays authoritative over interruption (Phase E).
      - output_modalities = ["audio"]  -> native voice out (used from Phase D;
        harmless in Phase A since no response is ever created).

    Kept a pure function (no I/O) so the exact payload can be asserted in tests
    without a live connection - the single most important correctness check for
    this phase.
    """
    eagerness = settings.openai_realtime_semantic_eagerness
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": settings.openai_realtime_model,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": dict(_PCM_FORMAT),
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": eagerness,
                        "create_response": False,
                        "interrupt_response": False,
                    },
                    "transcription": {"model": settings.openai_realtime_transcription_model},
                },
                "output": {
                    "format": dict(_PCM_FORMAT),
                    "voice": settings.openai_realtime_voice,
                },
            },
        },
    }


NATIVE_ALLOWED_FACTS_TOOL = "get_allowed_patient_facts"
NATIVE_STAGE_RESPONSE_TOOL = "stage_patient_response"


def native_agent_tools() -> list[dict[str, Any]]:
    """The complete, deliberately narrow capability surface for native mode.

    Neither tool accepts a session/case identifier; those are bound to the
    server-side worker job and can therefore never be redirected by model
    arguments to another interview.
    """
    return [
        {
            "type": "function",
            "name": NATIVE_ALLOWED_FACTS_TOOL,
            "description": (
                "Required before every clinical patient answer. Returns only the "
                "patient facts the PT backend authorizes for the current student turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": NATIVE_STAGE_RESPONSE_TOOL,
            "description": (
                "Submit natural patient wording and the fact IDs it uses for backend "
                "authorization. This stages no database mutation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "authorization_id": {"type": "string"},
                    "patient_text": {"type": "string"},
                    "used_fact_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["authorization_id", "patient_text", "used_fact_ids"],
                "additionalProperties": False,
            },
        },
    ]


def build_native_agent_session_update(
    settings: "Settings", *, instructions: str,
) -> dict[str, Any]:
    """Configure the normal Realtime conversation as the native voice agent.

    The first model response for every user turn is forced through the read-only
    fact tool. The worker subsequently forces the staging tool, validates its
    arguments, and only then permits an audio response. This is enforcement,
    not a prompt-only request.
    """
    eagerness = settings.openai_realtime_semantic_eagerness
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": settings.openai_realtime_native_agent_model,
            "instructions": instructions,
            "output_modalities": ["audio"],
            "tools": native_agent_tools(),
            "tool_choice": {
                "type": "function", "name": NATIVE_ALLOWED_FACTS_TOOL,
            },
            "audio": {
                "input": {
                    "format": dict(_PCM_FORMAT),
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": eagerness,
                        "create_response": True,
                        # LiveKit, not OpenAI, owns the playback buffer. The
                        # worker reacts to Realtime's speech_started signal by
                        # clearing that queue, targeting response.cancel, and
                        # truncating conversation audio to the delivered point.
                        "interrupt_response": False,
                    },
                    "transcription": {"model": settings.openai_realtime_transcription_model},
                },
                "output": {
                    "format": dict(_PCM_FORMAT),
                    "voice": settings.openai_realtime_voice,
                },
            },
        },
    }


def encode_audio_append(pcm16_bytes: bytes) -> dict[str, Any]:
    """The input_audio_buffer.append client event for one chunk of 24kHz mono
    PCM16 student audio (base64 as the wire format requires)."""
    return {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(pcm16_bytes).decode("ascii"),
    }


class OpenAIRealtimeClient:
    """Default (real) connection factory. Injected into RealtimeSession so tests
    can substitute a fake with the same `connect()` contract."""

    def __init__(self, *, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[RealtimeConnectionLike]:
        # Imported lazily so importing this module (and thus worker.py) never
        # requires the realtime extra until the engine is actually switched on.
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self._api_key)
        async with client.realtime.connect(model=self._model) as conn:
            yield conn
