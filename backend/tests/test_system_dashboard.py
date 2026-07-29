"""Tests for the technical System Dashboard endpoints.

Verifies admin-only access, that every value is real (not fabricated), that
secrets/voice-ids are never leaked, honest 'not configured' states, real audit
writes, and that the academic dashboard is untouched.
"""
import json

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.database.connection import get_db
from app.services.system_service import mask_secret
from app.voice.elevenlabs_client import get_elevenlabs_client
from tests.conftest import make_client
from tests.test_auth import auth_header, login_token, make_admin, register
from tests.test_voice import FakeElevenLabsClient


def admin_token(client, engine):
    make_admin(engine, email="sysadmin@school.edu", password="adminpass1")
    return login_token(client, "sysadmin@school.edu", "adminpass1")


# ------------------------------------------------------------------ authz
def test_system_overview_requires_authentication(client):
    assert client.get("/api/admin/system/overview").status_code == 401


def test_system_overview_forbidden_for_students(client, engine):
    register(client, email="stud@school.edu", password="studpass1", number="S9")
    tok = login_token(client, "stud@school.edu", "studpass1")
    r = client.get("/api/admin/system/overview", headers=auth_header(tok))
    assert r.status_code == 403


# ------------------------------------------------------------------ real health
def test_overview_reports_real_health(client, engine):
    tok = admin_token(client, engine)
    r = client.get("/api/admin/system/overview", headers=auth_header(tok))
    assert r.status_code == 200
    d = r.json()

    assert d["backend"]["status"] == "healthy"
    assert isinstance(d["backend"]["responseTimeMs"], int)  # measured, not hardcoded
    assert d["backend"]["version"]

    assert d["database"]["status"] == "connected"
    assert d["database"]["dbType"] == "sqlite"  # the ACTIVE test database
    assert isinstance(d["database"]["latencyMs"], int)

    # No fabricated queue: honest unavailable state.
    assert d["audioQueue"]["available"] is False
    assert d["audioQueue"]["status"] == "unavailable"

    # Real storage numbers.
    assert d["storage"]["percentUsed"] is not None
    assert d["storage"]["audioCacheMaxEntries"] is not None


def test_service_status_is_configured_not_connected(client, engine):
    """OpenAI/ElevenLabs must never claim 'connected' just because a key exists."""
    tok = admin_token(client, engine)
    d = client.get("/api/admin/system/overview", headers=auth_header(tok)).json()
    assert d["openai"]["status"] in ("configured", "not_configured")
    assert d["elevenlabs"]["status"] in ("configured", "not_configured")
    assert d["openai"]["status"] != "connected"


# ------------------------------------------------------------------ voices
def test_voices_include_mother_as_not_configured_and_mask_ids(client, engine):
    tok = admin_token(client, engine)
    d = client.get("/api/admin/system/overview", headers=auth_header(tok)).json()
    voices = d["voices"]
    labels = [(v["speakerLabel"], v["status"]) for v in voices]

    # Camden has two speakers; the mother has no distinct voice -> not configured.
    assert ("Mother", "not_configured") in labels
    mother = next(v for v in voices if v["speakerLabel"] == "Mother")
    assert mother["maskedVoiceId"] is None

    camden = next(v for v in voices if v["speakerLabel"] == "Camden (Patient)")
    assert camden["status"] == "active"
    assert camden["maskedVoiceId"] and "••••" in camden["maskedVoiceId"]


def test_full_voice_ids_never_leak(client, engine):
    tok = admin_token(client, engine)
    body = json.dumps(client.get("/api/admin/system/overview", headers=auth_header(tok)).json())
    # Full configured voice IDs from the case files must not appear anywhere.
    for full_id in ("x86DtpnPPuq2BpEiKPRy", "aj0fZfXTBc7E3By4X8L2", "MKlLqCItoCkvdhrxgtLv"):
        assert full_id not in body


# ------------------------------------------------------------------ credentials
def test_credentials_are_masked_and_full_key_never_returned(client, engine):
    tok = admin_token(client, engine)
    d = client.get("/api/admin/system/overview", headers=auth_header(tok)).json()
    body = json.dumps(d)
    settings = get_settings()
    for cred in d["credentials"]:
        if cred["configured"]:
            assert cred["maskedValue"] and "••••" in cred["maskedValue"]
        else:
            assert cred["maskedValue"] is None
            assert cred["status"] == "not_configured"
    # Whatever the real keys are, the full value must never be in the payload.
    for key in (settings.openai_api_key, settings.elevenlabs_api_key):
        if key:
            assert key not in body


def test_mask_secret_reveals_only_head_and_tail():
    assert mask_secret("sk-proj-ABCDEFGH1234") == "sk-p••••1234"
    assert "ABCDEFGH" not in mask_secret("sk-proj-ABCDEFGH1234")
    assert mask_secret("") == ""


# ------------------------------------------------------------------ real audit
def test_clear_audio_cache_is_real_and_audited(client, engine):
    tok = admin_token(client, engine)
    r = client.post("/api/admin/system/audio-cache/clear", headers=auth_header(tok))
    assert r.status_code == 200
    assert r.json()["success"] is True

    # The action must appear in the real activity feed (from the audit log).
    d = client.get("/api/admin/system/overview", headers=auth_header(tok)).json()
    actions = " ".join(a["action"].lower() for a in d["activity"])
    assert "audio cache" in actions
    assert any(a["admin"] == "sysadmin@school.edu" for a in d["activity"])


# ------------------------------------------------------------------ voice preview
def _admin_client_with_elevenlabs(engine, fake):
    from app.main import create_app

    app = create_app()
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_elevenlabs_client] = lambda: fake
    from fastapi.testclient import TestClient

    return TestClient(app)


def test_voice_preview_returns_real_audio(engine, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test_preview")
    monkeypatch.setenv("ELEVENLABS_ENABLED", "true")
    get_settings.cache_clear()
    try:
        fake = FakeElevenLabsClient(chunks=(b"ID3preview", b"audio"))
        c = _admin_client_with_elevenlabs(engine, fake)
        make_admin(engine, email="prev@school.edu", password="adminpass1")
        tok = login_token(c, "prev@school.edu", "adminpass1")

        r = c.post("/api/admin/system/voices/camden/preview", headers=auth_header(tok))
        assert r.status_code == 200
        assert r.content  # real audio bytes were streamed
        # A fixed sample sentence (never free-form) was synthesized.
        assert fake.calls and fake.calls[0]["text"] == "Hi, my name is Camden."

        # Unknown case -> 404, not a fabricated success.
        assert c.post("/api/admin/system/voices/nobody/preview", headers=auth_header(tok)).status_code == 404
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------------------ untouched
def test_academic_dashboard_still_works(client, engine):
    tok = admin_token(client, engine)
    r = client.get("/api/admin/dashboard", headers=auth_header(tok))
    assert r.status_code == 200
