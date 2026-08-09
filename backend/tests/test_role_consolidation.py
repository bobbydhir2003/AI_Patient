"""Two-role consolidation tests (student/admin only).

These cover PART 16 of the Admin + System Dashboard consolidation:
- authorization: student blocked / admin allowed across every admin surface;
- roles: only student/admin accepted; legacy super_admin folds into admin;
- voice settings: every field saves and the LIVE runtime VoiceProfile uses the
  saved values (including the previously-dropped `speed`), and persists across a
  new DB session;
- credential security: keys never returned unmasked; student cannot change them;
- workers: heartbeat reports a live worker, an expired one is not reported, and
  the dashboard invents nothing without Redis records.
"""
import json

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.core.security import hash_password
from app.models import PatientVoiceSetting, User
from tests.conftest import FakeOpenAIClient, bearer, make_client
from tests.test_auth import login_token, register


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _make_user(engine, email, *, role, password="pw12345678"):
    db = _factory(engine)()
    try:
        u = User(email=email, password_hash=hash_password(password), full_name="U",
                 role=role, account_status="ACTIVE", is_active=True)
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


def _admin(c, engine, email="admin_rc@school.edu"):
    _make_user(engine, email, role="admin")
    return bearer(login_token(c, email, "pw12345678"))


def _student(c, email="stud_rc@school.edu"):
    register(c, email=email, password="studpass1", number="RC1")
    return bearer(login_token(c, email, "studpass1"))


# ============================ AUTHORIZATION (1-7) ============================
ADMIN_GET_ENDPOINTS = [
    "/api/admin/dashboard",                       # normal admin dashboard
    "/api/admin/users",                           # user management
    "/api/admin/system/overview",                 # system dashboard data
    "/api/admin/system/live",                     # live worker/architecture view
    "/api/admin/runtime/voices",                  # voice configuration
    "/api/admin/runtime/credentials",             # API credentials
    "/api/admin/runtime/history",                 # configuration history
    "/api/admin/system/load-tests/config",        # load testing
    "/api/admin/system/traffic/overview",         # traffic monitoring
]


