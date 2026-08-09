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
def test_voices_include_camden_mother_only_and_mask_ids(client, engine):
    tok = admin_token(client, engine)
    d = client.get("/api/admin/system/overview", headers=auth_header(tok)).json()
    voices = d["voices"]
    labels = [v["speakerLabel"] for v in voices]

    # Camden exposes a SINGLE voice speaker: his mother (sourced from the case
    # file). There is no separate Camden child ("patient") voice entry.
    assert "Camden's Mother" in labels
    assert "Camden (Patient)" not in labels
    mother = next(v for v in voices if v["speakerLabel"] == "Camden's Mother")
    assert mother["status"] == "active"
    assert mother["maskedVoiceId"] and "••••" in mother["maskedVoiceId"]


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
        # Camden is voiced by his mother, so the preview auditions HER fixed sample.
        assert fake.calls and fake.calls[0]["text"] == "Hi, I'm Camden's mother. I can help answer your questions."

        # Unknown case -> 404, not a fabricated success.
        assert c.post("/api/admin/system/voices/nobody/preview", headers=auth_header(tok)).status_code == 404
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------------------ untouched
def test_academic_dashboard_still_works(client, engine):
    tok = admin_token(client, engine)
    r = client.get("/api/admin/dashboard", headers=auth_header(tok))
    assert r.status_code == 200


# --------------------------------------- live worker/concurrency/checks (Part 2)
def test_overview_includes_honest_worker_fleet(client, engine):
    """With no Redis (test default) the overview must report the fleet as
    local_only - never a fabricated 4/4 observed."""
    tok = admin_token(client, engine)
    d = client.get("/api/admin/system/overview", headers=auth_header(tok)).json()
    assert "workers" in d
    w = d["workers"]
    assert w["monitoring"] == "local_only"      # honest: no shared store
    assert w["observed"] is None                # not measurable without Redis
    assert isinstance(w["configured"], int)     # real config value (app_workers)
    # current_task is never fabricated
    assert all(x["currentTask"] is None for x in w["workers"])


def test_overview_concurrency_uses_real_limits(client, engine):
    tok = admin_token(client, engine)
    d = client.get("/api/admin/system/overview", headers=auth_header(tok)).json()
    conc = d["concurrency"]
    # Denominators come from live settings (20 / 10 by default), not from the
    # screenshot's example 300 numbers.
    assert conc["openai"]["limit"] == get_settings().max_concurrent_ai_interviews
    assert conc["tts"]["limit"] == get_settings().max_concurrent_tts_requests
    assert conc["openai"]["active"] >= 0


def test_realtime_checks_reflect_real_state(client, engine):
    tok = admin_token(client, engine)
    d = client.get("/api/admin/system/overview", headers=auth_header(tok)).json()
    checks = {c["key"]: c for c in d["checks"]}
    # DB is reachable in tests -> healthy; Redis is not configured -> honest.
    assert checks["postgres"]["status"] == "healthy"
    assert checks["redis"]["status"] == "not_configured"
    assert checks["heartbeat"]["status"] == "not_configured"


def test_live_endpoint_is_lean_and_admin_only(client, engine):
    assert client.get("/api/admin/system/live").status_code == 401
    tok = admin_token(client, engine)
    d = client.get("/api/admin/system/live", headers=auth_header(tok)).json()
    for key in ("backend", "database", "redis", "workers", "concurrency", "checks", "alerts"):
        assert key in d
    # lean: the heavy config sections are NOT in the live payload
    assert "voices" not in d and "credentials" not in d
