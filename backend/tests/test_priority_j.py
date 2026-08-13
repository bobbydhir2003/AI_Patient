"""Priority J tests: Load & Capacity Testing.

Covers RBAC (admin only), create/validation, provider labeling + the
real-provider cost confirmation guard, the single-run conflict guard, isolated
test-account provisioning, the simulated-AI runtime override, stop, persistence
of completed-run metadata, empty states, and the TRANSPARENT capacity analysis
(PASS / PASS_WITH_WARNING / FAIL / INCONCLUSIVE + observed safe capacity that is
computed from the data, never hardcoded).

The load worker itself runs in a separate process in production; these tests
patch the process launch so no real subprocess/server is needed.
"""
import json
import os

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.security import hash_password
from app.models import User
from app.models.load_test_job import LoadTestJob
from app.services import load_capacity_analysis as lca
from app.services import load_test_service as lts
from app.services import runtime_config_service as rc
from tests.conftest import FakeOpenAIClient, bearer, make_client
from tests.test_auth import login_token


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def make_user(engine, *, email, role, password="pw12345678"):
    db = _factory(engine)()
    try:
        u = User(email=email, password_hash=hash_password(password), full_name="X",
                 role=role, is_active=True)
        u.account_status = "ACTIVE"
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


class FakePopen:
    def __init__(self, pid=4242):
        self.pid = pid
        self.signals = []

    def wait(self):
        return 0

    def send_signal(self, sig):
        self.signals.append(sig)


@pytest.fixture(autouse=True)
def _clean_running():
    lts._RUNNING.clear()
    yield
    lts._RUNNING.clear()


@pytest.fixture()
def patched(engine, monkeypatch):
    """A super-admin client with the worker launch + finalize patched so no real
    process spawns, and the controller's own DB factory pointed at the test
    engine."""
    monkeypatch.setattr(lts, "_launch_worker", lambda job, creds_file: FakePopen())
    monkeypatch.setattr(lts, "_monitor", lambda job_id: None)  # no background finalize
    monkeypatch.setattr(lts, "get_session_factory", lambda: _factory(engine))
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        # Two-role model: every admin has full load-testing access. There is no
        # separate super-admin tier anymore.
        make_user(engine, email="adm@s.edu", role="admin")
        yield c, engine


def _super(c):
    # Backwards-compatible name used by the positive-path tests below; it now
    # simply returns a normal admin token (admins have all admin powers).
    return bearer(login_token(c, "adm@s.edu", "pw12345678"))


def _admin(c):
    return bearer(login_token(c, "adm@s.edu", "pw12345678"))


def _student(c):
    from tests.test_auth import register
    register(c, email="stud@s.edu", password="studpass1", number="S1")
    return bearer(login_token(c, "stud@s.edu", "studpass1"))


# ============================ RBAC (admin only) ============================
def test_config_rbac(patched):
    c, _ = patched
    assert c.get("/api/admin/system/load-tests/config").status_code == 401
    assert c.get("/api/admin/system/load-tests/config", headers=_student(c)).status_code == 403
    assert c.get("/api/admin/system/load-tests/config", headers=_admin(c)).status_code == 200


def test_create_forbidden_for_student(patched, engine):
    c, _ = patched
    body = {"testType": "smoke", "providerMode": "SIMULATED_AI", "targetUsers": 5, "durationSeconds": 30}
    # A student is forbidden; a normal admin is allowed (covered elsewhere).
    assert c.post("/api/admin/system/load-tests", json=body, headers=_student(c)).status_code == 403


def test_recent_and_metrics_rbac(patched):
    c, _ = patched
    assert c.get("/api/admin/system/load-tests/recent", headers=_student(c)).status_code == 403
    assert c.get("/api/admin/system/load-tests/recent", headers=_admin(c)).status_code == 200


# ============================ Empty state ============================
def test_recent_empty_state(patched):
    c, _ = patched
    r = c.get("/api/admin/system/load-tests/recent", headers=_super(c))
    assert r.status_code == 200 and r.json() == {"jobs": []}