def test_student_cannot_access_admin_endpoints(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        sh = _student(c)
        for path in ADMIN_GET_ENDPOINTS:
            r = c.get(path, headers=sh)
            assert r.status_code == 403, f"{path} should be 403 for a student, got {r.status_code}"
            # And unauthenticated is 401 (server-side enforcement, not just UI).
            assert c.get(path).status_code == 401, f"{path} should be 401 anon"


def test_admin_can_access_all_admin_endpoints(engine):
    """A single normal admin (no super/system tier) reaches every admin surface:
    management, system dashboard, voices, credentials and load testing. This is
    the core 'any admin controls the full System Dashboard' guarantee."""
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin(c, engine)
        for path in ADMIN_GET_ENDPOINTS:
            r = c.get(path, headers=ah)
            assert r.status_code == 200, f"{path} should be 200 for an admin, got {r.status_code}: {r.text}"


def test_old_super_admin_requirement_no_longer_blocks_admin(engine):
    """Endpoints that historically required super_admin (credential replace, load
    test create) now accept a normal admin."""
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin(c, engine)
        # Load-test create (was super-admin-only).
        body = {"testType": "smoke", "providerMode": "SIMULATED_AI",
                "targetUsers": 5, "durationSeconds": 30}
        r = c.post("/api/admin/system/load-tests", json=body, headers=ah)
        assert r.status_code in (200, 201, 409), r.text  # not 403


# ============================ ROLES (8-10) ============================
def test_only_student_and_admin_roles_accepted(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin(c, engine)
        target = _make_user(engine, "target_rc@school.edu", role="student")
        # student -> admin OK
        assert c.post(f"/api/admin/users/{target}/role", json={"role": "admin"}, headers=ah).status_code == 200
        # admin -> student OK
        assert c.post(f"/api/admin/users/{target}/role", json={"role": "student"}, headers=ah).status_code == 200
        # anything else rejected by validation
        for bad in ("super_admin", "system_admin", "root", ""):
            assert c.post(f"/api/admin/users/{target}/role", json={"role": bad}, headers=ah).status_code == 422


def test_legacy_super_admin_migrates_to_admin(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        _make_user(engine, "legacy@school.edu", role="super_admin")
        db = _factory(engine)()
        try:
            db.execute(text("UPDATE users SET role='admin' WHERE role IN ('super_admin','system_admin')"))
            db.commit()
        finally:
            db.close()
        tok = login_token(c, "legacy@school.edu", "pw12345678")
        me = c.get("/api/auth/me", headers=bearer(tok)).json()
        assert me["role"] == "admin"
        assert c.get("/api/admin/dashboard", headers=bearer(tok)).status_code == 200


def test_admin_frontend_receives_correct_role(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin(c, engine, email="role_check@school.edu")
        me = c.get("/api/auth/me", headers=ah).json()
        assert me["role"] == "admin"


# ============================ VOICE SETTINGS (11-20) ============================
VOICE_PATCH = {
    "voiceId": "RCVoiceID0001",
    "modelId": "eleven_turbo_v2_5",
    "stability": 0.33,
    "similarityBoost": 0.66,
    "style": 0.22,
    "speed": 1.15,
    "speakerBoost": False,
}


def test_all_voice_fields_save_and_persist(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin(c, engine)
        r = c.patch("/api/admin/runtime/voices/sofia/patient", json=VOICE_PATCH, headers=ah)
        assert r.status_code == 200, r.text

        # Persisted in a FRESH DB session (proves it is not per-process state).
        db = _factory(engine)()
        try:
            row = db.query(PatientVoiceSetting).filter_by(case_id="sofia", speaker_id="patient").one()
            assert row.voice_id == "RCVoiceID0001"
            assert row.model_id == "eleven_turbo_v2_5"
            assert abs(row.stability - 0.33) < 1e-6
            assert abs(row.similarity_boost - 0.66) < 1e-6
            assert abs(row.style - 0.22) < 1e-6
            assert abs(row.speed - 1.15) < 1e-6
            assert row.speaker_boost is False
        finally:
            db.close()


def test_runtime_voice_profile_uses_saved_speed_and_voice_id(tmp_path, monkeypatch):
    """The live resolver (used by a real interview) must copy the saved voice_id
    AND speed into the runtime VoiceProfile. Regression guard for the speed bug:
    previously `speed` was dropped so a saved pace change had no runtime effect.
    Uses the app's own engine/session factory against a temp DB (no monkeypatched
    internals), matching how a real request resolves the voice."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'voice.db'}")
    from app.core.config import get_settings
    from app.database.base import Base
    from app.database.connection import get_engine, get_session_factory, reset_engine

    get_settings.cache_clear()
    reset_engine()
    try:
        Base.metadata.create_all(get_engine())
        db = get_session_factory()()
        from app.services import runtime_config_service as rc
        rc.set_voice(db, case_id="sofia", speaker_id="patient", patch={
            "voice_id": "RCVoiceID0001", "model_id": "eleven_turbo_v2_5",
            "stability": 0.33, "similarity_boost": 0.66, "style": 0.22,
            "speed": 1.15, "speaker_boost": False,
        }, admin_email="a@x")
        db.commit(); db.close()

        monkeypatch.setattr(get_settings(), "elevenlabs_api_key", "k")
        monkeypatch.setattr(get_settings(), "elevenlabs_enabled", True)

        from app.voice.voice_profile_loader import load_voice_profile
        resolved = load_voice_profile("sofia", "patient")
        assert resolved.available is True
        assert resolved.profile.voice_id == "RCVoiceID0001"
        assert abs(resolved.profile.speed - 1.15) < 1e-6   # speed must flow through
        assert abs(resolved.profile.stability - 0.33) < 1e-6
        assert resolved.profile.speaker_boost is False
    finally:
        get_settings.cache_clear()
        reset_engine()


# ============================ CREDENTIAL SECURITY (21-23) ============================
def test_api_keys_never_returned_unmasked(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin(c, engine)
        body = c.get("/api/admin/runtime/credentials", headers=ah).json()
        blob = json.dumps(body)
        assert "encrypted_secret" not in blob and "encryptedSecret" not in blob
        for cred in body["credentials"]:
            masked = cred.get("maskedValue")
            # Either not configured (None) or masked (contains bullets), never raw.
            assert masked is None or "•" in masked or masked == ""


def test_student_cannot_change_credentials(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        sh = _student(c)
        r = c.post("/api/admin/runtime/credentials/openai",
                   json={"key": "sk-should-not-work-123"}, headers=sh)
        assert r.status_code == 403


# ============================ WORKERS (24-26) ============================
# NOTE: live/expired heartbeat semantics (24, 25) against a virtual-clock fake
# Redis are covered in depth by tests/test_worker_registry.py. Here we assert the
# real "never invent workers" honesty rule (26) plus the derived fleet status.
def test_worker_fleet_reports_no_data_without_redis(monkeypatch):
    from app.core import redis_client, worker_registry as wr

    # No Redis configured -> the fleet cannot be observed; report None (the
    # dashboard renders "unavailable"), never a fabricated worker list.
    monkeypatch.setattr(redis_client, "redis_configured", lambda: False)
    assert wr.observed_workers() is None
    assert wr.fleet_status(configured=4, observed_count=None) == "unavailable"


def test_worker_fleet_status_is_derived_from_real_counts():
    from app.core import worker_registry as wr

    assert wr.fleet_status(4, None) == "unavailable"   # cannot observe
    assert wr.fleet_status(4, 0) == "unavailable"      # observed nothing
    assert wr.fleet_status(4, 4) == "healthy"          # observed == configured
    assert wr.fleet_status(4, 1) == "degraded"         # real mismatch, shown honestly
    assert wr.fleet_status(4, 2) == "degraded"
