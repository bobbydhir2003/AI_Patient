"""Development-only stand-in for the OpenAI boundary (AI_SP_DEV_FAKE_AI).

Lets the AI iEXCEL Course 1 flow run end to end on a laptop without paid or
production AI services. Active only when ENVIRONMENT=development (the settings
refuse the switch in production/staging). It produces structurally valid,
clearly synthetic outputs; everything downstream of it (validation, storage,
completion, reporting) is the real code path.

Supported: the standard-case assessment pipeline (combined_assessment +
assessment_review) and patient replies. Referral assessments are not faked.
"""
import re

from app.core.constants import RUBRIC_DOMAINS
from app.core.exceptions import PatientEngineError
from app.schemas.interview_schema import PatientReply

_STUDENT_LINE = re.compile(r"^\[(turn_\d+)\] STUDENT: (.+)$", re.M)
_LEVEL = "Developing"
_SUMMARY = "Synthetic development assessment (fake AI provider) - not real feedback."

# Scripted synthetic interview for the development "simulate interview" action.
SIMULATED_EXCHANGE = (
    ("Hello, my name is Alex and I'm a student physical therapist. How are you today?",
     "Hi. I'm okay, a little nervous."),
    ("What brings you in to see us today?",
     "I get tired faster than I used to when I'm playing."),
    ("Can you tell me more about when you notice getting tired?",
     "Mostly after school when I try to run around with my friends."),
    ("Is there anything you'd like to be able to do again?",
     "I'd like to keep up at recess."),
)


def _domain(name: str, items: list[dict]) -> dict:
    return {
        "rubric_domain": name,
        "performance_level": _LEVEL,
        "summary": "Synthetic summary for development.",
        "narrative": "Synthetic narrative generated without an AI provider.",
        "strengths": ["Opened the conversation politely."],
        "areas_for_growth": ["Explore the main concern in more depth."],
        "evidence_items": items,
    }


def _combined(prompt: str) -> dict:
    match = _STUDENT_LINE.search(prompt)
    items = []
    if match:
        items.append({
            "evidence_id": "ev_dev_01", "turn_label": match.group(1),
            "evidence_type": "strength", "label": "Polite opening", "severity": "",
            "student_excerpt": match.group(2).strip(), "patient_excerpt": "",
            "explanation": "The student opened the interview politely.",
            "why_it_matters": "", "suggested_alternative": "", "confidence_level": "moderate",
        })
    return {
        "domains": [_domain(name, items if i == 0 else []) for i, name in enumerate(RUBRIC_DOMAINS)],
        "overall_level": _LEVEL,
        "overall_summary": _SUMMARY,
        "focus_areas": [],
    }


def _review() -> dict:
    return {
        "verdicts": [
            {"rubric_domain": d, "approved": True, "issues": [], "rejected_evidence_ids": []}
            for d in RUBRIC_DOMAINS
        ],
        "overall_level": _LEVEL,
        "overall_summary": _SUMMARY,
        "focus_areas": [],
    }


class DevFakeAIClient:
    configured = True

    def generate_structured(self, messages, schema, schema_name, max_output_tokens=None,
                            allow_truncation_retry=False, usage_out=None, timeout_seconds=None) -> dict:
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        if schema_name == "combined_assessment":
            return _combined(prompt)
        if schema_name == "assessment_review":
            return _review()
        if schema_name == "patient_reply":
            return {"patient_text": SIMULATED_EXCHANGE[0][1], "used_fact_ids": [],
                    "response_type": "clinical_answer", "supported": True, "speech": None}
        raise PatientEngineError(f"The development AI stand-in does not support '{schema_name}'.")

    def generate(self, messages, usage_out=None) -> PatientReply:
        return PatientReply.model_validate(self.generate_structured(messages, {}, "patient_reply"))
