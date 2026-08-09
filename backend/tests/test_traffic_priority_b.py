"""Priority B tests: telemetry, dashboard security, concurrency guards, TTS
degradation, assessment queue, provider retry/backoff, and bounded context."""
import threading
import time
import types

import pytest

from tests.conftest import FakeOpenAIClient, bearer, make_client, register_student
from tests.test_auth import login_token, make_admin


# ==========================================================================
#  TELEMETRY
# ==========================================================================
def test_http_requests_and_429_and_latency_counted():
    from app.core.telemetry import reset_telemetry, get_telemetry

    reset_telemetry()
    t = get_telemetry()
    t.record_http(200, 100.0)
    t.record_http(200, 300.0)
    t.record_http(429, 50.0)
    t.record_http(500, 20.0)
    assert t.http.sum("requests", 60) == 4
    assert t.http.sum("429", 60) == 1
    assert t.http.sum("5xx", 60) == 1
    assert t.http.sum("2xx", 60) == 2
    assert t.http.percentile(60, 50) is not None


def test_old_metric_buckets_expire_and_samples_bounded():
    from app.core.telemetry import RollingWindow

    w = RollingWindow(bucket_seconds=1, window_seconds=2, max_samples=10)
    # Bucket far in the past is pruned once a new bucket is written.
    now = time.monotonic()
    old_idx = int(now // 1) - 100
    w._buckets[old_idx] = {"c": __import__("collections").defaultdict(int, {"requests": 5}), "lat": []}
    w.incr("requests")
    assert old_idx not in w._buckets  # pruned
    # Latency samples are capped (reservoir), never unbounded.
    for i in range(1000):
        w.observe_latency(float(i))
    total = sum(len(b["lat"]) for b in w._buckets.values())
    assert total <= 10


def test_active_users_expire_after_inactivity():
    from app.core.telemetry import reset_telemetry, get_telemetry

    reset_telemetry()
    t = get_telemetry()
    t.live.touch_user("u1")
    t.live.touch_user("u2")
    assert t.live.active_users(120) == 2
    # Simulate u1 going stale by rewinding its timestamp.
    t.live._users["u1"] = time.monotonic() - 999
    assert t.live.active_users(120) == 1


def test_live_session_status_updates():
    from app.core.telemetry import reset_telemetry, get_telemetry

    reset_telemetry()
    t = get_telemetry()
    t.live.start_session("s1", "Ann", "A1", "carly", "Carly")
    t.live.set_status("s1", "WAITING_FOR_AI", latency_ms=842)
    sessions = t.live.list_sessions(900)
    assert sessions and sessions[0].status == "WAITING_FOR_AI"
    assert sessions[0].latest_latency_ms == 842


# ==========================================================================
#  DASHBOARD SECURITY
# ==========================================================================
def _endpoints():
    return [
        "/api/admin/system/traffic/overview",
        "/api/admin/system/traffic/live-sessions",
        "/api/admin/system/traffic/history",
        "/api/admin/system/traffic/providers",
        "/api/admin/system/traffic/capacity",
        "/api/admin/system/traffic/alerts",
    ]


def test_traffic_dashboard_requires_admin(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        # anonymous -> 401
        for ep in _endpoints():
            assert c.get(ep).status_code == 401, ep
        # student -> 403
        stud = register_student(c, email="s@school.edu")
        h = bearer(stud["accessToken"])
        for ep in _endpoints():
            assert c.get(ep, headers=h).status_code == 403, ep
        # admin -> 200
        make_admin(engine, email="admin@school.edu")
        atoken = login_token(c, "admin@school.edu", "adminpass1")
        ah = bearer(atoken)
        for ep in _endpoints():
            assert c.get(ep, headers=ah).status_code == 200, ep


def test_overview_has_no_fabricated_infrastructure(engine):
    """No fake nodes / Redis / autoscaling numbers - only real, measurable data."""
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        make_admin(engine, email="a2@school.edu")
        ah = bearer(login_token(c, "a2@school.edu", "adminpass1"))
        ov = c.get("/api/admin/system/traffic/overview", headers=ah).json()
        blob = str(ov).lower()
        for forbidden in ("ip-10-", "redis", "autoscal", "in-service", "standby", "worker node"):
            assert forbidden not in blob
        cap = c.get("/api/admin/system/traffic/capacity", headers=ah).json()
        assert cap["deployment_mode"] == "single_instance"
        assert "not queried" in cap["notes"]["autoscaling"].lower()
        # SQLite (test DB) is reported honestly as not-applicable, not faked.
        assert ov["server"]["db_pool"]["applicable"] is False


def test_history_time_range(engine):
    from app.core.telemetry import get_telemetry

    t = get_telemetry()
    for _ in range(3):
        t.history.record({"_ts": time.time(), "t": "now", "active_users": 1, "http_rpm": 5,
                          "openai_rpm": 0, "elevenlabs_rpm": 0, "rate_limited": 0})
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        make_admin(engine, email="a3@school.edu")
        ah = bearer(login_token(c, "a3@school.edu", "adminpass1"))
        r = c.get("/api/admin/system/traffic/history?minutes=15", headers=ah).json()
        assert r["minutes"] == 15
        assert len(r["points"]) >= 3


# ==========================================================================
#  INTERVIEW CONCURRENCY GUARD
# ==========================================================================
def test_interview_slot_cap_and_release_on_success_and_exception(monkeypatch):
    from app.core import concurrency
    from app.core.config import get_settings
    from app.core.exceptions import ServiceOverloadedError
    from app.core.telemetry import reset_telemetry, get_telemetry

    reset_telemetry()
    s = get_settings()
    monkeypatch.setattr(s, "max_concurrent_ai_interviews", 1)
    monkeypatch.setattr(s, "ai_interview_wait_seconds", 0.05)
    # fresh semaphore so the test is independent of prior state (no REDIS_URL
    # in tests, so this uses the local per-process fallback)
    monkeypatch.setattr(concurrency, "_interview_sem", concurrency.DistributedSemaphore("test_interview_cap"))

    held = concurrency.interview_slot().__enter__()
    assert get_telemetry().interview_in_flight.value == 1
    # cap enforced: a second acquire cannot get in within the bounded wait
    with pytest.raises(ServiceOverloadedError):
        concurrency.interview_slot().__enter__()
    held.__exit__(None, None, None)
    assert get_telemetry().interview_in_flight.value == 0

    # slot released after an exception inside the with-block
    try:
        with concurrency.interview_slot():
            raise ValueError("boom")
    except ValueError:
        pass
    assert get_telemetry().interview_in_flight.value == 0
    # and is immediately reusable
    with concurrency.interview_slot():
        pass


def test_interview_overload_returns_503(engine, monkeypatch):
    from app.core import concurrency
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "max_concurrent_ai_interviews", 1)
    monkeypatch.setattr(s, "ai_interview_wait_seconds", 0.05)
    monkeypatch.setattr(concurrency, "_interview_sem", concurrency.DistributedSemaphore("test_interview_overload"))

    with make_client(engine, FakeOpenAIClient(text="ok"), authenticate=False) as c:
        h = bearer(register_student(c, email="ov@school.edu")["accessToken"])
        sid = c.post("/api/sessions", json={"studentName": "O", "caseId": "camden"}, headers=h).json()["sessionId"]
        # Hold the only slot, then a real interview request must get a clean 503.
        held = concurrency.interview_slot().__enter__()
        try:
            r = c.post(f"/api/interviews/{sid}/messages", json={"text": "hi", "caseId": "camden"}, headers=h)
            assert r.status_code == 503
            assert r.json()["error"]["code"] == "service_overloaded"
        finally:
            held.__exit__(None, None, None)


# ==========================================================================
#  TTS CONCURRENCY / DEGRADATION
# ==========================================================================
def test_tts_slot_degrades_when_full(monkeypatch):
    from app.core import concurrency
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "max_concurrent_tts_requests", 1)
    monkeypatch.setattr(s, "tts_wait_seconds", 0.05)
    monkeypatch.setattr(concurrency, "_tts_sem", concurrency.DistributedSemaphore("test_tts_cap"))

    a = concurrency.tts_slot().acquire()
    assert a.ok is True
    b = concurrency.tts_slot().acquire()
    assert b.ok is False  # degrade: caller keeps text, skips audio
    a.release()
    c = concurrency.tts_slot().acquire()
    assert c.ok is True
    c.release()


