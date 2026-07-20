"""Assessment pipeline tests using a fake OpenAI boundary (staged outputs)."""
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


def _extraction(turn_label="turn_00", student_text="Hi Camden, how are you?"):
    def domain(d, items):
        return {"rubric_domain": d, "evidence_items": items}

    item = {
        "evidence_id": "ev_oars_01",
        "turn_label": turn_label,
        "evidence_type": "strength",
        "label": "Warm, age-appropriate greeting",
        "severity": "",
        "student_excerpt": student_text,
        "patient_excerpt": "",
        "explanation": "The student opened with a friendly, child-appropriate greeting.",
        "why_it_matters": "",
        "suggested_alternative": "",
        "confidence_level": "strong",
    }
    missed = dict(item)
    missed.update(
        evidence_id="ev_hist_01",
        evidence_type="missed_opportunity",
        label="Functional impact not explored",
        severity="moderate",
        why_it_matters="Function is central to this case.",
        suggested_alternative="What games are hard for you to play now?",
        confidence_level="moderate",
    )
    return {
        "domains": [
            domain("OARS Communication", [item]),
            domain("History Checklist", [missed]),
            domain("Red Flags / Safety Screening", []),
            domain("Empathy & Patient-Centeredness", []),
        ]
    }


def _evaluation(domain, level="Developing", evidence_ids=None):
    return {
        "rubric_domain": domain,
        "performance_level": level,
        "summary": f"{domain} summary.",
        "narrative": "Narrative reasoning.",
        "strengths": ["Did something well."],
        "areas_for_growth": ["Could deepen follow-up."],
        "evidence_ids": evidence_ids or [],
    }


def _review(approved=True):
    return {
        "verdicts": [
            {"rubric_domain": d, "approved": approved, "issues": [] if approved else ["level unsupported"], "rejected_evidence_ids": []}
            for d in RUBRIC_DOMAINS
        ],
        "overall_level": "Developing",
        "overall_summary": "A respectful interview with room to deepen exploration.",
        "focus_areas": [
            {"title": "Deepen functional exploration", "why_it_matters": "Function drives this case.",
             "evidence_ids": ["ev_hist_01"], "suggested_practice": "Ask what daily activities changed."}
        ],
    }


def _queue_happy_path(fake):
    fake.queue_structured(
        _extraction(),
        _evaluation("OARS Communication", evidence_ids=["ev_oars_01"]),
        _evaluation("History Checklist", evidence_ids=["ev_hist_01"]),
        _evaluation("Red Flags / Safety Screening", "Insufficient Evidence"),
        _evaluation("Empathy & Patient-Centeredness", "Proficient"),
        _review(),
    )


@pytest.fixture()
def assess_env(engine):
    fake = FakeOpenAIClient(text="I get tired fast.", response_type="clinical_answer")
    with make_client(engine, fake) as api:
        yield api, fake


def test_full_assessment_flow(assess_env):
    api, fake = assess_env
    session_id = _run_interview(api)
    _queue_happy_path(fake)
    r = api.post(f"/api/sessions/{session_id}/assessment")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "COMPLETE"
    assert body["overallLevel"] == "Developing"
    assert {d["rubricDomain"] for d in body["domains"]} == set(RUBRIC_DOMAINS)
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
    extraction = _extraction()
    # fabricated turn label + cross-case reference get dropped by the validator
    extraction["domains"][0]["evidence_items"].append({
        "evidence_id": "ev_bad_01", "turn_label": "turn_99", "evidence_type": "strength",
        "label": "Fabricated", "severity": "", "student_excerpt": "never said",
        "patient_excerpt": "", "explanation": "x", "why_it_matters": "",
        "suggested_alternative": "", "confidence_level": "strong",
    })
    extraction["domains"][0]["evidence_items"].append({
        "evidence_id": "ev_bad_02", "turn_label": "turn_00", "evidence_type": "strength",
        "label": "Mentions Carly wrongly", "severity": "", "student_excerpt": "Hi Camden, how are you?",
        "patient_excerpt": "", "explanation": "Like Carly said before.", "why_it_matters": "",
        "suggested_alternative": "", "confidence_level": "strong",
    })
    fake.queue_structured(
        extraction,
        _evaluation("OARS Communication", evidence_ids=["ev_oars_01", "ev_bad_01", "ev_bad_02"]),
        _evaluation("History Checklist"),
        _evaluation("Red Flags / Safety Screening", "Insufficient Evidence"),
        _evaluation("Empathy & Patient-Centeredness"),
        _review(),
    )
    r = api.post(f"/api/sessions/{session_id}/assessment")
    assert r.status_code == 201
    body = r.json()
    labels = [e["label"] for d in body["domains"] for e in d["evidence"]]
    assert "Fabricated" not in labels
    assert "Mentions Carly wrongly" not in labels


def test_short_interview_can_return_insufficient_evidence(assess_env):
    api, fake = assess_env
    session_id = _run_interview(api, case_id="sofia", questions=("Hi",))
    extraction = {"domains": [{"rubric_domain": d, "evidence_items": []} for d in RUBRIC_DOMAINS]}
    fake.queue_structured(
        extraction,
        _evaluation("OARS Communication", "Insufficient Evidence"),
        _evaluation("History Checklist", "Insufficient Evidence"),
        _evaluation("Red Flags / Safety Screening", "Insufficient Evidence"),
        _evaluation("Empathy & Patient-Centeredness", "Insufficient Evidence"),
        {**_review(), "overall_level": "Insufficient Evidence"},
    )
    r = api.post(f"/api/sessions/{session_id}/assessment")
    assert r.status_code == 201
    body = r.json()
    assert body["overallLevel"] == "Insufficient Evidence"
    assert all(d["performanceLevel"] == "Insufficient Evidence" for d in body["domains"])


def test_rejected_domain_is_regenerated(assess_env):
    api, fake = assess_env
    session_id = _run_interview(api, case_id="carly", questions=("How are your wrists feeling?",))
    review_reject = _review(approved=True)
    review_reject["verdicts"][0]["approved"] = False
    review_reject["verdicts"][0]["issues"] = ["Level not supported by evidence"]
    fake.queue_structured(
        _extraction(student_text="How are your wrists feeling?"),
        _evaluation("OARS Communication", "Advanced"),
        _evaluation("History Checklist"),
        _evaluation("Red Flags / Safety Screening", "Insufficient Evidence"),
        _evaluation("Empathy & Patient-Centeredness"),
        review_reject,                                   # first review: rejects OARS
        _evaluation("OARS Communication", "Developing"),  # regeneration
        _review(),                                        # recheck approves
    )
    r = api.post(f"/api/sessions/{session_id}/assessment")
    assert r.status_code == 201
    body = r.json()
    oars = next(d for d in body["domains"] if d["rubricDomain"] == "OARS Communication")
    assert oars["performanceLevel"] == "Developing"
    assert body["status"] == "COMPLETE"


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
