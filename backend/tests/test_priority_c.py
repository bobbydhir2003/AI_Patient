"""Priority C tests: OpenAI capacity tracking, adaptive assessment throttling,
interview queue metrics, operator insights, and the email access-request flow."""
import types

import pytest

from tests.conftest import FakeOpenAIClient, bearer, make_client, register_student
from tests.test_auth import login_token, make_admin


# ==========================================================================
#  OPENAI CAPACITY TRACKING
# ==========================================================================
def _set_tokens(total: int):
    from app.core.telemetry import get_telemetry, reset_telemetry
    reset_telemetry()
    get_telemetry().openai.record_tokens(total, 0)


def test_token_usage_recorded_and_tpm_rpm(monkeypatch):
    from app.core import capacity
    from app.core.telemetry import get_telemetry, reset_telemetry

    reset_telemetry()
    t = get_telemetry()
    t.openai.record_tokens(1000, 500)      # 1500 total tokens
    t.openai.record(latency_ms=100, ok=True)  # 1 request
    t.openai.record(latency_ms=100, ok=True)  # 2 requests
    c = capacity.openai_capacity()
    assert c["tpm_used"] == 1500
    assert c["rpm_used"] == 2
    assert c["headroom_tokens"] == c["tpm_limit"] - 1500


def test_token_recording_single_path_no_double_count(monkeypatch):
    """The retry helper records REQUESTS but never TOKENS; the OpenAI client is
    the single authoritative token path. Driving the real non-stream path records
    the provider usage exactly once."""
    from app.core import provider_retry
    from app.core.telemetry import get_telemetry, reset_telemetry
    from app.patient_engine.openai_client import OpenAIPatientClient

    reset_telemetry()
    # 1) retry helper alone must NOT record tokens
    provider_retry.call_with_retry(lambda: types.SimpleNamespace(usage=types.SimpleNamespace(input_tokens=999, output_tokens=999)),
                                   provider=get_telemetry().openai, max_retries=0, base_ms=1, max_ms=1, sleep=lambda *_: None)
    assert get_telemetry().openai.window.sum("total_tokens", 60) == 0
    assert get_telemetry().openai.window.sum("requests", 60) == 1

    # 2) the real client path records tokens ONCE (from response.usage)
    reset_telemetry()
    client = OpenAIPatientClient()
    rt = types.SimpleNamespace(api_key="k", model="gpt-4o-mini", timeout_seconds=30,
                               max_output_tokens=400, patient_max_output_tokens=400)
    monkeypatch.setattr(OpenAIPatientClient, "_runtime", staticmethod(lambda: rt))
    reply_json = '{"patient_text":"ok","used_fact_ids":[],"response_type":"clinical_answer","supported":true,"speech":null}'
    fake_sdk = types.SimpleNamespace(responses=types.SimpleNamespace(
        create=lambda **kw: types.SimpleNamespace(output_text=reply_json,
                                                  usage=types.SimpleNamespace(input_tokens=120, output_tokens=30))))
    monkeypatch.setattr(client, "_get_client", lambda rt=None: fake_sdk)
    client.generate([{"role": "user", "content": "hi"}])
    assert get_telemetry().openai.window.sum("total_tokens", 60) == 150   # 120+30, once
    assert get_telemetry().openai.window.sum("requests", 60) == 1


def test_capacity_state_thresholds(monkeypatch):
    from app.core import capacity
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "openai_tpm_limit", 1000)
    monkeypatch.setattr(s, "openai_rpm_limit", 100000)  # keep RPM out of the way

    _set_tokens(500)   # 50%
    assert capacity.capacity_state() == "NORMAL"
    _set_tokens(720)   # 72%
    assert capacity.capacity_state() == "BUSY"
    _set_tokens(880)   # 88%
    assert capacity.capacity_state() == "PROTECTING"
    _set_tokens(970)   # 97%
    assert capacity.capacity_state() == "CRITICAL"


