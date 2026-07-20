from app.core.constants import CASE_IDS, DISCLOSURE_OPEN, DISCLOSURE_PROBE, DISCLOSURE_SENSITIVE, TOPICS
from app.patient_engine.case_loader import load_all_cases


def test_all_cases_load():
    cases = load_all_cases()
    assert set(cases.keys()) == set(CASE_IDS)
    assert {"camden", "carly", "sofia", "jayden"} <= set(cases.keys())
    assert len([c for c in cases.values() if c.case_category == "referral"]) == 4


def test_case_facts_are_well_formed():
    for case in load_all_cases().values():
        assert case.facts, f"{case.case_id} has no facts"
        seen_ids = set()
        for fact in case.facts:
            assert fact.id.startswith(case.case_id + "-")
            assert fact.id not in seen_ids
            seen_ids.add(fact.id)
            assert fact.topic in TOPICS
            assert fact.disclosure in (DISCLOSURE_OPEN, DISCLOSURE_PROBE, DISCLOSURE_SENSITIVE)
            assert len(fact.text.strip()) > 10


def test_case_ages_match_sources():
    cases = load_all_cases()
    assert cases["camden"].age == 4
    assert cases["carly"].age == 38
    assert cases["sofia"].age == 13
    assert cases["jayden"].age == 45


def test_cases_api_matches_frontend_shape(client):
    response = client.get("/api/cases")
    assert response.status_code == 200
    payload = response.json()
    cases = [c for s in payload["sections"] for c in s["cases"]]
    assert len(cases) == 8
    for item in cases:
        for key in ("id", "caseCategory", "name", "age", "image", "shortDescription",
                    "referralReason", "studentVisibleInfo", "task"):
            assert key in item, f"missing {key}"


def test_get_single_case_and_404(client):
    ok = client.get("/api/cases/sofia")
    assert ok.status_code == 200
    assert ok.json()["name"] == "Sofia"
    missing = client.get("/api/cases/nobody")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "case_not_found"
