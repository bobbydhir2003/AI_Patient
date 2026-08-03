"""Orchestrates the three-stage AI assessment pipeline.

All educational judgment comes from the AI stages; this module only sequences
calls, validates structure/grounding, and persists results. If generation
fails, the run is marked FAILED and NO feedback is fabricated.
"""
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.assessment import (
    assessment_reviewer,
    evidence_extractor,
    rubric_evaluator,
    rubric_loader,
    transcript_preparer,
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


def execute_pipeline(db: Session, run, session, client: OpenAIPatientClient) -> AssessmentOut:
    session_id = session.id
    prepared, case_reference, rubrics = _prepare(db, session)
    settings = get_settings()
    repo = AssessmentRepository(db)
    # Ensure derived metadata is set (a queued run is created with minimal fields).
    run.case_version = case_reference.get("version", "")
    run.rubric_version = rubric_loader.rubric_version()
    run.model_name = settings.openai_model
    run.prompt_version = ASSESSMENT_PROMPT_VERSION
    db.commit()

    try:
        # ---- Stage 1: evidence extraction --------------------------------
        extraction = evidence_extractor.extract_evidence(
            client, prepared.text, rubrics, case_reference
        )
        extraction = validate_extraction(extraction, prepared, session.case_id)
        evidence_by_domain = {d.rubric_domain: d for d in extraction.domains}

        # ---- Stage 2: rubric evaluation (one call per domain) ------------
        evaluations: list[DomainEvaluation] = []
        for rubric in rubrics:
            evaluations.append(
                rubric_evaluator.evaluate_domain(
                    client, prepared.text, rubric, case_reference,
                    evidence_by_domain.get(rubric["domain"]),
                )
            )
        validate_evaluations(evaluations, extraction, session.case_id)

        # ---- Stage 3: AI verification -------------------------------------
        run.status = "VERIFYING"
        db.commit()
        review = assessment_reviewer.review_assessment(
            client, prepared.text, case_reference, extraction, evaluations
        )

        # Regenerate ONLY rejected domains, once.
        rejected_ids = {rid for v in review.verdicts for rid in v.rejected_evidence_ids}
        needs_review = False
        verdicts_by_domain = {v.rubric_domain: v for v in review.verdicts}
        regenerated = False
        for i, evaluation in enumerate(evaluations):
            verdict = verdicts_by_domain.get(evaluation.rubric_domain)
            if verdict is not None and not verdict.approved:
                rubric = next(r for r in rubrics if r["domain"] == evaluation.rubric_domain)
                evaluations[i] = rubric_evaluator.evaluate_domain(
                    client, prepared.text, rubric, case_reference,
                    evidence_by_domain.get(rubric["domain"]),
                    reviewer_feedback=verdict.issues,
                )
                regenerated = True
        if regenerated:
            validate_evaluations(evaluations, extraction, session.case_id)
            recheck = assessment_reviewer.review_assessment(
                client, prepared.text, case_reference, extraction, evaluations
            )
            rejected_ids |= {rid for v in recheck.verdicts for rid in v.rejected_evidence_ids}
            needs_review = any(not v.approved for v in recheck.verdicts)
            review = recheck

        # ---- Persist -------------------------------------------------------
        confirmed_ids: set[str] = set()
        for domain in extraction.domains:
            domain.evidence_items = [
                item for item in domain.evidence_items if item.evidence_id not in rejected_ids
            ]
            confirmed_ids |= {item.evidence_id for item in domain.evidence_items}

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
                    reviewer_confirmed=True,
                )

        run.overall_level = review.overall_level
        run.overall_summary = review.overall_summary
        run.focus_areas = json.dumps([f.model_dump() for f in review.focus_areas])
        run.verification_status = "NEEDS_REVIEW" if needs_review else "VERIFIED"
        run.status = "NEEDS_REVIEW" if needs_review else "COMPLETE"
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(
            "assessment_completed session_id=%s assessment_id=%s case=%s status=%s "
            "verification=%s domains=%d persistence=saved",
            session_id, run.id, session.case_id, run.status,
            run.verification_status, len(evaluations),
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
        logger.error("assessment_failed session=%s error=%s", session_id, exc)
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
