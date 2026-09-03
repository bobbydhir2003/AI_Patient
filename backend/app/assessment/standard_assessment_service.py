"""Orchestrates the three-stage AI assessment pipeline.

All educational judgment comes from the AI stages; this module only sequences
calls, validates structure/grounding, and persists results. If generation
fails, the run is marked FAILED and NO feedback is fabricated.
"""
import json
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.assessment import (
    assessment_reviewer,
    combined_assessment,
    rubric_loader,
    transcript_preparer,
)
from app.assessment.assessment_call_budget import (
    AssessmentCallBudget,
    AssessmentCallBudgetExceeded,
)
from app.assessment.assessment_repository import AssessmentRepository
from app.assessment.assessment_validator import (
    AssessmentValidationError,
    validate_evaluations,
    validate_extraction,
)
from app.core.config import get_settings
from app.core.constants import (
    ASSESSMENT_PROMPT_VERSION,
    MIN_STUDENT_TURNS_FOR_ASSESSMENT,
    RUBRIC_DOMAINS,
    SESSION_STATUS_COMPLETED,
)
from app.core.exceptions import (
    AssessmentNotFoundError,
    AssessmentNotPossibleError,
    AssessmentUnavailableError,
    SessionNotCompletedError,
    SessionNotFoundError,
)
from app.core.logging import get_logger
from app.patient_engine.case_loader import load_case
from app.patient_engine.openai_client import OpenAIPatientClient
from app.repositories.session_repository import SessionRepository
from app.repositories.transcript_repository import TranscriptRepository
from app.schemas.assessment_schema import (
    AssessmentOut,
    AssessmentTurnOut,
    DomainEvaluation,
    DomainResultOut,
    EvidenceOut,
    FocusAreaOut,
    RubricOut,
    TranscriptMarkerOut,
)

logger = get_logger(__name__)


def list_rubrics() -> list[RubricOut]:
    return [
        RubricOut(
            rubric_id=r["rubric_id"],
            domain=r["domain"],
            version=r["version"],
            student_facing_description=r["student_facing_description"],
            criteria=r["criteria"],
        )
        for r in rubric_loader.load_rubrics()
    ]


def _prepare(db: Session, session):
    """Load + validate everything the pipeline needs from the completed session."""
    turns = TranscriptRepository(db).list_turns(session.id)
    prepared = transcript_preparer.prepare_transcript(turns)
    if prepared.student_turn_count < MIN_STUDENT_TURNS_FOR_ASSESSMENT:
        raise AssessmentNotPossibleError(
            "The interview contains no student questions, so there is nothing to assess."
        )
    load_case(session.case_id)  # sanity: known case
    case_reference = rubric_loader.load_case_reference(session.case_id)
    rubrics = rubric_loader.load_rubrics()
    return prepared, case_reference, rubrics


def generate_assessment(db: Session, session_id: str, client: OpenAIPatientClient, retry: bool = False) -> AssessmentOut:
    """Synchronous path (used when the background queue is disabled and by the
    sync test suite): create a PROCESSING run and run the pipeline inline."""
    logger.info("assessment_requested session_id=%s endpoint=POST /api/sessions/{id}/assessment", session_id)
    session = SessionRepository(db).get(session_id)
    if session is None:
        raise SessionNotFoundError(session_id)
    if session.status != SESSION_STATUS_COMPLETED or not session.locked:
        raise SessionNotCompletedError(session_id)

    existing = AssessmentRepository(db).latest_for_session(session_id)
    if existing is not None and existing.status in ("COMPLETE", "NEEDS_REVIEW"):
        logger.info(
            "assessment_existing_returned session_id=%s assessment_id=%s status=%s",
            session_id, existing.id, existing.status,
        )
        return _run_to_out(db, existing)

    _prepare(db, session)  # validates min turns before creating a run
    settings = get_settings()
    run = AssessmentRepository(db).create_run(
        session_id=session_id,
        case_id=session.case_id,
        rubric_version=rubric_loader.rubric_version(),
        model_name=settings.openai_model,
        prompt_version=ASSESSMENT_PROMPT_VERSION,
        status="PROCESSING",
    )
    db.commit()
    logger.info("assessment_created session_id=%s assessment_id=%s status=PROCESSING", session_id, run.id)
    return execute_pipeline(db, run, session, client)