def test_old_token_buckets_expire():
    import time
    from collections import defaultdict
    from app.core.telemetry import get_telemetry, reset_telemetry

    reset_telemetry()
    w = get_telemetry().openai.window
    old_idx = int(time.monotonic() // w.bucket_seconds) - 100
    w._buckets[old_idx] = {"c": defaultdict(int, {"total_tokens": 99999}), "lat": []}
    w.incr("total_tokens", 10)  # writing prunes old buckets
    assert w.sum("total_tokens", 60) == 10


# ==========================================================================
#  INTERVIEW QUEUE METRICS
# ==========================================================================
def test_queue_wait_metrics_and_timeout(monkeypatch):
    from app.core import concurrency
    from app.core.config import get_settings
    from app.core.exceptions import ServiceOverloadedError

    s = get_settings()
    monkeypatch.setattr(s, "max_concurrent_ai_interviews", 1)
    monkeypatch.setattr(s, "ai_interview_wait_seconds", 0.05)
    monkeypatch.setattr(concurrency, "_interview_sem", concurrency._ResizableSemaphore())

    with concurrency.interview_slot():
        pass
    stats = concurrency.interview_capacity()
    assert "waiting" in stats and stats["wait_p50_ms"] is not None  # wait recorded

    held = concurrency.interview_slot().__enter__()
    try:
        with pytest.raises(ServiceOverloadedError):
            concurrency.interview_slot().__enter__()
    finally:
        held.__exit__(None, None, None)
    assert concurrency.interview_capacity()["timeouts_5m"] >= 1


# ==========================================================================
#  ADAPTIVE ASSESSMENT THROTTLING
# ==========================================================================
def test_effective_workers_by_state(monkeypatch):
    from app.core import capacity
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "assessment_worker_concurrency", 3)
    monkeypatch.setattr(s, "assessment_workers_busy", 2)
    monkeypatch.setattr(s, "assessment_workers_protecting", 1)
    monkeypatch.setattr(s, "assessment_pause_on_critical", True)

    assert capacity.effective_assessment_workers("NORMAL") == (3, "NORMAL")
    assert capacity.effective_assessment_workers("BUSY") == (2, "REDUCED")
    assert capacity.effective_assessment_workers("PROTECTING") == (1, "MINIMAL")
    assert capacity.effective_assessment_workers("CRITICAL") == (0, "PAUSED")


