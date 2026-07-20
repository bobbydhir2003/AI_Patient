"""Stage 2: AI rubric evaluation (qualitative levels, no numbers)."""
import json
from pathlib import Path

from app.patient_engine.openai_client import OpenAIPatientClient
from app.schemas.assessment_schema import (
    EVALUATION_JSON_SCHEMA,
    DomainEvaluation,
    DomainEvidence,
)

_PROMPT = Path(__file__).resolve().parent / "prompts" / "rubric_evaluation_prompt.txt"


def evaluate_domain(
    client: OpenAIPatientClient,
    transcript_text: str,
    rubric: dict,
    case_reference: dict,
    domain_evidence: DomainEvidence | None,
    reviewer_feedback: list[str] | None = None,
) -> DomainEvaluation:
    from app.core.config import get_settings
    evidence_json = domain_evidence.model_dump() if domain_evidence else {"evidence_items": []}
    prompt = (
        _PROMPT.read_text(encoding="utf-8")
        .replace("{{RUBRIC}}", json.dumps(rubric, indent=1))
        .replace("{{CASE_REFERENCE}}", json.dumps(case_reference, indent=1))
        .replace("{{TRANSCRIPT}}", transcript_text)
        .replace("{{EVIDENCE}}", json.dumps(evidence_json, indent=1))
    )
    user = "Evaluate this rubric domain now."
    if reviewer_feedback:
        user += (
            " A previous draft was rejected by the verification reviewer for these "
            "reasons; address them: " + "; ".join(reviewer_feedback)
        )
    settings = get_settings()
    data = client.generate_structured(
        [{"role": "developer", "content": prompt}, {"role": "user", "content": user}],
        EVALUATION_JSON_SCHEMA,
        "rubric_evaluation",
        max_output_tokens=settings.openai_standard_assessment_max_output_tokens or 1500,
        allow_truncation_retry=True,
    )
    return DomainEvaluation.model_validate(data)