def test_active_none_initially(patched):
    c, _ = patched
    r = c.get("/api/admin/system/load-tests/active", headers=_super(c))
    assert r.status_code == 200 and r.json() == {"job": None}


# ============================ Validation ============================
def test_target_users_over_cap_rejected(patched):
    c, _ = patched
    r = c.post("/api/admin/system/load-tests", headers=_super(c),
               json={"testType": "smoke", "providerMode": "SIMULATED_AI",
                     "targetUsers": 100000, "durationSeconds": 30})
    assert r.status_code == 422


def test_duration_over_cap_rejected(patched):
    c, _ = patched
    r = c.post("/api/admin/system/load-tests", headers=_super(c),
               json={"testType": "smoke", "providerMode": "SIMULATED_AI",
                     "targetUsers": 5, "durationSeconds": 999999})
    assert r.status_code == 422


def test_unknown_test_type_rejected(patched):
    c, _ = patched
    r = c.post("/api/admin/system/load-tests", headers=_super(c),
               json={"testType": "not_a_type", "providerMode": "SIMULATED_AI",
                     "targetUsers": 5, "durationSeconds": 30})
    assert r.status_code == 422


# ============================ Real-provider cost confirmation ============================
def test_real_provider_requires_confirmation(patched):
    c, _ = patched
    r = c.post("/api/admin/system/load-tests", headers=_super(c),
               json={"testType": "ai_traffic", "providerMode": "REAL_OPENAI",
                     "targetUsers": 3, "durationSeconds": 30})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "load_test_confirmation_required"


def test_real_provider_with_confirmation_starts(patched):
    c, _ = patched
    r = c.post("/api/admin/system/load-tests", headers=_super(c),
               json={"testType": "ai_traffic", "providerMode": "REAL_OPENAI",
                     "targetUsers": 3, "durationSeconds": 30, "confirmRealProvider": True})
    assert r.status_code == 200
    assert r.json()["providerMode"] == "REAL_OPENAI"
    assert r.json()["status"] == "RUNNING"


# ============================ Simulated-AI runtime override ============================
def test_simulated_mode_sets_mock_override(patched, engine):
    c, _ = patched
    r = c.post("/api/admin/system/load-tests", headers=_super(c),
               json={"testType": "smoke", "providerMode": "SIMULATED_AI",
                     "targetUsers": 4, "durationSeconds": 30})
    assert r.status_code == 200 and r.json()["providerMode"] == "SIMULATED_AI"
    db = _factory(engine)()
    try:
        assert rc.mock_ai_enabled(db) is True  # override on for the run
    finally:
        db.close()


# ============================ Single-run conflict guard ============================
def test_single_run_conflict(patched):
    c, _ = patched
    body = {"testType": "smoke", "providerMode": "SIMULATED_AI", "targetUsers": 4, "durationSeconds": 30}
    assert c.post("/api/admin/system/load-tests", json=body, headers=_super(c)).status_code == 200
    r2 = c.post("/api/admin/system/load-tests", json=body, headers=_super(c))
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "load_test_already_running"


# ============================ Isolated test accounts ============================
def test_provision_creates_isolated_users(engine):
    db = _factory(engine)()
    try:
        creds = lts.provision_test_users(db, 5)
        db.commit()
        assert len(creds) == 5
        users = db.query(User).filter(User.is_load_test.is_(True)).all()
        assert len(users) == 5
        for u in users:
            assert u.is_load_test is True
            assert u.account_status == "ACTIVE" and u.is_active is True
            assert u.role == "student"
            assert u.email.endswith("@loadtest.invalid")  # never a real student domain
    finally:
        db.close()


def test_provision_reuses_existing_users(engine):
    db = _factory(engine)()
    try:
        lts.provision_test_users(db, 3)
        db.commit()
        lts.provision_test_users(db, 5)
        db.commit()
        # 5 total, not 3+5 - the first three are reused, not duplicated.
        assert db.query(User).filter(User.is_load_test.is_(True)).count() == 5
    finally:
        db.close()


