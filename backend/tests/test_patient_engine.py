import pytest

from app.core.exceptions import PatientResponseUnavailableError
from app.patient_engine import generate_patient_response
from app.patient_engine.case_loader import load_case
from app.patient_engine.disclosure_manager import DisclosureManager
from app.patient_engine.fact_selector import select_facts
from app.patient_engine.prompt_builder import build_developer_prompt, build_messages, load_template
from app.patient_engine.response_validator import validate_response
from app.patient_engine.topic_classifier import classify, is_follow_up
from tests.conftest import FakeOpenAIClient


def test_classifier_matches_expected_topics():
    assert "symptoms_pain" in classify("Where does it hurt the most?")
    assert "family_social" in classify("Tell me about your family.")
    assert "medications" in classify("What medications are you taking?")
    assert "activity_exercise" in classify("Do you still go to dance class?")
    assert classify("qwertyuiop zxcvbnm") == ["other"]


def test_classifier_handles_presenting_concern_and_body_parts():
    assert "condition" in classify("What brought you in today?")
    assert "symptoms_pain" in classify("Tell me about your wrists")
    assert "greeting" in classify("Hi")
    assert "greeting" in classify("How are you?")


def test_follow_up_detection_and_topic_inheritance():
    assert is_follow_up("How does that affect your daily life?")
    result_topics = classify("How does that affect your daily life?")
    assert "function_mobility" in result_topics  # "daily life" keyword
    # A pure pronoun follow-up inherits the active topic inside the engine:
    client = FakeOpenAIClient(text="It makes mornings hard.", response_type="follow_up_answer")
    result = generate_patient_response(
        case_id="sofia",
        question="Why is that?",
        turns=[],
        disclosed_fact_ids=set(),
        active_topic="symptoms_pain",
        client=client,
    )
    assert "symptoms_pain" in result.topics
    assert result.active_topic == "symptoms_pain"


def test_disclosure_probe_facts_need_direct_question():
    case = load_case("camden")
    manager = DisclosureManager(case, set())
    med_facts = [f for f in case.facts if f.topic == "medications"]
    assert manager.eligible_facts(med_facts, ["activity_exercise"]) == []
    assert manager.eligible_facts(med_facts, ["medications"])


def test_sensitive_facts_require_sensitive_topic():
    case = load_case("carly")
    manager = DisclosureManager(case, set())
    sensitive = [f for f in case.facts if f.disclosure == "sensitive"]
    assert sensitive
    assert manager.eligible_facts(sensitive, ["exam_findings"]) == []
    assert manager.eligible_facts(sensitive, ["emotional_wellbeing"])


def test_prompt_contains_persona_facts_rules_and_greeting_instruction():
    case = load_case("sofia")
    facts = select_facts(case, ["symptoms_pain"])
    prompt = build_developer_prompt(case, facts)
    assert "Sofia Hernandez" in prompt
    assert "Never mention that you are an AI" in prompt
    assert "small talk" in prompt  # greeting/small-talk behavior instruction
    assert any(f.text[:30] in prompt for f in facts)


def test_prompt_has_no_scripted_answers():
    template = load_template()
    assert "→" not in template
    assert '"answer"' not in template.lower()


def test_messages_end_with_student_question():
    case = load_case("jayden")
    from app.patient_engine.context_resolver import InterviewContext

    context = InterviewContext(case_id="jayden", topics=["condition"])
    messages = build_messages(case, [], context, "How was your diagnosis explained to you?")
    assert messages[0]["role"] == "developer"
    assert messages[-1] == {"role": "user", "content": "How was your diagnosis explained to you?"}


def test_validator_rejects_ai_disclosure_and_empty():
    case = load_case("camden")
    ok, _ = validate_response(case, "I get tired when I play outside.")
    assert ok
    bad, _ = validate_response(case, "As an AI language model I cannot answer.")
    assert not bad
    empty, _ = validate_response(case, "   ")
    assert not empty


def test_engine_returns_validated_openai_reply():
    client = FakeOpenAIClient(text="I get tired really fast when we play.", response_type="clinical_answer")
    result = generate_patient_response(
        case_id="camden",
        question="How do you feel when you play with your brother?",
        turns=[],
        disclosed_fact_ids=set(),
        client=client,
    )
    assert result.text == "I get tired really fast when we play."
    assert result.response_type == "clinical_answer"
    assert client.calls, "OpenAI boundary was not called"


def test_engine_raises_on_failure_no_canned_reply():
    client = FakeOpenAIClient(fail=True)
    with pytest.raises(PatientResponseUnavailableError):
        generate_patient_response(
            case_id="camden",
            question="Do you have any pain right now?",
            turns=[],
            disclosed_fact_ids=set(),
            client=client,
        )


def test_engine_rejects_character_breaking_reply():
    client = FakeOpenAIClient(text="As an AI language model, I do not have wrists.")
    with pytest.raises(PatientResponseUnavailableError):
        generate_patient_response(
            case_id="carly",
            question="How are your wrists feeling?",
            turns=[],
            disclosed_fact_ids=set(),
            client=client,
        )


def test_only_eligible_fact_ids_are_recorded():
    client = FakeOpenAIClient(
        text="My wrists hurt when I lift things.",
        used_fact_ids=["carly-pain-01", "camden-pain-01", "not-a-real-id"],
    )
    result = generate_patient_response(
        case_id="carly",
        question="Tell me about your wrist pain.",
        turns=[],
        disclosed_fact_ids=set(),
        client=client,
    )
    assert "carly-pain-01" in result.used_fact_ids
    assert all(fid.startswith("carly-") for fid in result.used_fact_ids)
