"""Tests for the technical System Dashboard endpoints.

Verifies admin-only access, that every value is real (not fabricated), that
secrets are never leaked, honest 'not configured' states, real audit writes, and
that the academic dashboard is untouched.

Patient interview voice is now provided by OpenAI Realtime, so the dashboard no
longer surfaces ElevenLabs status, per-case voices, an audio queue/cache, or a
TTS concurrency lane.
"""
import json

from app.core.config import get_settings
from app.services.system_service import mask_secret
from tests.test_auth import auth_header, login_token, make_admin, register


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

    # Real storage numbers.
    assert d["storage"]["percentUsed"] is not None


def test_service_status_is_configured_not_connected(client, engine):
    """OpenAI must never claim 'connected' just because a key exists."""
    tok = admin_token(client, engine)
    d = client.get("/api/admin/system/overview", headers=auth_header(tok)).json()
    assert d["openai"]["status"] in ("configured", "not_configured")
    assert d["openai"]["status"] != "connected"
    # ElevenLabs is fully removed from the dashboard.
    assert "elevenlabs" not in d
    assert "voices" not in d
    assert "audioQueue" not in d


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
    # Whatever the real key is, the full value must never be in the payload.
    if settings.openai_api_key:
        assert settings.openai_api_key not in body


def test_mask_secret_reveals_only_head_and_tail():
    assert mask_secret("sk-proj-ABCDEFGH1234") == "sk-p••••1234"
    assert "ABCDEFGH" not in mask_secret("sk-proj-ABCDEFGH1234")
    assert mask_secret("") == ""


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
    # Denominators come from live settings, not example numbers. TTS lane is gone.
    assert conc["openai"]["limit"] == get_settings().max_concurrent_ai_interviews
    assert conc["openai"]["active"] >= 0
    assert "tts" not in conc


def test_realtime_checks_reflect_real_state(client, engine):
    tok = admin_token(client, engine)
    d = client.get("/api/admin/system/overview", headers=auth_header(tok)).json()
    checks = {c["key"]: c for c in d["checks"]}
    # DB is reachable in tests -> healthy; Redis is not configured -> honest.
    assert checks["postgres"]["status"] == "healthy"
    assert checks["redis"]["status"] == "not_configured"
    assert checks["heartbeat"]["status"] == "not_configured"
    # No ElevenLabs infra check anymore.
    assert "elevenlabs" not in checks


def test_live_endpoint_is_lean_and_admin_only(client, engine):
    assert client.get("/api/admin/system/live").status_code == 401
    tok = admin_token(client, engine)
    d = client.get("/api/admin/system/live", headers=auth_header(tok)).json()
    for key in ("backend", "database", "redis", "workers", "concurrency", "checks", "alerts"):
        assert key in d
    # lean: the heavy config sections are NOT in the live payload
    assert "voices" not in d and "credentials" not in d