def execute_existing(db: Session, run, client: OpenAIPatientClient) -> AssessmentOut:
    """Background-worker entry: run the pipeline on an already-created run."""
    session = SessionRepository(db).get(run.session_id)
    if session is None:
        raise SessionNotFoundError(run.session_id)
    run.status = "PROCESSING"
    db.commit()
    return execute_pipeline(db, run, session, client)


def _aggregate_issues(review) -> list[str]:
    """Concrete, domain-tagged issues from unapproved verdicts, for ONE combined
    correction request. Never a per-domain regeneration loop."""
    issues: list[str] = []
    for v in review.verdicts:
        if not v.approved:
            for issue in v.issues:
                issues.append(f"[{v.rubric_domain}] {issue}")
    return issues or ["A domain evaluation was not adequately supported by its evidence."]


def _evidence_count(extraction) -> int:
    return sum(len(d.evidence_items) for d in extraction.domains)


def _missing_domains(combined) -> list[str]:
    """Required rubric domains absent from a combined result (order-preserving).

    The strengthened json-schema forces exactly four domain objects, each with a
    domain from the canonical enum - but json-schema cannot express "all four
    DISTINCT", so a response can still repeat one domain and omit another. This
    is the single check both the recovery retry and the final validator care
    about.
    """
    present = {d.rubric_domain for d in combined.domains}
    return [d for d in RUBRIC_DOMAINS if d not in present]


def _recover_missing_domains(
    budget, combined, transcript_text, rubrics, case_reference, session_id, assessment_id,
):
    """ONE bounded recovery attempt when CALL 1 came back missing a required
    rubric domain (the confirmed production failure: got ['OARS Communication']).

    Deterministic, non-looping: at most one extra logical call, only if the
    per-assessment call budget still allows it. The correction feedback names the
    exact missing domains and demands all four. If no budget remains, or the
    retry still doesn't cover all four, we return the best result we have and let
    the final validator fail the run cleanly rather than retry forever.
    """
    missing = _missing_domains(combined)
    if not missing:
        return combined
    logger.warning(
        "assessment_missing_domains session_id=%s assessment_id=%s missing=%s",
        session_id, assessment_id, missing,
    )
    if not budget.can_call():
        return combined  # no budget for recovery; validator will fail cleanly
    feedback = [
        "The previous draft was missing required rubric domain(s): "
        + ", ".join(missing)
        + ". Return a COMPLETE assessment containing ALL FOUR rubric domains, "
        "exactly once each: " + ", ".join(RUBRIC_DOMAINS) + "."
    ]
    logger.info(
        "assessment_domain_recovery_requested session_id=%s assessment_id=%s missing=%s",
        session_id, assessment_id, missing,
    )
    recovered = combined_assessment.generate_combined(
        budget, transcript_text, rubrics, case_reference,
        stage="assessment_domain_recovery", correction_feedback=feedback,
    )
    # Adopt the recovery only if it covers strictly more domains; otherwise keep
    # whichever result carries more domain objects (never fewer).
    if not _missing_domains(recovered):
        return recovered
    if len(_missing_domains(recovered)) < len(missing):
        return recovered
    return recovered if len(recovered.domains) >= len(combined.domains) else combined


