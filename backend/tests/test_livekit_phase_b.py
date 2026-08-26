"""Tests for Phase B's backend surface: GET /api/interviews/config now also
reports voice_engine, so the real InterviewPage can learn which architecture
to use. No other backend behavior changed in Phase B - the student-safe
token endpoint (POST /api/interviews/{session_id}/livekit-token) and the
VOICE_ENGINE flag's own validation are already covered by
test_livekit_phase_a.py.
"""
from app.core.config import get_settings
from tests.conftest import make_client
from tests.test_auth import auth_header, login_token, register


def _student_client(engine, *, email="phaseb@school.edu", number="PB1"):
    client = make_client(engine, authenticate=False)
    register(client, email=email, password="studpass1", number=number)
    token = login_token(client, email, "studpass1")
    client.headers.update(auth_header(token))
    return client


def test_interview_config_reports_legacy_by_default(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "voice_engine", "legacy")
    client = _student_client(engine)
    r = client.get("/api/interviews/config")
    assert r.status_code == 200, r.text
    assert r.json()["voiceEngine"] == "legacy"


def test_interview_config_reports_livekit_when_configured(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "voice_engine", "livekit")
    client = _student_client(engine)
    r = client.get("/api/interviews/config")
    assert r.status_code == 200, r.text
    assert r.json()["voiceEngine"] == "livekit"


def test_interview_config_requires_authentication(engine):
    client = make_client(engine, authenticate=False)
    r = client.get("/api/interviews/config")
    assert r.status_code == 401


def test_interview_config_never_leaks_livekit_credentials(engine, monkeypatch):
    """The config endpoint is student-safe by design (see its docstring) -
    confirm the LiveKit fields added in Phase 1/2/A never leak through it,
    even when they are configured."""
    settings = get_settings()
    monkeypatch.setattr(settings, "voice_engine", "livekit")
    monkeypatch.setattr(settings, "livekit_api_key", "should-never-appear")
    monkeypatch.setattr(settings, "livekit_api_secret", "should-never-appear-either")
    client = _student_client(engine)
    r = client.get("/api/interviews/config")
    assert r.status_code == 200
    body_keys = set(r.json().keys())
    assert body_keys == {"streamingEnabled", "sentencePipeliningEnabled", "voiceEngine"}
    assert "should-never-appear" not in r.text
