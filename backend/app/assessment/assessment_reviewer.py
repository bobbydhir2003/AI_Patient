"""Stage 3: independent AI verification of the draft assessment."""
import json
from pathlib import Path

from app.patient_engine.openai_client import OpenAIPatientClient
from app.schemas.assessment_schema import (
    REVIEW_JSON_SCHEMA,
    DomainEvaluation,
    ExtractionResult,
    ReviewResult,
)

_PROMPT = Path(__file__).resolve().parent / "prompts" / "assessment_review_prompt.txt"


def review_assessment(
    client: OpenAIPatientClient,
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
    data = client.generate_structured(
        [{"role": "developer", "content": prompt},
         {"role": "user", "content": "Review and verify the draft assessment now."}],
        REVIEW_JSON_SCHEMA,
        "assessment_review",
        max_output_tokens=settings.openai_standard_assessment_max_output_tokens or 2500,
        allow_truncation_retry=True,
    )
    return ReviewResult.model_validate(data)
