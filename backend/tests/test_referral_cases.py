"""Referral & Interprofessional case section: catalog, protection, isolation."""
import json
import re
from pathlib import Path

from app.core.constants import REFERRAL_CASE_IDS, STANDARD_CASE_IDS
from app.patient_engine.case_loader import load_all_cases, load_case
from app.patient_engine.fact_selector import select_facts
from app.patient_engine.prompt_builder import build_developer_prompt
from app.patient_engine.topic_classifier import classify

SRC = Path(__file__).resolve().parent.parent.parent / "src"

# Words that would reveal the hidden educational objective if leaked publicly.
HIDDEN_MARKERS = (
    "hidden_context", "hiddenContext", "referral_context", "referralContext",
    "interprofessional_context", "disclosure_guidance", "care_pathways",
    "scope_considerations", "safety_considerations",
    "restricting food", "weight quickly", "sleep aid", "choking", "swallow", "hopeless",
)


def test_catalog_returns_both_sections(client):
    body = client.get("/api/cases").json()
    sections = {s["id"]: s for s in body["sections"]}
    assert set(sections) == {"standard", "referral"}
    assert sections["referral"]["description"].startswith("Practice recognizing concerns")
    assert len(sections["standard"]["cases"]) == 4
    assert len(sections["referral"]["cases"]) == 4


def test_standard_cases_preserved(client):
    body = client.get("/api/cases").json()
    standard = next(s for s in body["sections"] if s["id"] == "standard")
    assert {c["id"] for c in standard["cases"]} == set(STANDARD_CASE_IDS)
    camden = next(c for c in standard["cases"] if c["id"] == "camden")
    assert camden["name"] == "Camden" and camden["age"] == 4


def test_grouping_is_by_category_not_id(client):
    body = client.get("/api/cases").json()
    for section in body["sections"]:
        for case in section["cases"]:
            assert case["caseCategory"] == section["id"]


def test_public_catalog_contains_no_hidden_fields(client):
    text = json.dumps(client.get("/api/cases").json()).lower()
    for marker in HIDDEN_MARKERS:
        assert marker.lower() not in text, f"public catalog leaks '{marker}'"
    # neutral titles: educational objective must not be in card titles
    for revealing in ("nutrition referral", "medication referral", "mental health", "swallowing"):
        assert revealing not in text


def test_public_single_case_contains_no_hidden_fields(client):
    for case_id in REFERRAL_CASE_IDS:
        response = client.get(f"/api/cases/{case_id}")
        assert response.status_code == 200
        text = json.dumps(response.json()).lower()
        for marker in HIDDEN_MARKERS:
            assert marker.lower() not in text, f"{case_id} leaks '{marker}'"


def test_unknown_case_returns_404(client):
    assert client.get("/api/cases/not_a_case").status_code == 404


