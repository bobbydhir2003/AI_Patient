"""Assessment pipeline tests using a fake OpenAI boundary (staged outputs).

The redesigned standard pipeline makes 2 OpenAI calls normally (combined
generation + independent verification) and at most 3 (one combined correction).
These tests queue the combined/review payloads and assert on both the produced
assessment AND the number of provider calls.
"""
import pytest

from app.core.constants import RUBRIC_DOMAINS
from tests.conftest import FakeOpenAIClient, make_client


def _run_interview(client, case_id="camden", questions=("Hi Camden, how are you?", "What do you like to play?")):
    session_id = client.post(
        "/api/sessions", json={"studentName": "Assess Tester", "studentId": "", "caseId": case_id}
    ).json()["sessionId"]
    for q in questions:
        r = client.post(f"/api/interviews/{session_id}/messages", json={"text": q, "caseId": case_id})
        assert r.status_code == 200
    client.post(f"/api/sessions/{session_id}/complete")
    return session_id


def _ev_item(evidence_id, turn_label, student_text, evidence_type="strength", **over):
    base = {
        "evidence_id": evidence_id, "turn_label": turn_label, "evidence_type": evidence_type,
        "label": "Warm, age-appropriate greeting", "severity": "",
        "student_excerpt": student_text, "patient_excerpt": "",
        "explanation": "The student opened with a friendly, child-appropriate greeting.",
        "why_it_matters": "", "suggested_alternative": "", "confidence_level": "strong",
    }
    base.update(over)
    return base


def _domain(name, level="Developing", items=None):
    return {
        "rubric_domain": name,
        "performance_level": level,
        "summary": f"{name} summary.",
        "narrative": "Narrative reasoning.",
        "strengths": ["Did something well."],
        "areas_for_growth": ["Could deepen follow-up."],
        "evidence_items": items or [],
    }


def _combined(student_text="Hi Camden, how are you?", turn_label="turn_00",
              overall="Developing", levels=None, extra_oars_items=None):
    """A full combined CALL-1 payload: all 4 domains + overall."""
    levels = levels or {}
    oars_items = [_ev_item("ev_oars_01", turn_label, student_text)]
    if extra_oars_items:
        oars_items.extend(extra_oars_items)
    hist_item = _ev_item(
        "ev_hist_01", turn_label, student_text, evidence_type="missed_opportunity",
        label="Functional impact not explored", severity="moderate",
        why_it_matters="Function is central to this case.",
        suggested_alternative="What games are hard for you to play now?",
        confidence_level="moderate",
    )
    return {
        "domains": [
            _domain("OARS Communication", levels.get("OARS Communication", "Developing"), oars_items),
            _domain("History Checklist", levels.get("History Checklist", "Developing"), [hist_item]),
            _domain("Red Flags / Safety Screening",
                    levels.get("Red Flags / Safety Screening", "Insufficient Evidence"), []),
            _domain("Empathy & Patient-Centeredness",
                    levels.get("Empathy & Patient-Centeredness", "Proficient"), []),
        ],
        "overall_level": overall,
        "overall_summary": "A respectful interview with room to deepen exploration.",
        "focus_areas": [
            {"title": "Deepen functional exploration", "why_it_matters": "Function drives this case.",
             "evidence_ids": ["ev_hist_01"], "suggested_practice": "Ask what daily activities changed."}
        ],
    }


def _review(approved=True, reject_domain=None):
    verdicts = []
    for d in RUBRIC_DOMAINS:
        is_approved = approved and (reject_domain != d)
        verdicts.append({
            "rubric_domain": d,
            "approved": is_approved,
            "issues": [] if is_approved else ["Level not supported by the cited evidence"],
            "rejected_evidence_ids": [],
        })
    return {
        "verdicts": verdicts,
        "overall_level": "Developing",
        "overall_summary": "A respectful interview with room to deepen exploration.",
        "focus_areas": [
            {"title": "Deepen functional exploration", "why_it_matters": "Function drives this case.",
             "evidence_ids": ["ev_hist_01"], "suggested_practice": "Ask what daily activities changed."}
        ],
    }