# ==========================================================================
#  ASSESSMENT QUEUE
# ==========================================================================
def _completed_session(client, headers, case_id="carly"):
    sid = client.post("/api/sessions", json={"studentName": "Q", "caseId": case_id}, headers=headers).json()["sessionId"]
    client.post(f"/api/interviews/{sid}/messages",
                json={"text": "How are you?", "caseId": case_id, "clientTurnId": "q1"}, headers=headers)
    client.post(f"/api/sessions/{sid}/complete", headers=headers)
    return sid


def test_assessment_enqueues_and_dedups(engine, monkeypatch):
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "assessment_queue_enabled", True)
    with make_client(engine, FakeOpenAIClient(text="ok"), authenticate=False) as c:
        h = bearer(register_student(c, email="q1@school.edu")["accessToken"])
        sid = _completed_session(c, h)
        r1 = c.post(f"/api/sessions/{sid}/assessment", headers=h)
        assert r1.status_code == 202
        assert r1.json()["status"] in ("pending", "processing")
        # duplicate submit must NOT create a second run
        r2 = c.post(f"/api/sessions/{sid}/assessment", headers=h)
        assert r2.status_code == 202
        # exactly one run exists for the session
        from sqlalchemy.orm import sessionmaker
        from app.models import AssessmentRun
        db = sessionmaker(bind=engine)()
        try:
            runs = db.query(AssessmentRun).filter(AssessmentRun.session_id == sid).all()
            assert len(runs) == 1 and runs[0].status == "PENDING"
        finally:
            db.close()


