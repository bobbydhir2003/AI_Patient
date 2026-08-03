"""A patient must never leak identity or facts from another case."""
import re

import pytest


@pytest.fixture()
def client(student_client):
    return student_client

from app.core.constants import CASE_IDS
from app.patient_engine.case_loader import load_all_cases, load_case
from app.patient_engine.fact_selector import select_facts
from app.patient_engine.prompt_builder import build_developer_prompt
from app.patient_engine.response_validator import validate_response
from app.patient_engine.topic_classifier import classify
from tests.conftest import FakeOpenAIClient

_ALL_TOPIC_QUESTIONS = [
    "Tell me about your health condition and treatment.",
    "What medications do you take?",
    "Tell me about your family and home.",
    "How is school or work going?",
    "How do you sleep and eat?",
    "How have you been feeling emotionally?",
    "What are your goals?",
]


def test_case_files_do_not_mention_other_patients():
    for case_id in CASE_IDS:
        case = load_case(case_id)
        blob = " ".join(f.text.lower() for f in case.facts)
        for other_id, other in load_all_cases().items():
            if other_id == case_id:
                continue
            assert not re.search(rf"\b{other.display_name.lower()}\b", blob), (
                f"{case_id} facts mention {other.display_name}"
            )


def test_case_files_contain_no_scripted_dialogue():
    """Case JSONs are structured truth, not conversation scripts."""
    for case_id in CASE_IDS:
        case = load_case(case_id)
        raw = case.model_dump()
        assert "responses" not in raw
        for fact in case.facts:
            assert "?" != fact.text.strip()[-1:], "facts must not be questions"
        # no question->answer mapping keys anywhere
        assert all("question" not in f.model_dump() and "answer" not in f.model_dump() for f in case.facts)


def test_prompts_are_isolated_per_case():
    for case_id in CASE_IDS:
        case = load_case(case_id)
        for question in _ALL_TOPIC_QUESTIONS:
            facts = select_facts(case, classify(question))
            prompt = build_developer_prompt(case, facts).lower()
            for other_id, other in load_all_cases().items():
                if other_id == case_id:
                    continue
                assert not re.search(rf"\b{other.display_name.lower()}\b", prompt), (
                    f"Prompt for {case_id} leaked name '{other.display_name}'"
                )


def test_prompt_facts_all_belong_to_the_session_case():
    """The engine must never receive multiple cases together."""
    for case_id in CASE_IDS:
        case = load_case(case_id)
        for question in _ALL_TOPIC_QUESTIONS:
            for fact in select_facts(case, classify(question)):
                assert fact.id.startswith(case_id + "-")


def test_validator_blocks_cross_case_names():
    camden = load_case("camden")
    ok, _ = validate_response(camden, "My wrists hurt like Carly's do.")
    assert not ok
    ok2, _ = validate_response(camden, "I like playing with my trucks.")
    assert ok2


def test_switching_cases_creates_isolated_sessions(client):
    """Camden session then Carly session: separate ids, separate transcripts, correct case."""
    camden = client.post(
        "/api/sessions", json={"studentName": "Iso", "studentId": "", "caseId": "camden"}
    ).json()
    client.post(
        f"/api/interviews/{camden['sessionId']}/messages",
        json={"text": "Hi Camden", "caseId": "camden"},
    )
    carly = client.post(
        "/api/sessions", json={"studentName": "Iso", "studentId": "", "caseId": "carly"}
    ).json()
    assert carly["sessionId"] != camden["sessionId"]
    assert carly["caseId"] == "carly"
    assert carly["messages"] == []  # empty transcript for the new patient
    # And the camden transcript did not bleed over:
    fresh = client.get(f"/api/sessions/{carly['sessionId']}").json()
    assert fresh["messages"] == []