def execute_pipeline(db: Session, run, session, client: OpenAIPatientClient) -> AssessmentOut:
    """Redesigned standard pipeline: 2 OpenAI calls normally, 3 at the absolute
    maximum (one bounded combined correction). All four rubric domains, grounded
    evidence, performance levels, narratives, strengths, growth, and the overall
    impression are preserved - only the CALL COUNT changed.

        CALL 1  combined generation  (evidence + evaluation + overall, all domains)
          ->    deterministic Python validation (grounding / cross-case / levels)
        CALL 2  independent verification (qualitative judgment only)
          ->    deterministic cleanup of rejected evidence
        CALL 3  (only if a domain is still unapproved) ONE combined correction
    """
    session_id = session.id
    prepared, case_reference, rubrics = _prepare(db, session)
    settings = get_settings()
    repo = AssessmentRepository(db)

    # Model accounting (Part 8): record the RUNTIME-resolved model actually used
    # (DB override -> env fallback), not settings.openai_model.
    from app.services import runtime_config_service
    runtime = runtime_config_service.openai_runtime(db)

    # Ensure derived metadata is set (a queued run is created with minimal fields).
    run.case_version = case_reference.get("version", "")
    run.rubric_version = rubric_loader.rubric_version()
    run.model_name = runtime.model
    run.prompt_version = ASSESSMENT_PROMPT_VERSION
    db.commit()

    started = time.monotonic()
    logger.info(
        "assessment_started session_id=%s assessment_id=%s case=%s model=%s",
        session_id, run.id, session.case_id, runtime.model,
    )

    budget = AssessmentCallBudget(
        client, db,
        session_id=session_id, student_id=session.student_id, case_id=session.case_id,
        model=runtime.model, max_calls=settings.assessment_max_openai_calls,
        assessment_id=run.id,
    )

    try:
        # ---- CALL 1: combined structured generation ----------------------
        combined = combined_assessment.generate_combined(
            budget, prepared.text, rubrics, case_reference, stage="assessment_generate",
        )
        # Recover a missing rubric domain with ONE targeted retry BEFORE any
        # validation (the confirmed production failure was a one-domain result).
        combined = _recover_missing_domains(
            budget, combined, prepared.text, rubrics, case_reference, session_id, run.id,
        )
        raw_evidence = _evidence_count(combined.to_extraction())

        # ---- Deterministic validation (NO OpenAI call) -------------------
        extraction = validate_extraction(combined.to_extraction(), prepared, session.case_id)
        evaluations = combined.to_evaluations()
        validate_evaluations(evaluations, extraction, session.case_id)
        logger.info(
            "assessment_validation_completed session_id=%s assessment_id=%s "
            "invalid_evidence_removed=%d",
            session_id, run.id, raw_evidence - _evidence_count(extraction),
        )

        # ---- CALL 2: independent verification ----------------------------
        run.status = "VERIFYING"
        db.commit()
        review = None
        try:
            review = assessment_reviewer.review_assessment(
                budget, prepared.text, case_reference, extraction, evaluations
            )
        except AssessmentCallBudgetExceeded:
            # Fail-closed: never make another provider call; flag for human review.
            logger.warning(
                "assessment_call_budget_exceeded session_id=%s assessment_id=%s calls=%d "
                "stage=verify", session_id, run.id, budget.calls,
            )

        rejected_ids: set[str] = set()
        needs_review = False
        corrected = False

        if review is not None:
            unapproved = [v.rubric_domain for v in review.verdicts if not v.approved]
            # Deterministic cleanup first: drop the evidence the reviewer rejected.
            rejected_ids = {rid for v in review.verdicts for rid in v.rejected_evidence_ids}
            if unapproved:
                if budget.can_call():
                    # ---- CALL 3: ONE bounded combined correction ---------
                    logger.info(
                        "assessment_correction_requested session_id=%s assessment_id=%s domains=%s",
                        session_id, run.id, sorted(unapproved),
                    )
                    try:
                        combined = combined_assessment.generate_combined(
                            budget, prepared.text, rubrics, case_reference,
                            stage="assessment_correction",
                            correction_feedback=_aggregate_issues(review),
                        )
                        extraction = validate_extraction(
                            combined.to_extraction(), prepared, session.case_id
                        )
                        evaluations = combined.to_evaluations()
                        validate_evaluations(evaluations, extraction, session.case_id)
                        # We do NOT re-verify (would exceed the call budget). A
                        # corrected assessment is deterministically validated and
                        # flagged NEEDS_REVIEW for a human glance.
                        rejected_ids = set()
                        corrected = True
                        needs_review = True
                    except AssessmentCallBudgetExceeded:
                        logger.warning(
                            "assessment_call_budget_exceeded session_id=%s assessment_id=%s "
                            "calls=%d stage=correction", session_id, run.id, budget.calls,
                        )
                        needs_review = True
                # else: unapproved but no budget for a correction -> flag below.
                else:
                    needs_review = True
        else:
            # Verification could not run within the call budget.
            needs_review = True

        # The overall impression comes from the verifier on the normal path, and
        # from the corrected combined output when a correction was made (or when
        # verification never ran).
        use_combined_overall = corrected or review is None
        overall_level = combined.overall_level if use_combined_overall else review.overall_level
        overall_summary = (
            combined.overall_summary if use_combined_overall else review.overall_summary
        )
        focus_areas = combined.focus_areas if use_combined_overall else review.focus_areas

        # ---- Persist -----------------------------------------------------
        confirmed_ids: set[str] = set()
        evidence_by_domain = {}
        for domain in extraction.domains:
            domain.evidence_items = [
                item for item in domain.evidence_items if item.evidence_id not in rejected_ids
            ]
            confirmed_ids |= {item.evidence_id for item in domain.evidence_items}
            evidence_by_domain[domain.rubric_domain] = domain

        for evaluation in evaluations:
            evaluation.evidence_ids = [
                eid for eid in evaluation.evidence_ids if eid in confirmed_ids
            ]
            result = repo.add_domain_result(
                run,
                rubric_domain=evaluation.rubric_domain,
                performance_level=evaluation.performance_level,
                summary=evaluation.summary,
                narrative=evaluation.narrative,
                strengths=evaluation.strengths,
                areas_for_growth=evaluation.areas_for_growth,
            )
            domain_evidence = evidence_by_domain.get(evaluation.rubric_domain)
            for item in (domain_evidence.evidence_items if domain_evidence else []):
                turn = prepared.label_to_turn.get(item.turn_label)
                if turn is None:
                    continue
                repo.add_evidence(
                    result,
                    turn_id=turn.id,
                    turn_label=item.turn_label,
                    evidence_type=item.evidence_type,
                    label=item.label,
                    severity=item.severity or None,
                    student_excerpt=item.student_excerpt,
                    patient_excerpt=item.patient_excerpt,
                    explanation=item.explanation,
                    why_it_matters=item.why_it_matters,
                    suggested_alternative=item.suggested_alternative,
                    confidence_level=item.confidence_level,
                    # Reviewer-confirmed only on the normal (independently verified)
                    # path; corrected/unverified evidence is not claimed as confirmed.
                    reviewer_confirmed=not use_combined_overall,
                )

        run.overall_level = overall_level
        run.overall_summary = overall_summary
        run.focus_areas = json.dumps([f.model_dump() for f in focus_areas])
        run.verification_status = "NEEDS_REVIEW" if needs_review else "VERIFIED"
        run.status = "NEEDS_REVIEW" if needs_review else "COMPLETE"
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(
            "assessment_completed session_id=%s assessment_id=%s case=%s status=%s "
            "verification=%s domains=%d openai_calls=%d input_tokens=%d output_tokens=%d "
            "estimated_cost_usd=%.6f duration_ms=%d persistence=saved",
            session_id, run.id, session.case_id, run.status, run.verification_status,
            len(evaluations), budget.calls, budget.input_tokens, budget.output_tokens,
            budget.estimated_cost_usd, int((time.monotonic() - started) * 1000),
        )
        return get_assessment(db, run.id)

    except (AssessmentValidationError, Exception) as exc:
        db.rollback()
        run = repo.get(run.id)
        if run is not None:
            run.status = "FAILED"
            run.error_code = "ASSESSMENT_UNAVAILABLE"
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
        logger.error(
            "assessment_failed session=%s error=%s openai_calls=%d",
            session_id, exc, budget.calls,
        )
        raise AssessmentUnavailableError() from exc


