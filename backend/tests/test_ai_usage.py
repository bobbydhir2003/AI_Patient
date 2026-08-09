"""AI Usage & Cost telemetry tests.

Covers: real OpenAI usage recording on an interview turn, totals = input + output,
session aggregation, ElevenLabs recording, cost calculation, historical pricing
preservation, average cost, time filters, admin authorization, no double-counting
on replay, and separation of concurrent sessions.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker

from app.core import pricing
from app.models import AiUsageEvent
from app.services import usage_recorder, usage_service
from tests.conftest import bearer, make_client, FakeOpenAIClient
from tests.test_auth import login_token, make_admin, register


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _start(client, case_id="camden"):
    r = client.post("/api/sessions", json={"studentName": "Usage Tester", "studentId": "", "caseId": case_id})
    return r.json()["sessionId"]


def _send(client, session_id, text, case_id="camden", client_turn_id=None):
    body = {"text": text, "caseId": case_id}
    if client_turn_id:
        body["clientTurnId"] = client_turn_id
    return client.post(f"/api/interviews/{session_id}/messages", json=body)


# --------------------------------------------------- 1,2,3: OpenAI recording
def test_openai_usage_recorded_with_totals(student_client, engine):
    sid = _start(student_client)
    assert _send(student_client, sid, "Hi, how are you?").status_code == 200

    db = _factory(engine)()
    try:
        events = db.query(AiUsageEvent).filter_by(session_id=sid, provider="openai").all()
        assert len(events) == 1, "exactly one OpenAI event per turn"
        e = events[0]
        # The FakeOpenAIClient reports 100 input / 40 output.
        assert e.input_tokens == 100 and e.output_tokens == 40
        assert e.total_tokens == e.input_tokens + e.output_tokens == 140  # totals = in + out
        assert e.estimated_cost_usd > 0
    finally:
        db.close()


def test_multiple_turns_accumulate(student_client, engine):
    sid = _start(student_client)
    for q in ("one", "two", "three"):
        assert _send(student_client, sid, f"question {q}?").status_code == 200
    db = _factory(engine)()
    try:
        events = db.query(AiUsageEvent).filter_by(session_id=sid, provider="openai").all()
        assert len(events) == 3
        assert sum(e.total_tokens for e in events) == 3 * 140
    finally:
        db.close()


# --------------------------------------------------- 4: session aggregation
def test_session_aggregation(student_client, engine):
    sid = _start(student_client)
    _send(student_client, sid, "a?")
    _send(student_client, sid, "b?")
    db = _factory(engine)()
    try:
        agg = usage_service.session_detail(db, sid)
        assert agg is not None
        assert agg["input_tokens"] == 200 and agg["output_tokens"] == 80
        assert agg["total_tokens"] == 280
        assert agg["openai_requests"] == 2
        assert agg["total_cost_usd"] > 0
    finally:
        db.close()


# --------------------------------------------------- 5,6,7: EL + cost + pricing
def test_elevenlabs_recording_and_cost(engine):
    db = _factory(engine)()
    try:
        usage_recorder.record_elevenlabs_usage(
            db, session_id="s_el", student_id="stud", case_id="camden",
            characters=1000, voice_id="v1", model_id="eleven_turbo_v2_5",
        )
        e = db.query(AiUsageEvent).filter_by(session_id="s_el", provider="elevenlabs").one()
        assert e.characters_generated == 1000
        expected, per_char = pricing.estimate_elevenlabs_cost(1000)
        assert abs(e.estimated_cost_usd - expected) < 1e-9
        # Historical pricing preserved on the row.
        assert abs(e.provider_unit_price - per_char) < 1e-12
        assert e.pricing_version == pricing.PRICING_VERSION
    finally:
        db.close()


def test_openai_cost_matches_pricing_and_preserves_rates(engine):
    db = _factory(engine)()
    try:
        usage_recorder.record_openai_usage(
            db, "s_oi", "stud", "camden",
            {"input_tokens": 1000, "output_tokens": 500, "model": "gpt-4o-mini"},
        )
        e = db.query(AiUsageEvent).filter_by(session_id="s_oi", provider="openai").one()
        expected, rates = pricing.estimate_openai_cost(1000, 500, 0, "gpt-4o-mini")
        assert abs(e.estimated_cost_usd - expected) < 1e-9
        assert abs(e.input_unit_price - rates.input_per_token) < 1e-12
        assert abs(e.output_unit_price - rates.output_per_token) < 1e-12
        assert e.pricing_version == pricing.PRICING_VERSION
    finally:
        db.close()


def test_missing_usage_records_nothing(engine):
    db = _factory(engine)()
    try:
        usage_recorder.record_openai_usage(db, "s_none", "stud", "c", None)
        usage_recorder.record_openai_usage(db, "s_none", "stud", "c", {"input_tokens": 0, "output_tokens": 0})
        assert db.query(AiUsageEvent).filter_by(session_id="s_none").count() == 0
    finally:
        db.close()


# --------------------------------------------------- 8: average cost + filters
def test_summary_average_cost_and_totals(student_client, engine):
    s1 = _start(student_client)
    _send(student_client, s1, "q?")
    s2 = _start(student_client)
    _send(student_client, s2, "q?")
    db = _factory(engine)()
    try:
        summ = usage_service.summary(db, "today")
        assert summ["input_tokens"] == 200 and summ["output_tokens"] == 80
        assert summ["total_tokens"] == 280
        assert summ["session_count"] == 2
        # avg cost = total cost / distinct sessions
        assert abs(summ["avg_cost_per_interview_usd"] - round(summ["total_cost_usd"] / 2, 4)) < 1e-6
        assert summ["provider_split"]["openai_pct"] >= 0
    finally:
        db.close()


def test_time_filter_excludes_old_events(engine):
    db = _factory(engine)()
    try:
        old = AiUsageEvent(
            session_id="s_old", provider="openai", model="gpt-4o-mini",
            input_tokens=10, output_tokens=5, total_tokens=15, estimated_cost_usd=0.001,
            created_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        db.add(old)
        db.commit()
        # 24h window must exclude a 10-day-old event; 30d must include it.
        assert usage_service.summary(db, "24h")["total_tokens"] == 0
        assert usage_service.summary(db, "30d")["total_tokens"] == 15
    finally:
        db.close()


# --------------------------------------------------- 9: admin authorization
def test_usage_endpoints_require_admin(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        make_admin(engine, email="admin_usage@school.edu")
        ah = bearer(login_token(c, "admin_usage@school.edu", "adminpass1"))
        assert c.get("/api/admin/usage/summary?range=today", headers=ah).status_code == 200
        assert c.get("/api/admin/usage/timeseries?range=24h", headers=ah).status_code == 200
        assert c.get("/api/admin/usage/sessions?range=today", headers=ah).status_code == 200

        register(c, email="stud_usage@school.edu", password="studpass1", number="U1")
        sh = bearer(login_token(c, "stud_usage@school.edu", "studpass1"))
        assert c.get("/api/admin/usage/summary", headers=sh).status_code == 403
        assert c.get("/api/admin/usage/summary").status_code == 401  # anon


# --------------------------------------------------- 11: no double counting
def test_replayed_turn_not_double_counted(student_client, engine):
    sid = _start(student_client)
    cti = "fixed-client-turn-id"
    r1 = _send(student_client, sid, "hello?", client_turn_id=cti)
    r2 = _send(student_client, sid, "hello?", client_turn_id=cti)  # idempotent replay
    assert r1.status_code == 200 and r2.status_code == 200
    db = _factory(engine)()
    try:
        events = db.query(AiUsageEvent).filter_by(session_id=sid, provider="openai").count()
        assert events == 1, "a replayed (idempotent) turn must be counted once"
    finally:
        db.close()


# --------------------------------------------------- 12: concurrent sessions
def test_sessions_kept_separate(student_client, engine):
    s1 = _start(student_client)
    s2 = _start(student_client)
    _send(student_client, s1, "q1?")
    _send(student_client, s2, "q2?")
    _send(student_client, s2, "q3?")
    db = _factory(engine)()
    try:
        assert usage_service.session_detail(db, s1)["total_tokens"] == 140
        assert usage_service.session_detail(db, s2)["total_tokens"] == 280
        listing = usage_service.sessions(db, "today", limit=10)
        assert listing["total"] == 2
        ids = {row["session_id"] for row in listing["sessions"]}
        assert ids == {s1, s2}
    finally:
        db.close()
