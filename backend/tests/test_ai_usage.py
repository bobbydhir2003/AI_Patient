"""AI Usage & Cost telemetry tests.

Covers OpenAI usage recording, totals = input + output, session aggregation,
cost calculation, historical pricing preservation, average cost, time filters,
admin authorization, and separation of concurrent sessions.

The live interview usage producer is now the LiveKit + OpenAI Realtime worker
(and the assessment pipeline); the old typed HTTP /messages producer was
removed. These tests therefore seed usage events directly through
`usage_recorder.record_openai_usage` - exactly the call those producers make -
and then assert the aggregation/reporting the dashboard performs. ElevenLabs
usage recording was removed with the ElevenLabs TTS path.
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


def _record_turn(engine, session_id, *, case_id="camden", n=1):
    """Record N OpenAI usage events for a session, mirroring what a producer
    records per turn (100 input / 40 output, matching the legacy fake)."""
    db = _factory(engine)()
    try:
        for _ in range(n):
            usage_recorder.record_openai_usage(
                db, session_id, "stud", case_id,
                {"input_tokens": 100, "output_tokens": 40, "model": "gpt-4o-mini"},
            )
    finally:
        db.close()


# --------------------------------------------------- 1,2,3: OpenAI recording
def test_openai_usage_recorded_with_totals(engine):
    _record_turn(engine, "s1")
    db = _factory(engine)()
    try:
        events = db.query(AiUsageEvent).filter_by(session_id="s1", provider="openai").all()
        assert len(events) == 1, "exactly one OpenAI event per turn"
        e = events[0]
        assert e.input_tokens == 100 and e.output_tokens == 40
        assert e.total_tokens == e.input_tokens + e.output_tokens == 140  # totals = in + out
        assert e.estimated_cost_usd > 0
    finally:
        db.close()


def test_multiple_turns_accumulate(engine):
    _record_turn(engine, "s2", n=3)
    db = _factory(engine)()
    try:
        events = db.query(AiUsageEvent).filter_by(session_id="s2", provider="openai").all()
        assert len(events) == 3
        assert sum(e.total_tokens for e in events) == 3 * 140
    finally:
        db.close()


# --------------------------------------------------- 4: session aggregation
def test_session_aggregation(engine):
    _record_turn(engine, "s3", n=2)
    db = _factory(engine)()
    try:
        agg = usage_service.session_detail(db, "s3")
        assert agg is not None
        assert agg["input_tokens"] == 200 and agg["output_tokens"] == 80
        assert agg["total_tokens"] == 280
        assert agg["openai_requests"] == 2
        assert agg["total_cost_usd"] > 0
    finally:
        db.close()


# --------------------------------------------------- 5,6: cost + pricing
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
def test_summary_average_cost_and_totals(engine):
    _record_turn(engine, "s_a")
    _record_turn(engine, "s_b")
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


# --------------------------------------------------- 12: concurrent sessions
def test_sessions_kept_separate(engine):
    _record_turn(engine, "sep1", n=1)
    _record_turn(engine, "sep2", n=2)
    db = _factory(engine)()
    try:
        assert usage_service.session_detail(db, "sep1")["total_tokens"] == 140
        assert usage_service.session_detail(db, "sep2")["total_tokens"] == 280
        listing = usage_service.sessions(db, "today", limit=10)
        assert listing["total"] == 2
        ids = {row["session_id"] for row in listing["sessions"]}
        assert ids == {"sep1", "sep2"}
    finally:
        db.close()