def test_worker_pauses_on_critical_and_resumes(engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from app.assessment.assessment_repository import AssessmentRepository
    from app.core.assessment_worker import get_assessment_worker
    from app.core.config import get_settings
    from app.core.telemetry import reset_telemetry, get_telemetry

    s = get_settings()
    monkeypatch.setattr(s, "openai_tpm_limit", 1000)
    monkeypatch.setattr(s, "assessment_pause_on_critical", True)
    reset_telemetry()

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        AssessmentRepository(db).create_run(session_id="sc", case_id="carly", status="PENDING")
        db.commit()
        worker = get_assessment_worker()

        # CRITICAL -> paused: no claim even though a PENDING job exists.
        get_telemetry().openai.record_tokens(970, 0)  # 97% of 1000
        assert worker._claim_if_capacity(db) is None

        # Recovery -> NORMAL: claiming resumes.
        reset_telemetry()  # clears token usage -> back to NORMAL
        claimed = worker._claim_if_capacity(db)
        assert claimed is not None and claimed.status == "PROCESSING"
    finally:
        db.close()


# ==========================================================================
#  OPERATOR INSIGHTS (deterministic)
# ==========================================================================
def _overview(engine, admin_email):
    from app.database.connection import get_db  # noqa
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        make_admin(engine, email=admin_email)
        ah = bearer(login_token(c, admin_email, "adminpass1"))
        return c.get("/api/admin/system/traffic/overview", headers=ah).json()


def test_operator_insights_states(engine, monkeypatch):
    from app.core.config import get_settings
    from app.core.telemetry import get_telemetry, reset_telemetry

    # healthy/idle
    reset_telemetry()
    ov = _overview(engine, "op1@school.edu")
    tones = {i["tone"] for i in ov["insights"]}
    assert "green" in tones

    # BUSY -> yellow insight
    s = get_settings()
    monkeypatch.setattr(s, "openai_tpm_limit", 1000)
    reset_telemetry()
    get_telemetry().openai.record_tokens(750, 0)  # 75%
    ov = _overview(engine, "op2@school.edu")
    assert any(i["tone"] == "yellow" for i in ov["insights"])

    # CRITICAL -> red insight + paused assessment
    reset_telemetry()
    get_telemetry().openai.record_tokens(980, 0)  # 98%
    ov = _overview(engine, "op3@school.edu")
    assert any(i["tone"] == "red" for i in ov["insights"])
    assert ov["assessment"]["throttle_mode"] == "PAUSED"

    # 429 detected -> red insight
    reset_telemetry()
    get_telemetry().openai.window.incr("rate_limited", 1)
    ov = _overview(engine, "op4@school.edu")
    assert any("429" in i["message"] for i in ov["insights"])


# ==========================================================================
#  ACCESS REQUEST FLOW
# ==========================================================================
def test_public_request_pending_and_dedup(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        r1 = c.post("/api/access/request", json={"email": "Ann@School.edu"})
        assert r1.status_code == 200 and r1.json()["result"] == "PENDING"
        r2 = c.post("/api/access/request", json={"email": "ann@school.edu"})  # normalized dup
        assert r2.json()["result"] == "ALREADY_PENDING"
        # exactly one row
        from sqlalchemy.orm import sessionmaker
        from app.models import AccessRequest
        db = sessionmaker(bind=engine)()
        try:
            assert db.query(AccessRequest).count() == 1
        finally:
            db.close()


def test_admin_approve_reject_and_audit_fields(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        c.post("/api/access/request", json={"email": "bob@school.edu"})
        make_admin(engine, email="adm@school.edu")
        ah = bearer(login_token(c, "adm@school.edu", "adminpass1"))
        lst = c.get("/api/admin/access-requests", headers=ah).json()
        rid = lst[0]["id"]
        appr = c.post(f"/api/admin/access-requests/{rid}/approve", json={"note": "ok"}, headers=ah)
        assert appr.status_code == 200
        body = appr.json()
        assert body["status"] == "APPROVED"
        assert body["reviewedBy"] == "adm@school.edu" and body["reviewedAt"] and body["reviewerNote"] == "ok"
        # approved email re-submitting does not create another pending
        assert c.post("/api/access/request", json={"email": "bob@school.edu"}).json()["result"] == "ALREADY_APPROVED"
        # reject a different request
        c.post("/api/access/request", json={"email": "carl@school.edu"})
        rid2 = [r for r in c.get("/api/admin/access-requests", headers=ah).json() if r["email"] == "carl@school.edu"][0]["id"]
        rej = c.post(f"/api/admin/access-requests/{rid2}/reject", json={}, headers=ah)
        assert rej.json()["status"] == "REJECTED"


def test_student_cannot_manage_access_requests(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        h = bearer(register_student(c, email="stud@school.edu")["accessToken"])
        assert c.get("/api/admin/access-requests", headers=h).status_code == 403
        assert c.get("/api/admin/access-requests").status_code == 401  # anon


def test_no_public_status_enumeration_endpoint(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        # There must be NO public GET status-by-email route.
        assert c.get("/api/access/status?email=someone@school.edu").status_code in (404, 405)


# NOTE: the Priority C "require_access_approval" REGISTRATION gate was superseded
# in Priority D by account approval (every registration creates a PENDING account
# that an admin approves). The access-request endpoints/table remain for
# historical data; the account-approval flow is covered in tests/test_priority_d.py.