# ============================ Stop ============================
def test_stop_running_job(patched):
    c, _ = patched
    j = c.post("/api/admin/system/load-tests", headers=_super(c),
               json={"testType": "smoke", "providerMode": "SIMULATED_AI",
                     "targetUsers": 4, "durationSeconds": 30}).json()
    r = c.post(f"/api/admin/system/load-tests/{j['id']}/stop", headers=_super(c))
    assert r.status_code == 200
    # A SIGTERM was delivered to the (fake) worker process.
    assert lts._RUNNING[j["id"]]["popen"].signals  # signal recorded
    assert lts._RUNNING[j["id"]]["cancelled"] is True


# ============================ Persistence of completed metadata ============================
def test_finalize_persists_results(patched, engine, monkeypatch):
    c, _ = patched
    j = c.post("/api/admin/system/load-tests", headers=_super(c),
               json={"testType": "concurrent", "providerMode": "SIMULATED_AI",
                     "targetUsers": 10, "durationSeconds": 60}).json()
    # Simulate the worker having written a real snapshot with healthy samples.
    os.makedirs(lts.METRICS_DIR, exist_ok=True)
    series = [{"t": float(i), "activeUsers": 10, "successRate": 100.0,
               "requestsPerSec": 20.0, "p95": 120, "windowRequests": 20,
               "windowSuccess": 20, "windowFailed": 0} for i in range(1, 8)]
    snap = {
        "status": "COMPLETED",
        "final": {"overall": {"requests": 140, "success": 140, "failed": 0,
                              "networkErrors": 0, "successRate": 100.0,
                              "statusCounts": {"200": 140}, "maxActiveUsers": 10,
                              "latencyMs": {"p50": 90, "p95": 120, "p99": 150},
                              "turnLatencyMs": {"p50": 90, "p95": 120, "p99": 150}}},
        "series": series,
    }
    with open(lts._metrics_path(j["id"]), "w") as f:
        json.dump(snap, f)

    lts._finalize(j["id"])  # normally called by the monitor thread on exit

    db = _factory(engine)()
    try:
        job = db.get(LoadTestJob, j["id"])
        assert job.status == "COMPLETED"
        assert job.results is not None
        results = json.loads(job.results)
        assert results["capacity"]["overallStatus"] in ("PASS", "PASS_WITH_WARNING")
        # Runtime mock override cleared after the run.
        assert rc.mock_ai_enabled(db) is False
    finally:
        db.close()

    # Recent now shows the completed run (no longer empty).
    recent = c.get("/api/admin/system/load-tests/recent", headers=_super(c)).json()
    assert any(x["id"] == j["id"] and x["status"] == "COMPLETED" for x in recent["jobs"])


# ============================ Capacity analysis (transparent, not hardcoded) ============================
def test_analysis_inconclusive_when_too_short():
    out = lca.analyze(target_users=10, duration_seconds=5, ramp_seconds=0,
                      overall={"requests": 3}, series=[])
    assert out["overallStatus"] == "INCONCLUSIVE"
    assert out["recommendedSafeCapacity"]["value"] is None


def _healthy_overall(reqs=200, sr=100.0, p95=120):
    return {"requests": reqs, "success": int(reqs * sr / 100), "failed": 0,
            "networkErrors": 0, "successRate": sr, "statusCounts": {"200": reqs},
            "maxActiveUsers": 20, "latencyMs": {"p50": 90, "p95": p95, "p99": 150},
            "turnLatencyMs": {}}


def test_analysis_pass_and_safe_capacity_from_observed_region():
    series = [{"activeUsers": lvl, "successRate": 100.0, "p95": 120} for lvl in (5, 10, 15, 20)]
    out = lca.analyze(target_users=20, duration_seconds=120, ramp_seconds=10,
                      overall=_healthy_overall(), series=series)
    assert out["overallStatus"] == "PASS"
    # Safe capacity is the observed peak (20), NOT a preset constant.
    assert out["recommendedSafeCapacity"]["value"] == 20