def _queue_happy_path(fake, student_text="Hi Camden, how are you?"):
    # Normal: exactly 2 calls (combined generate + verify).
    fake.queue_structured(_combined(student_text=student_text), _review())


@pytest.fixture()
def assess_env(engine):
    fake = FakeOpenAIClient(text="I get tired fast.", response_type="clinical_answer")
    with make_client(engine, fake) as api:
        yield api, fake


def test_full_assessment_flow_uses_two_calls(assess_env):
    api, fake = assess_env
    session_id = _run_interview(api)
    _queue_happy_path(fake)
    r = api.post(f"/api/sessions/{session_id}/assessment")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "COMPLETE"
    assert body["overallLevel"] == "Developing"
    assert {d["rubricDomain"] for d in body["domains"]} == set(RUBRIC_DOMAINS)
    # NORMAL assessment = exactly two OpenAI calls.
    assert fake.structured_calls == ["combined_assessment", "assessment_review"]
    # no numeric scores anywhere in the payload
    import json as _json
    text = _json.dumps(body).lower()
    for banned in ("score", "percent", "points"):
        assert banned not in text
    # evidence grounded in a real turn
    ev = body["domains"][0]["evidence"][0]
    transcript = api.get(f"/api/assessments/{body['assessmentId']}/transcript").json()
    turn_ids = {t["turnId"] for t in transcript}
    assert ev["turnId"] in turn_ids
    marked = [t for t in transcript if t["markers"]]
    assert marked, "transcript must carry assessment markers"
    # latest-for-session endpoint
    latest = api.get(f"/api/sessions/{session_id}/assessment")
    assert latest.status_code == 200
    assert latest.json()["assessmentId"] == body["assessmentId"]


def test_assessment_requires_completed_session(assess_env):
    api, fake = assess_env
    session_id = api.post(
        "/api/sessions", json={"studentName": "T", "studentId": "", "caseId": "carly"}
    ).json()["sessionId"]
    r = api.post(f"/api/sessions/{session_id}/assessment")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "session_not_completed"


def test_ai_failure_produces_no_fake_feedback(assess_env):
    api, fake = assess_env
    session_id = _run_interview(api, case_id="jayden",
                                questions=("Tell me about your running program.",))
    fake.fail = True
    r = api.post(f"/api/sessions/{session_id}/assessment")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "ASSESSMENT_UNAVAILABLE"
    latest = api.get(f"/api/sessions/{session_id}/assessment").json()
    assert latest["status"] == "FAILED"
    assert latest["domains"] == []          # no fabricated feedback
    assert latest["overallLevel"] is None


def test_fabricated_turns_and_cross_case_evidence_are_dropped(assess_env):
    api, fake = assess_env
    session_id = _run_interview(api, case_id="camden")
    bad_fabricated = _ev_item(
        "ev_bad_01", "turn_99", "never said", label="Fabricated", explanation="x",
    )
    bad_cross_case = _ev_item(
        "ev_bad_02", "turn_00", "Hi Camden, how are you?", label="Mentions Carly wrongly",
        explanation="Like Carly said before.",
    )
    combined = _combined(extra_oars_items=[bad_fabricated, bad_cross_case])
    fake.queue_structured(combined, _review())
    r = api.post(f"/api/sessions/{session_id}/assessment")
    assert r.status_code == 201
    body = r.json()
    labels = [e["label"] for d in body["domains"] for e in d["evidence"]]
    assert "Fabricated" not in labels
    assert "Mentions Carly wrongly" not in labels
    # the legitimate evidence survived
    assert any(e["label"] == "Warm, age-appropriate greeting" for d in body["domains"] for e in d["evidence"])


