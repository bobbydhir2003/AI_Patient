"""Unified voice architecture guardrails (post-ElevenLabs cleanup).

These tests assert the invariants of the single patient-conversation engine:

    Browser -> LiveKit -> OpenAI Realtime (hosted Prompt ID + OpenAI voice) -> LiveKit -> Browser

Specifically:
  - every current patient case maps to an OpenAI Realtime prompt/voice/model;
  - a missing Prompt ID fails loudly (never voices an unconfigured patient);
  - the LiveKit token carries per-session dispatch metadata + an isolated room;
  - the interview-config endpoint advertises only the voice engine (no legacy
    typed-streaming flags);
  - no ElevenLabs / legacy-voice code remains in the live spoken path.
"""
import pathlib

import pytest

from app.core.config import get_settings
from app.core.constants import REFERRAL_CASE_IDS, STANDARD_CASE_IDS
from app.livekit_agent.realtime_patient_configs import (
    PATIENT_CONFIGS,
    PatientConfigError,
    resolve_patient_config,
)

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
_LIVEKIT_DIR = _BACKEND / "app" / "livekit_agent"


# --------------------------------------------------- case -> prompt/voice/model
def test_every_configured_case_resolves_prompt_voice_model(monkeypatch):
    """With prompt IDs set, each configured case resolves to a Realtime config
    carrying a hosted prompt_id, an OpenAI voice, and a model."""
    settings = get_settings()
    for case_id, template in PATIENT_CONFIGS.items():
        monkeypatch.setattr(settings, template["prompt_id_setting"], f"pmpt_{case_id}_123", raising=False)
    for case_id in PATIENT_CONFIGS:
        cfg = resolve_patient_config(case_id, settings)
        assert cfg["prompt_id"] == f"pmpt_{case_id}_123"
        assert cfg["voice"]        # an OpenAI voice is always selected
        assert cfg["model"]        # an OpenAI Realtime model is always selected
        # server_vad tuning is present (OpenAI Realtime owns turn detection).
        assert "silence_duration_ms" in cfg["turn_detection"]


def test_missing_prompt_id_fails_loudly(monkeypatch):
    """An unconfigured Prompt ID must raise, so the worker fails the job closed
    rather than voicing an unconfigured/incorrect patient."""
    settings = get_settings()
    any_case = next(iter(PATIENT_CONFIGS))
    monkeypatch.setattr(
        settings, PATIENT_CONFIGS[any_case]["prompt_id_setting"], "", raising=False
    )
    with pytest.raises(PatientConfigError):
        resolve_patient_config(any_case, settings)


def test_unknown_case_fails_loudly():
    with pytest.raises(PatientConfigError):
        resolve_patient_config("not-a-real-case", get_settings())


def test_standard_cases_have_a_realtime_config():
    """Every standard patient case maps to an OpenAI Realtime config, and every
    configured entry is a real catalog case (no dangling config)."""
    standard = {c.lower() for c in STANDARD_CASE_IDS}
    configured = set(PATIENT_CONFIGS)
    catalog = {c.lower() for c in (*STANDARD_CASE_IDS, *REFERRAL_CASE_IDS)}
    assert standard <= configured, f"standard cases missing a Realtime config: {standard - configured}"
    assert configured <= catalog, f"config references unknown cases: {configured - catalog}"


# --------------------------------------------------- LiveKit token / isolation
def test_livekit_token_gives_each_interview_an_isolated_room(student_client, monkeypatch):
    """Each student interview mints a token for its OWN, per-connection room
    (UUID-suffixed), so no two interviews ever share a LiveKit room. Exercises
    the real endpoint end to end."""
    settings = get_settings()
    monkeypatch.setattr(settings, "livekit_url", "wss://example.livekit.cloud")
    monkeypatch.setattr(settings, "livekit_api_key", "devkey")
    monkeypatch.setattr(settings, "livekit_api_secret", "devsecret-0123456789")
    monkeypatch.setattr(settings, "voice_engine", "livekit")

    sid = student_client.post(
        "/api/sessions", json={"studentName": "LK", "studentId": "", "caseId": "carly"}
    ).json()["sessionId"]

    r1 = student_client.post(f"/api/interviews/{sid}/livekit-token")
    r2 = student_client.post(f"/api/interviews/{sid}/livekit-token")
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    b1, b2 = r1.json(), r2.json()

    assert b1["token"] and b1["roomName"] and b1["connectionId"]
    assert sid in b1["roomName"]                       # room derived from the session
    assert b1["connectionId"] != b2["connectionId"]    # fresh per mint
    assert b1["roomName"] != b2["roomName"]             # isolated rooms, never shared


# --------------------------------------------------- interview config shape
def test_interview_config_advertises_only_voice_engine(student_client):
    r = student_client.get("/api/interviews/config")
    assert r.status_code == 200
    body = r.json()
    assert body == {"voiceEngine": "livekit"}
    # Legacy typed-streaming flags must be gone from the contract.
    assert "streamingEnabled" not in body
    assert "sentencePipeliningEnabled" not in body


# --------------------------------------------------- no legacy voice in the path
def test_no_legacy_voice_imports_in_livekit_path():
    """The live spoken path (app/livekit_agent/) must not IMPORT ElevenLabs, the
    deleted typed-generation modules, or the removed semantic/turn stack. Only
    real import statements are checked - explanatory comments/docstrings that
    describe what was removed are fine."""
    import ast

    banned_substrings = (
        "app.voice", "elevenlabs", "patient_adapter", "native_agent",
        "realtime_turn_controller", "turn_detector",
        "livekit.plugins",  # silero/deepgram plugins
    )
    offenders = []
    for path in _LIVEKIT_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                for bad in banned_substrings:
                    if bad in name:
                        offenders.append(f"{path.name}: imports {name}")
    assert not offenders, "legacy voice imports leaked into the live path:\n" + "\n".join(offenders)


def test_elevenlabs_voice_package_is_removed():
    """The ElevenLabs backend package and voice API must no longer exist."""
    assert not (_BACKEND / "app" / "voice").exists()
    assert not (_BACKEND / "app" / "api" / "voice.py").exists()
