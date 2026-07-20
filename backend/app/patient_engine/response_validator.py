"""Validates a generated patient response before it is saved."""
import re

from app.core.constants import CASE_IDS, MAX_PATIENT_RESPONSE_CHARS
from app.patient_engine.case_loader import load_all_cases
from app.schemas.case_schema import CaseDefinition

_FORBIDDEN_PHRASES = (
    "as an ai",
    "language model",
    "i am an ai",
    "i'm an ai",
    "openai",
    "system prompt",
    "developer prompt",
    "i cannot roleplay",
    "simulated patient",
)


def _other_case_names(case: CaseDefinition) -> list[str]:
    names: list[str] = []
    for other_id, other in load_all_cases().items():
        if other_id == case.case_id:
            continue
        names.append(other.display_name.lower())
        names.append(other.full_name.lower())
    return names


# Internal/streaming artifacts that must never appear in spoken patient text.
# These supplement (never replace) the full validate_response checks below.
_STREAM_LEAK_MARKERS = (
    "===meta===",
    "used_fact_ids",
    "response_type",
    "patient_text",
    "pause_before_ms",
    "fact id",
    "fact_id",
    "hidden context",
    "hidden-concern",
    "disclosure level",
    "disclosure_guidance",
    "developer prompt",
)


def validate_stream_text(case: CaseDefinition, text: str) -> tuple[bool, str]:
    """Sentence-gate validation for the streaming pipeline.

    Runs the SAME full-response validator (character break, leakage, other-case
    names) on the cumulative text, then adds streaming-specific checks: the
    metadata delimiter, internal schema field names, and this case's literal
    fact IDs must never be spoken. Returns (is_valid, cleaned_text).
    """
    valid, cleaned = validate_response(case, text)
    if not valid:
        return False, cleaned
    lowered = cleaned.lower()
    if any(marker in lowered for marker in _STREAM_LEAK_MARKERS):
        return False, cleaned
    for fact in case.facts:
        if fact.id.lower() in lowered:
            return False, cleaned
    return True, cleaned


def validate_response(case: CaseDefinition, text: str) -> tuple[bool, str]:
    """Return (is_valid, cleaned_text)."""
    cleaned = re.sub(r"[*_#`>]+", "", (text or "")).strip()
    if not cleaned:
        return False, ""
    if len(cleaned) > MAX_PATIENT_RESPONSE_CHARS:
        cleaned = cleaned[:MAX_PATIENT_RESPONSE_CHARS].rsplit(" ", 1)[0].rstrip(".,;: ") + "..."
    lowered = cleaned.lower()
    if any(phrase in lowered for phrase in _FORBIDDEN_PHRASES):
        return False, cleaned
    # Case isolation: a response must never reference another case's patient.
    if any(re.search(rf"\b{re.escape(name)}\b", lowered) for name in _other_case_names(case)):
        return False, cleaned
    return True, cleaned
