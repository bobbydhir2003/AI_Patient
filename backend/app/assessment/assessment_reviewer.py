"""CALL 2: independent AI verification of the combined draft assessment.

Runs through the AssessmentCallBudget (so it counts as one logical call and its
usage is recorded with purpose="assessment_verify"). Its job is QUALITATIVE
judgment only - fairness, whether each performance level matches its evidence,
case-appropriateness, educational quality, and whether conclusions are supported.
Mechanical checks (real turn labels, verbatim excerpts, duplicate/foreign ids,
cross-case names, four-domain/level validity) are already done deterministically
in assessment_validator before this call, so we never spend a provider call on them.
"""
import json
from pathlib import Path

from app.schemas.assessment_schema import (
    REVIEW_JSON_SCHEMA,
    DomainEvaluation,
    ExtractionResult,
    ReviewResult,
)

_PROMPT = Path(__file__).resolve().parent / "prompts" / "assessment_review_prompt.txt"


def review_assessment(
    budget,
    transcript_text: str,
    case_reference: dict,
    extraction: ExtractionResult,
    evaluations: list[DomainEvaluation],
) -> ReviewResult:
    from app.core.config import get_settings

    prompt = (
        _PROMPT.read_text(encoding="utf-8")
        .replace("{{TRANSCRIPT}}", transcript_text)
        .replace("{{CASE_REFERENCE}}", json.dumps(case_reference, indent=1))
        .replace("{{EVIDENCE}}", json.dumps(extraction.model_dump(), indent=1))
        .replace("{{EVALUATIONS}}", json.dumps([e.model_dump() for e in evaluations], indent=1))
    )
    settings = get_settings()
    data = budget.generate_structured(
        [{"role": "developer", "content": prompt},
         {"role": "user", "content": "Review and verify the draft assessment now."}],
        REVIEW_JSON_SCHEMA,
        "assessment_review",
        stage="assessment_verify",
        max_output_tokens=settings.openai_standard_assessment_max_output_tokens or 2500,
        allow_truncation_retry=True,
    )
    return ReviewResult.model_validate(data)
