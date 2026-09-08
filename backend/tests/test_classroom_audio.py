"""Classroom audio reliability: focused tests for the three-layer fix.

Layer 1 (frontend):  browser AEC / noise suppression / AGC — tested via
                      TypeScript build (no Python assertion possible here).
Layer 2 (Realtime):   input_audio_noise_reduction = near_field in the
                      prompt_agent session.update payload.
Layer 3 (VAD tuning): threshold=0.65, silence_duration_ms=750, and
                      interrupt_response remains true.

Also confirms: existing case→prompt→voice/model mapping is unchanged.
"""
import pytest

from app.core.config import get_settings, Settings
from app.livekit_agent.realtime_client import build_prompt_agent_session_update
from app.livekit_agent.realtime_patient_configs import (
    PATIENT_CONFIGS,
    _DEFAULT_TURN_DETECTION,
    resolve_patient_config,
)


def _settings(**overrides) -> Settings:
    base = dict(jwt_secret_key="test-secret-at-least-32-characters-long")
    base.update(overrides)
    return Settings(**base)


# -------------------------------------------------------------------
# Layer 3: VAD tuning defaults (single source of truth)
# -------------------------------------------------------------------

def test_default_vad_threshold_is_classroom_safe():
    assert _DEFAULT_TURN_DETECTION["threshold"] == 0.65


def test_default_vad_silence_duration_is_classroom_safe():
    assert _DEFAULT_TURN_DETECTION["silence_duration_ms"] == 750


def test_default_vad_prefix_padding_unchanged():
    assert _DEFAULT_TURN_DETECTION["prefix_padding_ms"] == 300


# -------------------------------------------------------------------
# Layer 2 + 3: prompt_agent session.update payload
# -------------------------------------------------------------------

def _build_prompt_agent_payload():
    """Resolve Carly's config and build the full session.update payload."""
    settings = _settings(
        openai_api_key="sk-test",
        openai_realtime_carly_prompt_id="prompt-test-carly",
        openai_realtime_transcription_model="gpt-4o-mini-transcribe",
    )
    config = resolve_patient_config("carly", settings)
    return build_prompt_agent_session_update(settings, config)


def test_prompt_agent_payload_includes_near_field_noise_reduction():
    payload = _build_prompt_agent_payload()
    audio_input = payload["session"]["audio"]["input"]
    assert audio_input["noise_reduction"] == {"type": "near_field"}


def test_prompt_agent_payload_vad_threshold():
    payload = _build_prompt_agent_payload()
    td = payload["session"]["audio"]["input"]["turn_detection"]
    assert td["threshold"] == 0.65


def test_prompt_agent_payload_vad_silence_duration():
    payload = _build_prompt_agent_payload()
    td = payload["session"]["audio"]["input"]["turn_detection"]
    assert td["silence_duration_ms"] == 750


def test_prompt_agent_payload_interrupt_response_remains_true():
    payload = _build_prompt_agent_payload()
    td = payload["session"]["audio"]["input"]["turn_detection"]
    assert td["interrupt_response"] is True


def test_prompt_agent_payload_create_response_remains_true():
    payload = _build_prompt_agent_payload()
    td = payload["session"]["audio"]["input"]["turn_detection"]
    assert td["create_response"] is True


def test_prompt_agent_payload_server_vad_type():
    payload = _build_prompt_agent_payload()
    td = payload["session"]["audio"]["input"]["turn_detection"]
    assert td["type"] == "server_vad"


# -------------------------------------------------------------------
# Existing case→prompt→voice/model mapping unchanged
# -------------------------------------------------------------------

def test_patient_config_mapping_unchanged(monkeypatch):
    """All four patients still resolve with the correct structural shape:
    prompt_id, voice, model, and turn_detection. The classroom fix must
    not break or alter any patient's identity/voice/model mapping."""
    settings = get_settings()
    for case_id, template in PATIENT_CONFIGS.items():
        monkeypatch.setattr(settings, template["prompt_id_setting"], f"pmpt_{case_id}_test", raising=False)
    for case_id in PATIENT_CONFIGS:
        cfg = resolve_patient_config(case_id, settings)
        assert cfg["prompt_id"] == f"pmpt_{case_id}_test"
        assert cfg["voice"]  # an OpenAI voice
        assert cfg["model"]  # an OpenAI Realtime model
        assert cfg["turn_detection"]["threshold"] == 0.65
        assert cfg["turn_detection"]["silence_duration_ms"] == 750
        assert cfg["turn_detection"]["prefix_padding_ms"] == 300


def test_noise_reduction_not_in_non_prompt_agent_path():
    """The legacy/semantic_vad build_session_update path (used by non-prompt_agent
    mode, currently inactive) must NOT gain noise_reduction — it was never part
    of that config and adding it there is out of scope."""
    from app.livekit_agent.realtime_client import build_session_update
    settings = _settings(
        openai_realtime_model="gpt-realtime",
        openai_realtime_voice="sage",
        openai_realtime_semantic_eagerness="low",
        openai_realtime_transcription_model="gpt-4o-mini-transcribe",
    )
    payload = build_session_update(settings)
    audio_input = payload["session"]["audio"]["input"]
    assert "noise_reduction" not in audio_input