def _run_to_out(db: Session, run) -> AssessmentOut:
    domains: list[DomainResultOut] = []
    for result in run.domain_results:
        evidence = [
            EvidenceOut(
                evidence_id=e.id,
                turn_id=e.turn_id,
                turn_label=e.turn_label,
                evidence_type=e.evidence_type,
                label=e.label,
                severity=e.severity,
                student_excerpt=e.student_excerpt,
                patient_excerpt=e.patient_excerpt,
                explanation=e.explanation,
                why_it_matters=e.why_it_matters,
                suggested_alternative=e.suggested_alternative,
                confidence_level=e.confidence_level,
                reviewer_confirmed=e.reviewer_confirmed,
            )
            for e in result.evidence_items
        ]
        domains.append(
            DomainResultOut(
                rubric_domain=result.rubric_domain,
                performance_level=result.performance_level,
                summary=result.summary,
                narrative=result.narrative,
                strengths=json.loads(result.strengths or "[]"),
                areas_for_growth=json.loads(result.areas_for_growth or "[]"),
                evidence=evidence,
            )
        )
    focus = [FocusAreaOut(**f) for f in json.loads(run.focus_areas or "[]")]
    referral = None
    if getattr(run, "referral_payload", None):
        from app.schemas.referral_assessment_schema import ReferralOut
        referral = ReferralOut.model_validate(json.loads(run.referral_payload))
    return AssessmentOut(
        assessment_id=run.id,
        assessment_mode=getattr(run, "assessment_mode", "standard") or "standard",
        session_id=run.session_id,
        case_id=run.case_id,
        status=run.status,
        overall_level=run.overall_level,
        overall_summary=run.overall_summary,
        focus_areas=focus,
        domains=domains,
        case_version=run.case_version,
        rubric_version=run.rubric_version,
        model_name=run.model_name,
        prompt_version=run.prompt_version,
        verification_status=run.verification_status,
        created_at=run.created_at,
        completed_at=run.completed_at,
        referral=referral,
    )