def test_safe_capacity_tracks_the_data_not_a_constant():
    # Degradation appears above 30 users -> safe capacity must reflect 30.
    series = ([{"activeUsers": lvl, "successRate": 100.0, "p95": 200} for lvl in (10, 20, 30)]
              + [{"activeUsers": lvl, "successRate": 80.0, "p95": 9000} for lvl in (40, 50)])
    out = lca.analyze(target_users=50, duration_seconds=180, ramp_seconds=30,
                      overall={"requests": 500, "success": 460, "failed": 40,
                               "networkErrors": 0, "successRate": 92.0,
                               "statusCounts": {"200": 460, "503": 40},
                               "maxActiveUsers": 50, "latencyMs": {"p50": 200, "p95": 9000, "p99": 12000},
                               "turnLatencyMs": {}},
                      series=series)
    assert out["recommendedSafeCapacity"]["value"] == 30
    assert out["overallStatus"] == "FAIL"  # 503s present
    assert out["observedBottleneck"]["observed"] is True


def test_analysis_fail_on_low_success():
    series = [{"activeUsers": lvl, "successRate": 80.0, "p95": 300} for lvl in (10, 15, 20, 20)]
    out = lca.analyze(target_users=20, duration_seconds=120, ramp_seconds=10,
                      overall=_healthy_overall(sr=80.0), series=series)
    assert out["overallStatus"] == "FAIL"


def test_analysis_warns_on_rate_limiting():
    ov = _healthy_overall(sr=99.5)
    ov["statusCounts"] = {"200": 190, "429": 10}
    series = [{"activeUsers": lvl, "successRate": 100.0, "p95": 120} for lvl in (5, 10, 15, 20)]
    out = lca.analyze(target_users=20, duration_seconds=120, ramp_seconds=10, overall=ov, series=series)
    assert out["overallStatus"] == "PASS_WITH_WARNING"
    assert out["observedBottleneck"]["kind"] == "rate_limiting"


# ==================== streaming_voice realistic voice-capacity mode ====================
def test_streaming_voice_is_a_known_test_type():
    from app.schemas.load_test_schema import TEST_TYPES

    assert "streaming_voice" in TEST_TYPES


def test_streaming_voice_worker_params_enable_tts_and_disable_assessment():
    params = lts._derive_worker_params("streaming_voice", "SIMULATED_AI")
    assert params["streaming_voice"] is True
    assert params["enable_tts"] is True  # the whole point of this mode
    assert params["assessment"] is False  # measures interview+TTS, not assessment load


def test_other_test_types_are_not_streaming_voice():
    for t in ("smoke", "concurrent", "ai_traffic", "tts_traffic", "stress"):
        assert lts._derive_worker_params(t, "SIMULATED_AI")["streaming_voice"] is False


def test_streaming_voice_launch_passes_streaming_voice_flag(monkeypatch):
    """_launch_worker's argv for a streaming_voice job must include
    --streaming-voice and --enable-tts, and must NOT include --assessment."""
    from app.models.load_test_job import LoadTestJob

    job = LoadTestJob(
        id="job1", created_by="a@x", environment="local", test_type="streaming_voice",
        provider_mode="SIMULATED_AI", target_users=2, ramp_seconds=0, duration_seconds=10,
        status="STARTING",
    )
    captured = {}
    monkeypatch.setattr(
        lts.subprocess, "Popen",
        lambda argv, **kw: (captured.__setitem__("argv", argv), FakePopen())[1],
    )
    lts._launch_worker(job, "/tmp/does-not-need-to-exist.json")
    assert "--streaming-voice" in captured["argv"]
    assert "--enable-tts" in captured["argv"]
    assert "--assessment" not in captured["argv"]