def test_duplicate_active_run_blocked_by_unique_index(engine, monkeypatch):
    """The partial unique index makes a concurrent second active run impossible."""
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import sessionmaker

    from app.assessment.assessment_repository import AssessmentRepository

    db = sessionmaker(bind=engine)()
    try:
        AssessmentRepository(db).create_run(session_id="sX", case_id="carly", status="PENDING")
        db.commit()
        with pytest.raises(IntegrityError):
            AssessmentRepository(db).create_run(session_id="sX", case_id="carly", status="PROCESSING")
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_worker_claims_atomically_and_completes_in_mock(engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from app.assessment.assessment_repository import AssessmentRepository
    from app.core.assessment_worker import get_assessment_worker
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "mock_ai", True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        run = AssessmentRepository(db).create_run(session_id="sw", case_id="carly", status="PENDING")
        db.commit()
        run_id = run.id
        worker = get_assessment_worker()
        # First claim wins; a second claim finds nothing PENDING.
        claimed = worker._claim_one(db)
        assert claimed is not None and claimed.id == run_id
        assert worker._claim_one(db) is None
        worker._execute(db, claimed)
        db.expire_all()
        from app.models import AssessmentRun
        assert db.get(AssessmentRun, run_id).status == "COMPLETE"
    finally:
        db.close()


def test_failed_assessment_marks_failed(engine):
    """A generation failure marks the run FAILED (no fabricated feedback)."""
    from app.assessment import standard_assessment_service as sas
    from app.core.exceptions import AssessmentUnavailableError
    from app.models import AssessmentRun, InterviewSession, Student
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        st = Student(name="F", student_number="F1"); db.add(st); db.flush()
        sess = InterviewSession(student_id=st.id, case_id="carly", case_category="standard",
                                status="completed", locked=True)
        db.add(sess)
        # a student turn so _prepare passes the min-turn check
        from app.repositories.transcript_repository import TranscriptRepository
        db.flush()
        TranscriptRepository(db).append_turn(sess.id, "student", "hi", client_turn_id="c1", source="typed")
        run = AssessmentRun(session_id=sess.id, case_id="carly", status="PROCESSING")
        db.add(run); db.commit()
        with pytest.raises(AssessmentUnavailableError):
            sas.execute_existing(db, run, FakeOpenAIClient(fail=True))
        db.expire_all()
        assert db.get(AssessmentRun, run.id).status == "FAILED"
    finally:
        db.close()


# ==========================================================================
#  PROVIDER RETRY / BACKOFF
# ==========================================================================
def _exc(name="APIError", status=None, retry_after=None):
    e = type(name, (Exception,), {})()
    if status is not None:
        e.status_code = status
    if retry_after is not None:
        e.response = types.SimpleNamespace(headers={"retry-after": str(retry_after)})
    return e


def _pm():
    from app.core.telemetry import ProviderMetrics
    return ProviderMetrics("test")


def test_retry_on_429_and_500_then_success():
    from app.core import provider_retry

    for status in (429, 500, 503):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _exc("RateLimitError" if status == 429 else "InternalServerError", status=status)
            return "ok"

        res = provider_retry.call_with_retry(fn, provider=_pm(), max_retries=3, base_ms=1, max_ms=5, sleep=lambda *_: None)
        assert res == "ok"
        assert calls["n"] == 3


def test_no_retry_on_401_403_and_validation():
    from app.core import provider_retry

    for status in (401, 403, 400, 422):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise _exc("AuthenticationError", status=status)

        with pytest.raises(Exception):
            provider_retry.call_with_retry(fn, provider=_pm(), max_retries=3, base_ms=1, max_ms=5, sleep=lambda *_: None)
        assert calls["n"] == 1  # never retried


def test_max_retries_respected():
    from app.core import provider_retry

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _exc("InternalServerError", status=500)

    with pytest.raises(Exception):
        provider_retry.call_with_retry(fn, provider=_pm(), max_retries=3, base_ms=1, max_ms=5, sleep=lambda *_: None)
    assert calls["n"] == 4  # 1 initial + 3 retries


def test_retry_after_honored():
    from app.core import provider_retry

    d = provider_retry.classify(_exc("RateLimitError", status=429, retry_after=2))
    assert d.retry is True and d.rate_limited is True and d.retry_after == 2.0
    # backoff uses Retry-After when present (capped at max)
    assert provider_retry.backoff_seconds(0, base_ms=100, max_ms=5000, retry_after=2.0) == 2.0
    assert provider_retry.backoff_seconds(0, base_ms=100, max_ms=1000, retry_after=2.0) == 1.0


# ==========================================================================
#  BOUNDED CONTEXT (B8)
# ==========================================================================
def _turn(role, content):
    return types.SimpleNamespace(role=role, content=content)


def test_context_bounded_over_40_turns(monkeypatch):
    from app.core.config import get_settings
    from app.patient_engine import context_resolver

    s = get_settings()
    monkeypatch.setattr(s, "actor_max_recent_turns", 12)
    monkeypatch.setattr(s, "actor_context_char_limit", 0)  # isolate the turn cap

    turns = []
    for i in range(20):
        turns.append(_turn("student", f"question {i}"))
        turns.append(_turn("patient", f"answer {i}"))  # 40 turns total
    ctx = context_resolver.resolve_context("carly", [], turns, set())
    assert len(ctx.history) <= 12  # never grows with the interview
    assert ctx.turn_count == 40    # full count still reported
    # The most RECENT turns are the ones kept (continuity preserved).
    assert ctx.history[-1]["content"] == "answer 19"


def test_context_char_cap_trims_oldest(monkeypatch):
    from app.core.config import get_settings
    from app.patient_engine import context_resolver

    s = get_settings()
    monkeypatch.setattr(s, "actor_max_recent_turns", 50)
    monkeypatch.setattr(s, "actor_context_char_limit", 30)

    turns = [_turn("student", "x" * 20) for _ in range(10)]  # 200 chars total
    ctx = context_resolver.resolve_context("carly", [], turns, set())
    total = sum(len(h["content"]) for h in ctx.history)
    assert total <= 30
    assert len(ctx.history) >= 1