def get_assessment(db: Session, assessment_id: str) -> AssessmentOut:
    run = AssessmentRepository(db).get(assessment_id)
    if run is None:
        raise AssessmentNotFoundError(assessment_id)
    return _run_to_out(db, run)


def get_latest_for_session(db: Session, session_id: str) -> AssessmentOut:
    run = AssessmentRepository(db).latest_for_session(session_id)
    if run is None:
        raise AssessmentNotFoundError(f"session {session_id}")
    return _run_to_out(db, run)


def get_assessment_transcript(db: Session, assessment_id: str) -> list[AssessmentTurnOut]:
    run = AssessmentRepository(db).get(assessment_id)
    if run is None:
        raise AssessmentNotFoundError(assessment_id)
    turns = TranscriptRepository(db).list_turns(run.session_id)
    markers_by_turn: dict[str, list[TranscriptMarkerOut]] = {}
    for result in run.domain_results:
        for e in result.evidence_items:
            markers_by_turn.setdefault(e.turn_id, []).append(
                TranscriptMarkerOut(
                    evidence_id=e.id,
                    rubric_domain=result.rubric_domain,
                    evidence_type=e.evidence_type,
                    label=e.label,
                    severity=e.severity,
                    confidence_level=e.confidence_level,
                    reviewer_confirmed=e.reviewer_confirmed,
                    explanation=e.explanation,
                    why_it_matters=e.why_it_matters,
                    suggested_alternative=e.suggested_alternative,
                )
            )
    return [
        AssessmentTurnOut(
            turn_id=t.id,
            turn_label=f"turn_{t.turn_index:02d}",
            sender=t.role,
            text=t.content,
            timestamp=t.created_at,
            markers=markers_by_turn.get(t.id, []),
        )
        for t in turns
    ]
