"""Code-level structural validation. This module makes NO educational judgments -
it only checks that the AI output is well-formed, grounded in real transcript
turns, and free of cross-case contamination."""
from app.core.constants import CASE_IDS, PERFORMANCE_LEVELS, RUBRIC_DOMAINS
from app.patient_engine.case_loader import load_all_cases
from app.assessment.transcript_preparer import PreparedTranscript
from app.schemas.assessment_schema import DomainEvaluation, ExtractionResult


class AssessmentValidationError(Exception):
    pass


def _other_case_names(case_id: str) -> list[str]:
    names: list[str] = []
    for other_id, other in load_all_cases().items():
        if other_id != case_id:
            names.append(other.display_name.lower())
            names.extend(n for n in other.full_name.lower().split() if len(n) >= 4)
    return names


def validate_extraction(
    extraction: ExtractionResult, prepared: PreparedTranscript, case_id: str
) -> ExtractionResult:
    """Drop ungrounded evidence items; fail on structural problems."""
    seen_ids: set[str] = set()
    other_names = _other_case_names(case_id)
    for domain in extraction.domains:
        kept = []
        for item in domain.evidence_items:
            if item.evidence_id in seen_ids:
                continue  # duplicated ids are dropped
            turn = prepared.label_to_turn.get(item.turn_label)
            if turn is None:
                continue  # fabricated turn label: drop
            if item.student_excerpt:
                anchor = turn if turn.role == "student" else None
                if anchor is None or item.student_excerpt.strip() not in anchor.content:
                    # try to keep items anchored to student turns only with real quotes
                    continue
            blob = " ".join(
                [item.label, item.explanation, item.why_it_matters, item.suggested_alternative]
            ).lower()
            if any(f" {name} " in f" {blob} " for name in other_names):
                continue  # cross-case contamination: drop
            seen_ids.add(item.evidence_id)
            kept.append(item)
        domain.evidence_items = kept
    return extraction


def validate_evaluations(
    evaluations: list[DomainEvaluation], extraction: ExtractionResult, case_id: str
) -> None:
    if case_id not in CASE_IDS:
        raise AssessmentValidationError(f"Unknown case '{case_id}'")
    domains = {e.rubric_domain for e in evaluations}
    if domains != set(RUBRIC_DOMAINS):
        raise AssessmentValidationError(f"Expected all four rubric domains, got {sorted(domains)}")
    valid_ids = {
        item.evidence_id for d in extraction.domains for item in d.evidence_items
    }
    other_names = _other_case_names(case_id)
    for evaluation in evaluations:
        if evaluation.performance_level not in PERFORMANCE_LEVELS:
            raise AssessmentValidationError(
                f"Invalid performance level '{evaluation.performance_level}'"
            )
        evaluation.evidence_ids = [eid for eid in evaluation.evidence_ids if eid in valid_ids]
        blob = " ".join(
            [evaluation.summary, evaluation.narrative]
            + evaluation.strengths
            + evaluation.areas_for_growth
        ).lower()
        if any(f" {name} " in f" {blob} " for name in other_names):
            raise AssessmentValidationError(
                f"Cross-case reference detected in '{evaluation.rubric_domain}' evaluation"
            )