def test_short_interview_can_return_insufficient_evidence(assess_env):
    api, fake = assess_env
    session_id = _run_interview(api, case_id="sofia", questions=("Hi",))
    levels = {d: "Insufficient Evidence" for d in RUBRIC_DOMAINS}
    combined = {
        "domains": [_domain(d, "Insufficient Evidence", []) for d in RUBRIC_DOMAINS],
        "overall_level": "Insufficient Evidence",
        "overall_summary": "Too little conversation to assess.",
        "focus_areas": [],
    }
    review = _review()
    review["overall_level"] = "Insufficient Evidence"
    fake.queue_structured(combined, review)
    r = api.post(f"/api/sessions/{session_id}/assessment")
    assert r.status_code == 201
    body = r.json()
    assert body["overallLevel"] == "Insufficient Evidence"
    assert all(d["performanceLevel"] == "Insufficient Evidence" for d in body["domains"])


def test_correction_path_stays_within_three_calls(assess_env):
    api, fake = assess_env
    session_id = _run_interview(api, case_id="carly", questions=("How are your wrists feeling?",))
    st = "How are your wrists feeling?"
    fake.queue_structured(
        _combined(student_text=st, levels={"OARS Communication": "Advanced"}),   # CALL 1
        _review(reject_domain="OARS Communication"),                              # CALL 2: rejects OARS
        _combined(student_text=st, overall="Developing",
                  levels={"OARS Communication": "Developing"}),                   # CALL 3: correction
    )
    r = api.post(f"/api/sessions/{session_id}/assessment")
    assert r.status_code == 201
    body = r.json()
    # ABSOLUTE MAX = 3 calls: combined + verify + one combined correction.
    assert fake.structured_calls == ["combined_assessment", "assessment_review", "combined_assessment"]
    assert len(fake.structured_calls) <= 3
    oars = next(d for d in body["domains"] if d["rubricDomain"] == "OARS Communication")
    assert oars["performanceLevel"] == "Developing"
    # A corrected (not independently re-verified) assessment is flagged for review.
    assert body["status"] == "NEEDS_REVIEW"
    assert body["verificationStatus"] == "NEEDS_REVIEW"


def test_rubrics_endpoint_is_student_safe(assess_env):
    api, _ = assess_env
    r = api.get("/api/rubrics")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 4
    import json as _json
    text = _json.dumps(body).lower()
    # protected case-reference material must not leak through this endpoint
    for banned in ("assessment_area", "case_context", "importance"):
        assert banned not in text


def test_assessment_post_is_idempotent(assess_env):
    api, fake = assess_env
    session_id = _run_interview(api)
    _queue_happy_path(fake)
    first = api.post(f"/api/sessions/{session_id}/assessment")
    assert first.status_code == 201
    calls_after_first = len(fake.calls)
    # Second POST: no new AI calls, same assessment returned, no duplicates.
    second = api.post(f"/api/sessions/{session_id}/assessment")
    assert second.status_code == 200
    assert second.json()["assessmentId"] == first.json()["assessmentId"]
    assert len(fake.calls) == calls_after_first, "idempotent POST must not rerun the AI pipeline"


def test_unknown_session_returns_404(assess_env):
    api, _ = assess_env
    r = api.post("/api/sessions/doesnotexist/assessment")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "session_not_found"


def test_session_and_assessment_ids_never_mix(assess_env):
    api, fake = assess_env
    session_id = _run_interview(api)
    _queue_happy_path(fake)
    created = api.post(f"/api/sessions/{session_id}/assessment").json()
    # Session id on the assessment-id endpoint → 404 (not a silent wrong lookup)
    assert api.get(f"/api/assessments/{session_id}").status_code == 404
    # Assessment id on the session endpoint → 404
    assert api.get(f"/api/sessions/{created['assessmentId']}/assessment").status_code == 404
    # Correct pairing works
    assert api.get(f"/api/assessments/{created['assessmentId']}").status_code == 200


def test_route_paths_are_exact(assess_env):
    api, _ = assess_env
    for expected in (
        "/api/sessions/foo/assessment",
        "/api/assessments/bar",
        "/api/assessments/bar/transcript",
        "/api/rubrics",
    ):
        assert api.options(expected).status_code != 404, f"missing route {expected}"