def test_referral_session_stores_category_and_capabilities(client):
    response = client.post(
        "/api/sessions",
        json={"studentName": "Ref Tester", "studentId": "", "caseId": "referral_case_01"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["caseCategory"] == "referral"
    assert set(body["assessmentCapabilities"]) == {"standard_interview", "advanced_referral"}
    assert body["protectedReferenceVersion"] == "1.0"
    # standard case: standard capabilities
    std = client.post(
        "/api/sessions", json={"studentName": "Ref Tester", "studentId": "", "caseId": "camden"}
    ).json()
    assert std["caseCategory"] == "standard"
    assert std["assessmentCapabilities"] == ["standard_interview"]


def test_referral_prompt_contains_own_hidden_context_only():
    for case_id in REFERRAL_CASE_IDS:
        case = load_case(case_id)
        assert case.referral_context is not None
        prompt = build_developer_prompt(case, select_facts(case, classify("How are you feeling?")))
        # the AI patient gets its own hidden context...
        assert case.referral_context.hidden_context[:40] in prompt
        assert "Never say or hint that the student should refer you" in prompt
        # ...and never another case's hidden context or patient names
        for other_id in REFERRAL_CASE_IDS:
            if other_id == case_id:
                continue
            other = load_case(other_id)
            assert other.referral_context.hidden_context[:40] not in prompt
            assert not re.search(
                rf"\b{other.display_name.lower()}\b", prompt.lower()
            ), f"{case_id} prompt leaks {other.display_name}"


def test_standard_cases_have_no_referral_block():
    camden = load_case("camden")
    prompt = build_developer_prompt(camden, [])
    assert "HIDDEN CONTEXT" not in prompt


def test_referral_facts_use_progressive_disclosure():
    for case_id in REFERRAL_CASE_IDS:
        case = load_case(case_id)
        levels = {f.disclosure for f in case.facts}
        assert {"open", "probe", "sensitive"} <= levels, (
            f"{case_id} must stage its hidden concern across disclosure levels"
        )


def test_referral_case_files_contain_no_scoring_rules():
    backend = Path(__file__).resolve().parent.parent
    for case_id in REFERRAL_CASE_IDS:
        blob = (backend / "app" / "cases" / f"{case_id}.json").read_text(encoding="utf-8").lower()
        for banned in ('"required_question"', '"if_question_contains"', '"points"', '"score"', '"pass"', '"fail"'):
            assert banned not in blob, f"{case_id} contains scoring rule {banned}"


def test_frontend_catalog_is_data_driven():
    """No case ids may be hardcoded in the catalog UI."""
    if not SRC.exists():
        return
    ui_files = [
        SRC / "pages" / "CaseCatalogPage.tsx",
        SRC / "components" / "cases" / "CaseSection.tsx",
        SRC / "components" / "cases" / "CaseCard.tsx",
    ]
    for path in ui_files:
        content = path.read_text(encoding="utf-8")
        assert "referral_case_" not in content, f"{path.name} hardcodes a referral case id"
        for cid in STANDARD_CASE_IDS:
            assert f'"{cid}"' not in content, f"{path.name} hardcodes case id {cid}"

from app.schemas.referral_assessment_schema import (
    ReferralExtraction, DomainEvidenceExtraction, ExcerptItem, ReferralDomainEvaluation, ReferralReview, ReferralDomainReview
)
from app.assessment.referral_assessment_service import _validate_extraction
from app.models import ConversationTurn
from app.assessment.transcript_preparer import PreparedTranscript

def _make_prepared():
    return PreparedTranscript(
        text="Student: Hi\nPatient: I fall.",
        student_turn_count=1,
        label_to_turn={
            "turn_00": ConversationTurn(id="t0", turn_index=0, role="student", content="Hi"),
            "turn_01": ConversationTurn(id="t1", turn_index=1, role="patient", content="I fall.")
        }
    )

def test_validate_extraction_drops_invalid_speaker():
    p = _make_prepared()
    ext = ReferralExtraction(
        referral_status="active",
        domain_evidence=[
            DomainEvidenceExtraction(
                domain_id="concern_recognition",
                student_evidence=[ExcerptItem(turn_label="turn_01", excerpt="I fall.")], # Patient turn used as student!
                patient_context_evidence=[],
                missed_opportunity_evidence=[],
                assessability="assessable",
                reason=""
            )
        ]
    )
    res = _validate_extraction(ext, p, ["concern_recognition"])
    # Should drop the student evidence because turn_01 is patient
    assert len(res.domain_evidence[0].student_evidence) == 0
    # Assessability should fall back to insufficient_evidence
    assert res.domain_evidence[0].assessability == "insufficient_evidence"

def test_validate_extraction_drops_invalid_excerpt():
    p = _make_prepared()
    ext = ReferralExtraction(
        referral_status="active",
        domain_evidence=[
            DomainEvidenceExtraction(
                domain_id="concern_recognition",
                student_evidence=[ExcerptItem(turn_label="turn_00", excerpt="Did you fall?")], # Not in transcript!
                patient_context_evidence=[],
                missed_opportunity_evidence=[],
                assessability="assessable",
                reason=""
            )
        ]
    )
    res = _validate_extraction(ext, p, ["concern_recognition"])
    assert len(res.domain_evidence[0].student_evidence) == 0
    assert res.domain_evidence[0].assessability == "insufficient_evidence"

def test_validate_extraction_keeps_valid_evidence():
    p = _make_prepared()
    ext = ReferralExtraction(
        referral_status="active",
        domain_evidence=[
            DomainEvidenceExtraction(
                domain_id="concern_recognition",
                student_evidence=[ExcerptItem(turn_label="turn_00", excerpt="Hi")],
                patient_context_evidence=[ExcerptItem(turn_label="turn_01", excerpt="I fall.")],
                missed_opportunity_evidence=[],
                assessability="assessable",
                reason=""
            )
        ]
    )
    res = _validate_extraction(ext, p, ["concern_recognition"])
    assert len(res.domain_evidence[0].student_evidence) == 1
    assert len(res.domain_evidence[0].patient_context_evidence) == 1
    assert res.domain_evidence[0].assessability == "assessable"
