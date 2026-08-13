"""Hard guarantees on assessment OpenAI call count + usage accounting.

These lock in the redesign: a standard assessment makes 2 calls normally, 3 at
the absolute maximum, and can NEVER regress to the old 6-11 per-domain fan-out.
Every billed assessment call is recorded in ai_usage_events with a stage purpose.
"""
import pytest
from sqlalchemy.orm import sessionmaker

from app.core.constants import RUBRIC_DOMAINS
from app.models import AiUsageEvent
from tests.conftest import FakeOpenAIClient, make_client
from tests.test_assessment import _combined, _review, _run_interview


@pytest.fixture()
def assess_env(engine):
    fake = FakeOpenAIClient(text="I get tired fast.")
    with make_client(engine, fake) as api:
        yield api, fake, engine


def _usage_rows(engine):
    db = sessionmaker(bind=engine)()
    try:
        return list(db.query(AiUsageEvent).filter(AiUsageEvent.purpose.isnot(None)).all())
    finally:
        db.close()


def test_normal_assessment_makes_exactly_two_calls(assess_env):
    api, fake, _ = assess_env
    session_id = _run_interview(api)
    fake.queue_structured(_combined(), _review())
    api.post(f"/api/sessions/{session_id}/assessment")
    assert fake.structured_calls == ["combined_assessment", "assessment_review"]


def test_correction_never_exceeds_three_calls(assess_env):
    api, fake, _ = assess_env
    session_id = _run_interview(api)
    # Even if the reviewer rejects EVERY domain, there is exactly ONE combined
    # correction - never a per-domain regeneration loop.
    fake.queue_structured(
        _combined(),
        {**_review(), "verdicts": [
            {"rubric_domain": d, "approved": False, "issues": ["x"], "rejected_evidence_ids": []}
            for d in RUBRIC_DOMAINS
        ]},
        _combined(),
    )
    api.post(f"/api/sessions/{session_id}/assessment")
    assert len(fake.structured_calls) == 3
    assert fake.structured_calls.count("combined_assessment") == 2  # never 4 per-domain


def test_assessment_can_never_reach_six_to_eleven_calls(assess_env):
    """The old architecture produced 6-11 calls. Queue far more responses than
    the budget allows and prove the pipeline stops at the hard ceiling of 3."""
    api, fake, _ = assess_env
    session_id = _run_interview(api)
    # 12 payloads available; a runaway would consume many. The budget must cap it.
    fake.queue_structured(
        _combined(),
        {**_review(), "verdicts": [
            {"rubric_domain": d, "approved": False, "issues": ["x"], "rejected_evidence_ids": []}
            for d in RUBRIC_DOMAINS
        ]},
        *[_combined() for _ in range(10)],
    )
    api.post(f"/api/sessions/{session_id}/assessment")
    assert len(fake.structured_calls) <= 3
    assert not (6 <= len(fake.structured_calls) <= 11)


def test_both_calls_record_usage_events_with_stages(assess_env):
    api, fake, engine = assess_env
    session_id = _run_interview(api)
    fake.queue_structured(_combined(), _review())
    api.post(f"/api/sessions/{session_id}/assessment")

    rows = _usage_rows(engine)
    purposes = sorted(r.purpose for r in rows)
    assert purposes == ["assessment_generate", "assessment_verify"]
    # Provider-reported token totals are recorded (fake returns 500 in / 200 out).
    assert all(r.provider == "openai" and r.model == "gpt-4o-mini" for r in rows)
    assert all(r.input_tokens == 500 and r.output_tokens == 200 for r in rows)
    assert all(r.estimated_cost_usd > 0 for r in rows)
    # session attribution is present
    assert all(r.session_id == session_id for r in rows)


def test_correction_records_three_usage_events(assess_env):
    api, fake, engine = assess_env
    session_id = _run_interview(api)
    fake.queue_structured(
        _combined(),
        _review(reject_domain="OARS Communication"),
        _combined(overall="Developing"),
    )
    api.post(f"/api/sessions/{session_id}/assessment")
    rows = _usage_rows(engine)
    purposes = sorted(r.purpose for r in rows)
    assert purposes == ["assessment_correction", "assessment_generate", "assessment_verify"]


def test_budget_exceeded_marks_needs_review_not_runaway(assess_env, monkeypatch):
    """If the hard ceiling is 1, verification can't run; the assessment is flagged
    NEEDS_REVIEW instead of making more provider calls or failing."""
    from app.core.config import get_settings

    api, fake, _ = assess_env
    settings = get_settings()
    monkeypatch.setattr(settings, "assessment_max_openai_calls", 1, raising=False)

    session_id = _run_interview(api)
    fake.queue_structured(_combined(), _review())  # verify is queued but must NOT run
    r = api.post(f"/api/sessions/{session_id}/assessment")
    assert r.status_code == 201
    body = r.json()
    assert fake.structured_calls == ["combined_assessment"]  # verify blocked by budget
    assert body["status"] == "NEEDS_REVIEW"
    # domains + overall still present (from the validated CALL-1 output)
    assert {d["rubricDomain"] for d in body["domains"]} == set(RUBRIC_DOMAINS)