# ---------------------------------------------------------------------------
# Four-domain hardening: schema requirement + missing-domain recovery retry.
# Regression for the production failure:
#   assessment_failed error=Expected all four rubric domains, got ['OARS Communication']
# ---------------------------------------------------------------------------
def _one_domain_combined(student_text="Hi Camden, how are you?", turn_label="turn_00"):
    """A structurally-parseable combined payload that (wrongly) carries only ONE
    rubric domain - exactly the shape that broke production."""
    return {
        "domains": [
            _domain("OARS Communication", "Developing",
                    [_ev_item("ev_oars_01", turn_label, student_text)]),
        ],
        "overall_level": "Developing",
        "overall_summary": "Only one domain returned (defective).",
        "focus_areas": [],
    }


def test_schema_requires_all_four_domains():
    """The combined structured-output contract must force exactly four domain
    objects, each pinned to the canonical rubric-domain enum."""
    from app.schemas.assessment_schema import COMBINED_ASSESSMENT_JSON_SCHEMA

    domains = COMBINED_ASSESSMENT_JSON_SCHEMA["properties"]["domains"]
    assert domains["minItems"] == len(RUBRIC_DOMAINS) == 4
    assert domains["maxItems"] == len(RUBRIC_DOMAINS)
    assert set(domains["items"]["properties"]["rubric_domain"]["enum"]) == set(RUBRIC_DOMAINS)


def test_one_domain_response_triggers_recovery_retry(assess_env):
    """A one-domain CALL-1 result must trigger ONE targeted regeneration BEFORE
    validation; the recovered four-domain result then completes normally."""
    api, fake = assess_env
    session_id = _run_interview(api)
    # CALL 1 = defective (one domain) -> recovery CALL 2 = full four domains
    # -> verify CALL 3. Still within the 3-call budget.
    fake.queue_structured(_one_domain_combined(), _combined(), _review())
    r = api.post(f"/api/sessions/{session_id}/assessment")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "COMPLETE"
    assert {d["rubricDomain"] for d in body["domains"]} == set(RUBRIC_DOMAINS)
    # gen -> recovery(gen) -> verify. The recovery reused the combined schema.
    assert fake.structured_calls == [
        "combined_assessment", "combined_assessment", "assessment_review",
    ]


def test_recovery_that_still_misses_a_domain_fails_cleanly(assess_env):
    """If even the ONE recovery retry comes back incomplete, the run must FAIL
    via the validator - bounded, never an infinite retry loop."""
    api, fake = assess_env
    session_id = _run_interview(api)
    # Both the initial and the single recovery attempt return one domain.
    fake.queue_structured(_one_domain_combined(), _one_domain_combined())
    r = api.post(f"/api/sessions/{session_id}/assessment")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "ASSESSMENT_UNAVAILABLE"
    latest = api.get(f"/api/sessions/{session_id}/assessment").json()
    assert latest["status"] == "FAILED"
    assert latest["domains"] == []  # no fabricated feedback
    # Exactly two generation attempts - the recovery is a SINGLE bounded retry,
    # and no verify ran because validation failed first. No loop.
    assert fake.structured_calls == ["combined_assessment", "combined_assessment"]


def test_retry_button_after_failure_starts_a_fresh_run(assess_env):
    """'Generate Assessment Again' (retry=true) must create a NEW run for a
    previously-FAILED session, not return the cached FAILED payload."""
    api, fake = assess_env
    session_id = _run_interview(api)
    # First attempt fails outright.
    fake.fail = True
    r1 = api.post(f"/api/sessions/{session_id}/assessment")
    assert r1.status_code == 503
    assert api.get(f"/api/sessions/{session_id}/assessment").json()["status"] == "FAILED"

    # Retry with a healthy provider now succeeds on a fresh run.
    fake.fail = False
    _queue_happy_path(fake)
    r2 = api.post(f"/api/sessions/{session_id}/assessment?retry=true")
    assert r2.status_code == 201
    body = r2.json()
    assert body["status"] == "COMPLETE"
    assert {d["rubricDomain"] for d in body["domains"]} == set(RUBRIC_DOMAINS)
