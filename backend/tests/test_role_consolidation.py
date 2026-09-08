"""Two-role consolidation tests (student/admin only).

These cover PART 16 of the Admin + System Dashboard consolidation:
- authorization: student blocked / admin allowed across every admin surface;
- roles: only student/admin accepted; legacy super_admin folds into admin;
- credential security: keys never returned unmasked; student cannot change them;
- workers: heartbeat reports a live worker, an expired one is not reported, and
  the dashboard invents nothing without Redis records.
"""
import json

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.core.security import hash_password
from app.models import User
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


def test_old_super_admin_requirement_no_longer_blocks_admin(engine, monkeypatch):
    """Endpoints that historically required super_admin (credential replace, load
    test create) now accept a normal admin.

    This is an authorization-only check - it has no business spawning a REAL
    load-test subprocess/background thread. Un-patched, load_test_service.
    create_job() does both for real (see _launch_worker/_monitor): the spawned
    daemon thread outlives this test (and its per-test `engine` fixture, torn
    down as soon as this function returns) and its eventual _finalize() call
    lands on the production app.database.connection.get_session_factory() - a
    process-global, lazily-created in-memory SQLite engine that is a DIFFERENT
    object from this test's `engine` and is never initialized with
    Base.metadata.create_all(). That raises an uncaught "no such table:
    load_test_jobs" inside the background thread well after this test has
    already passed, which pytest then attributes to whatever OTHER test
    happens to be running at that moment - confirmed by direct reproduction
    (the spawned thread was still alive 15s after the request returned, and
    querying get_session_factory()'s engine directly reproduces the exact
    error). Patched here exactly like test_priority_j.py's `patched` fixture
    already does for the same subsystem, so no real subprocess/thread/DB
    access can outlive this test.
    """
    from app.services import load_test_service as lts

    class _FakePopen:
        pid = 4242

    monkeypatch.setattr(lts, "_launch_worker", lambda job, creds_file: _FakePopen())
    monkeypatch.setattr(lts, "_monitor", lambda job_id: None)

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
