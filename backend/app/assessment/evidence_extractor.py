"""Stage 1: AI evidence extraction (no performance levels yet)."""
import json
from pathlib import Path

from app.patient_engine.openai_client import OpenAIPatientClient
from app.schemas.assessment_schema import EXTRACTION_JSON_SCHEMA, ExtractionResult

_PROMPT = Path(__file__).resolve().parent / "prompts" / "evidence_extraction_prompt.txt"


def extract_evidence(
    client: OpenAIPatientClient,
    transcript_text: str,
    rubrics: list[dict],
    case_reference: dict,
) -> ExtractionResult:
    from app.core.config import get_settings
    prompt = (
        _PROMPT.read_text(encoding="utf-8")
        .replace("{{RUBRICS}}", json.dumps(rubrics, indent=1))
        .replace("{{CASE_REFERENCE}}", json.dumps(case_reference, indent=1))
        .replace("{{TRANSCRIPT}}", transcript_text)
    )
    settings = get_settings()
    data = client.generate_structured(
        [{"role": "developer", "content": prompt},
         {"role": "user", "content": "Extract the rubric evidence now."}],
        EXTRACTION_JSON_SCHEMA,
        "evidence_extraction",
        max_output_tokens=settings.openai_standard_assessment_max_output_tokens or 4000,
        allow_truncation_retry=True,
    )
    return ExtractionResult.model_validate(data)
