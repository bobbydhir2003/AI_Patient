"""CALL 1 (and, when needed, CALL 3): combined structured assessment.

Produces evidence + qualitative evaluation for ALL four rubric domains, plus the
overall impression, in a SINGLE OpenAI request. This replaces the old pipeline's
1 evidence-extraction call + 4 per-domain evaluation calls (the source of the
6-11 calls per assessment).

The heavy lifting of counting logical provider calls, enforcing the per-
assessment call budget, and recording usage lives in the AssessmentCallBudget
passed in as `budget`; this module only builds the prompt and validates shape.
"""
import json
from pathlib import Path

from app.core.config import get_settings
from app.schemas.assessment_schema import (
    COMBINED_ASSESSMENT_JSON_SCHEMA,
    CombinedAssessmentResult,
)

_PROMPT = Path(__file__).resolve().parent / "prompts" / "combined_assessment_prompt.txt"

# Generous default output budget: the combined response carries evidence +
# evaluation for four domains plus the overall block, so it needs materially more
# room than any single old per-domain call.
_DEFAULT_MAX_OUTPUT_TOKENS = 8000


def generate_combined(
    budget,
    transcript_text: str,
    rubrics: list[dict],
    case_reference: dict,
    *,
    stage: str,
    correction_feedback: list[str] | None = None,
) -> CombinedAssessmentResult:
    """Run the combined generation (or correction) request through `budget`.

    `stage` is the usage/telemetry label ("assessment_generate" for CALL 1,
    "assessment_correction" for CALL 3). `correction_feedback`, when present,
    lists the verifier's concrete issues so the model fixes the whole assessment
    in ONE combined request (never a per-domain regeneration loop).
    """
    prompt = (
        _PROMPT.read_text(encoding="utf-8")
        .replace("{{RUBRICS}}", json.dumps(rubrics, indent=1))
        .replace("{{CASE_REFERENCE}}", json.dumps(case_reference, indent=1))
        .replace("{{TRANSCRIPT}}", transcript_text)
    )
    user = "Produce the complete four-domain assessment now."
    if correction_feedback:
        user += (
            " An independent verification reviewer flagged the following problems "
            "with a previous draft; correct ALL of them across the affected domains "
            "while keeping every well-supported finding: "
            + "; ".join(correction_feedback)
        )
    settings = get_settings()
    max_tokens = (
        settings.openai_standard_assessment_max_output_tokens or _DEFAULT_MAX_OUTPUT_TOKENS
    )
    data = budget.generate_structured(
        [{"role": "developer", "content": prompt}, {"role": "user", "content": user}],
        COMBINED_ASSESSMENT_JSON_SCHEMA,
        "combined_assessment",
        stage=stage,
        max_output_tokens=max_tokens,
        allow_truncation_retry=True,
    )
    return CombinedAssessmentResult.model_validate(data)
