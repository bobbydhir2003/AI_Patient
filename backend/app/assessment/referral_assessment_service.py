"""AI-only advanced referral assessment pipeline.

This service is selected by case_category, never by a case id.  The universal
seven-domain rubric is constant; patient-specific grounding comes only from the
protected reference loaded for the selected session.
"""
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.assessment import rubric_loader, transcript_preparer
from app.assessment.assessment_repository import AssessmentRepository
from app.core.config import get_settings
from app.core.constants import MIN_STUDENT_TURNS_FOR_ASSESSMENT, SESSION_STATUS_COMPLETED
from app.core.exceptions import (
    AssessmentNotPossibleError, AssessmentUnavailableError,
    SessionNotCompletedError, SessionNotFoundError,
)
from app.core.logging import get_logger
from app.patient_engine.openai_client import OpenAIPatientClient
from app.repositories.session_repository import SessionRepository
from app.repositories.transcript_repository import TranscriptRepository
from app.schemas.assessment_schema import AssessmentOut
from app.schemas.referral_assessment_schema import (
    REFERRAL_EVALUATION_JSON_SCHEMA, REFERRAL_EXTRACTION_JSON_SCHEMA,
    REFERRAL_REVIEW_JSON_SCHEMA, ReferralDomainEvaluation, ReferralExtraction,
    ReferralReview,
)

logger = get_logger(__name__)
PROMPT_VERSION = "referral-1.0"


def _messages(system: str, payload: dict) -> list[dict]:
    return [
        {"role": "developer", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _extract(caller, transcript, rubric, context) -> ReferralExtraction:
    prompt = """You are the evidence-extraction stage of a PT referral assessment.
Your goal is to separate the intended clinical situation (from protected case context) from what the student actually did (from the transcript).

PROTECTED CASE CONTEXT
- Not visible to the student.
- Used only to understand clinical relevance and acceptable alternatives.
- Never use this as evidence of student performance.

PATIENT DISCLOSURES
- Information actually spoken by the patient in the transcript.
- May establish that a concern appeared.
- Does not prove that the student recognized or handled it.

STUDENT ACTIONS
- Questions and statements actually spoken by the student.
- The ONLY valid basis for judging student performance.

Extract excerpts accurately. Determine `assessability` for each domain:
- `assessable`: The transcript contains enough relevant student behavior to evaluate performance.
- `insufficient_evidence`: The domain became relevant, but the student interaction was too limited to make a reliable judgment.
- `not_assessed`: The situation needed to evaluate the domain never occurred or was never reached in the conversation.
"""
    settings = get_settings()
    data = caller.generate_structured(
        _messages(prompt, {"transcript": transcript, "rubric": rubric, "protected_context": context}),
        REFERRAL_EXTRACTION_JSON_SCHEMA, "referral_evidence_extraction",
        stage="assessment_generate",
        max_output_tokens=settings.openai_referral_extraction_max_output_tokens,
        allow_truncation_retry=True,
    )
    return ReferralExtraction.model_validate(data)


def _evaluate(caller, transcript, domain, context, student_evidence, patient_evidence, missed_evidence, assessability, reviewer_feedback=None):
    prompt = """You are the domain-evaluation stage of an advanced referral assessment.

PROTECTED CASE CONTEXT
- Not visible to the student.
- Used only to understand clinical relevance.
- Never use this as evidence of student performance.

INSTRUCTIONS:
1. If assessability = 'not_assessed', level must be 'Not Assessed'.
2. If assessability = 'insufficient_evidence', level must be 'Insufficient Evidence'.
3. If assessability = 'assessable', evaluate ONLY from verified student evidence.
4. Patient disclosures alone DO NOT support a positive student performance.
5. Do not penalize absence without a fair opportunity. If the transcript ended early, it is usually Insufficient Evidence, not Needs Attention.
6. A level of Strong or Appropriate MUST cite at least one valid student turn excerpt that directly supports the domain judgment.
"""
    payload = {
        "transcript": transcript, "domain": domain, "protected_context": context,
        "assessability": assessability,
        "student_evidence": [e.model_dump() for e in student_evidence],
        "patient_context_evidence": [e.model_dump() for e in patient_evidence],
        "missed_opportunity_evidence": [e.model_dump() for e in missed_evidence],
        "reviewer_feedback": reviewer_feedback or [],
    }
    settings = get_settings()
    stage = "assessment_correction" if reviewer_feedback else "assessment_generate"
    data = caller.generate_structured(
        _messages(prompt, payload), REFERRAL_EVALUATION_JSON_SCHEMA,
        f"referral_domain_{domain['id']}",
        stage=stage,
        max_output_tokens=settings.openai_referral_domain_max_output_tokens,
        allow_truncation_retry=True,
    )
    return ReferralDomainEvaluation.model_validate(data)


def _review(caller, transcript, rubric, context, extraction, evaluations):
    prompt = """You are the independent verification stage.

INSTRUCTIONS:
1. Verify every judgment is supported by real transcript evidence.
2. Positive levels (Strong, Appropriate) MUST cite at least one verified student excerpt.
3. Reject any positive domain if its evidence is patient-only or zero.
4. Protected context may only guide interpretation; it cannot be cited as transcript evidence.
5. If you reject a domain, we will attempt regeneration.
6. Determine `overall_assessability` (sufficient | limited | insufficient). If insufficient, force `overall_level` to 'Insufficient Evidence'.
"""
    settings = get_settings()
    data = caller.generate_structured(
        _messages(prompt, {
            "transcript": transcript, "rubric": rubric, "protected_context": context,
            "extraction": extraction.model_dump(),
            "evaluations": [e.model_dump() for e in evaluations],
        }),
        REFERRAL_REVIEW_JSON_SCHEMA, "referral_assessment_review",
        stage="assessment_verify",
        max_output_tokens=settings.openai_referral_review_max_output_tokens,
        allow_truncation_retry=True,
    )
    return ReferralReview.model_validate(data)


def _validate_extraction(extraction, prepared, domain_ids):
    valid_labels = set(prepared.label_to_turn)
    valid_ids = set(domain_ids)
    clean_domains = []
    
    for d in extraction.domain_evidence:
        if d.domain_id not in valid_ids:
            continue
            
        clean_student = []
        for e in d.student_evidence:
            if e.turn_label in valid_labels:
                turn = prepared.label_to_turn[e.turn_label]
                if turn.role == "student" and e.excerpt.strip().lower() in turn.content.strip().lower():
                    clean_student.append(e)
        d.student_evidence = clean_student
        
        clean_patient = []
        for e in d.patient_context_evidence:
            if e.turn_label in valid_labels:
                turn = prepared.label_to_turn[e.turn_label]
                if turn.role == "patient" and e.excerpt.strip().lower() in turn.content.strip().lower():
                    clean_patient.append(e)
        d.patient_context_evidence = clean_patient

        clean_missed = []
        for e in d.missed_opportunity_evidence:
            if e.turn_label in valid_labels:
                turn = prepared.label_to_turn[e.turn_label]
                if e.excerpt.strip().lower() in turn.content.strip().lower():
                    clean_missed.append(e)
        d.missed_opportunity_evidence = clean_missed

        if not d.student_evidence and d.assessability == "assessable":
            d.assessability = "insufficient_evidence"

        clean_domains.append(d)

    extraction.domain_evidence = clean_domains
    return extraction


def execute_existing(db: Session, run, client: OpenAIPatientClient) -> AssessmentOut:
    """Background-worker entry: run the referral pipeline on an existing run."""
    return generate(db, run.session_id, client, retry=True, run=run)


def generate(db: Session, session_id: str, client: OpenAIPatientClient,
             retry: bool = False, run=None) -> AssessmentOut:
    session = SessionRepository(db).get(session_id)
    if session is None:
        raise SessionNotFoundError(session_id)
    if session.status != SESSION_STATUS_COMPLETED or not session.locked:
        raise SessionNotCompletedError(session_id)

    repo = AssessmentRepository(db)
    if run is None:
        existing = repo.latest_for_session(session_id)
        if existing is not None and existing.status in ("COMPLETE", "NEEDS_REVIEW"):
            from app.assessment.standard_assessment_service import _run_to_out
            return _run_to_out(db, existing)

    turns = TranscriptRepository(db).list_turns(session_id)
    prepared = transcript_preparer.prepare_transcript(turns)
    if prepared.student_turn_count < MIN_STUDENT_TURNS_FOR_ASSESSMENT:
        raise AssessmentNotPossibleError("The interview contains no student questions, so there is nothing to assess.")

    rubric = rubric_loader.load_referral_rubric()
    case_reference = rubric_loader.load_case_reference(session.case_id)
    context = case_reference.get("referral_assessment_context")
    if not context:
        raise AssessmentNotPossibleError("Protected referral assessment context is missing for this case.")

    settings = get_settings()
    # Model accounting: record the runtime-resolved model actually used.
    from app.services import runtime_config_service
    runtime = runtime_config_service.openai_runtime(db)
    if run is None:
        run = repo.create_run(
            session_id=session_id, case_id=session.case_id,
            assessment_mode="advanced_referral", case_version=case_reference.get("version", ""),
            rubric_version=rubric.get("version", "1.0"), model_name=runtime.model,
            prompt_version=PROMPT_VERSION, status="PROCESSING",
        )
        db.commit()
    else:  # worker path: promote the queued run and fill derived metadata
        run.status = "PROCESSING"
        run.case_version = case_reference.get("version", "")
        run.rubric_version = rubric.get("version", "1.0")
        run.model_name = runtime.model
        run.prompt_version = PROMPT_VERSION
        db.commit()

    # Referral shares the usage-accounting + call-counting infrastructure so its
    # OpenAI spend is recorded on the dashboard too. It legitimately fans out over
    # seven domains, so it gets its own (higher) safety ceiling - never the 3-call
    # standard cap.
    from app.assessment.assessment_call_budget import AssessmentCallBudget
    budget = AssessmentCallBudget(
        client, db,
        session_id=session_id, student_id=session.student_id, case_id=session.case_id,
        model=runtime.model, max_calls=settings.referral_assessment_max_openai_calls,
        assessment_id=run.id,
    )

    try:
        domain_defs = rubric["domains"]
        domain_ids = [d["id"] for d in domain_defs]
        extraction = _validate_extraction(
            _extract(budget, prepared.text, rubric, context), prepared, domain_ids
        )
        domain_evidence_map = {d.domain_id: d for d in extraction.domain_evidence}
        evaluations = []
        for d in domain_defs:
            de = domain_evidence_map.get(d["id"])
            if de:
                evaluations.append(_evaluate(
                    budget, prepared.text, d, context, de.student_evidence,
                    de.patient_context_evidence, de.missed_opportunity_evidence, de.assessability
                ))
            else:
                evaluations.append(_evaluate(budget, prepared.text, d, context, [], [], [], "not_assessed"))

        run.status = "VERIFYING"
        db.commit()
        review = _review(budget, prepared.text, rubric, context, extraction, evaluations)

        review_map = {r.domain_id: r for r in review.domain_reviews}
        regenerated = False
        for i, evaluation in enumerate(evaluations):
            verdict = review_map.get(evaluation.domain_id)
            if verdict and verdict.status == "rejected":
                domain = next(d for d in domain_defs if d["id"] == evaluation.domain_id)
                de = domain_evidence_map.get(evaluation.domain_id)
                evaluations[i] = _evaluate(
                    budget, prepared.text, domain, context,
                    de.student_evidence if de else [],
                    de.patient_context_evidence if de else [],
                    de.missed_opportunity_evidence if de else [],
                    de.assessability if de else "not_assessed",
                    reviewer_feedback=[verdict.reason]
                )
                regenerated = True
        if regenerated:
            review = _review(budget, prepared.text, rubric, context, extraction, evaluations)

        # Cross-result consistency checks
        for ev in evaluations:
            de = domain_evidence_map.get(ev.domain_id)
            has_student = de and len(de.student_evidence) > 0
            if ev.level in ("Strong", "Appropriate") and not has_student:
                ev.level = "Insufficient Evidence"

        if review.overall_assessability == "insufficient":
            review.overall_level = "Insufficient Evidence"

        final_review = {r.domain_id: r for r in review.domain_reviews}
        domain_payloads = []
        all_evidence_payloads = []
        timeline = []
        for domain in domain_defs:
            ev = next(e for e in evaluations if e.domain_id == domain["id"])
            reviewer_status = final_review.get(domain["id"]).status if final_review.get(domain["id"]) else "accepted"
            if reviewer_status == "rejected":
                ev.level = "Needs Attention"
            result = repo.add_domain_result(
                run, rubric_domain=domain["id"], performance_level=ev.level,
                summary=ev.summary, narrative=ev.narrative, strengths=ev.strengths,
                areas_for_growth=ev.growth_areas,
            )
            evidence_payloads = []
            de = domain_evidence_map.get(domain["id"])
            if de:
                def _add_evidence_record(item, is_student):
                    turn = prepared.label_to_turn.get(item.turn_label)
                    if not turn: return
                    ev_type = "strength" if ev.level in ("Strong", "Appropriate") else "concern"
                    if not is_student:
                        ev_type = "neutral"
                    stored = repo.add_evidence(
                        result, turn_id=turn.id, turn_label=item.turn_label,
                        evidence_type=ev_type, label=domain["title"], severity=None,
                        student_excerpt=item.excerpt if is_student else "",
                        patient_excerpt="" if is_student else item.excerpt,
                        explanation=de.reason, why_it_matters=de.reason,
                        suggested_alternative=ev.stronger_approach,
                        confidence_level="medium", reviewer_confirmed=reviewer_status == "accepted",
                    )
                    payload = {
                        "evidenceId": stored.id, "turnId": turn.id, "turnLabel": item.turn_label,
                        "turnIndex": turn.turn_index, "speaker": turn.role,
                        "evidenceType": ev_type, 
                        "studentExcerpt": item.excerpt if is_student else "",
                        "patientContextExcerpt": "" if is_student else item.excerpt,
                        "whyItMatters": de.reason, "confidence": "medium",
                        "reviewerConfirmed": reviewer_status == "accepted",
                        "domainId": domain["id"], "domainTitle": domain["title"],
                    }
                    evidence_payloads.append(payload)
                    all_evidence_payloads.append(payload)
                    timeline.append({
                        "turnId": turn.id, "turnLabel": item.turn_label, "turnIndex": turn.turn_index,
                        "label": domain["title"], "description": de.reason,
                        "excerpt": item.excerpt,
                        "speaker": turn.role, "evidenceType": ev_type,
                    })

                for item in de.student_evidence:
                    _add_evidence_record(item, True)
                for item in de.patient_context_evidence:
                    _add_evidence_record(item, False)
                for item in de.missed_opportunity_evidence:
                    _add_evidence_record(item, True)
            domain_payloads.append({
                "domainId": domain["id"], "title": domain["title"],
                "definition": domain["definition"], "level": ev.level,
                "summary": ev.summary, "narrative": ev.narrative,
                "strengths": ev.strengths, "growthAreas": ev.growth_areas,
                "strongerApproach": ev.stronger_approach, "assessability": ev.assessability,
                "reviewerStatus": reviewer_status, "evidence": evidence_payloads,
            })

        payload = {
            "status": extraction.referral_status,
            "activationReason": extraction.activation_reason,
            "overallLevel": review.overall_level,
            "overallSummary": review.overall_summary,
            "keyStrengths": review.key_strengths,
            "growthOpportunities": review.growth_opportunities,
            "priorityFocusAreas": review.priority_focus_areas,
            "verificationStatus": review.verification_status,
            "domains": domain_payloads,
            "timeline": sorted(timeline, key=lambda x: x["turnIndex"]),
            "keyMoments": all_evidence_payloads[:12],
        }
        run.referral_payload = json.dumps(payload)
        run.overall_level = review.overall_level
        run.overall_summary = review.overall_summary
        run.verification_status = review.verification_status.upper()
        run.status = "NEEDS_REVIEW" if review.verification_status != "verified" else "COMPLETE"
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        from app.assessment.standard_assessment_service import _run_to_out
        return _run_to_out(db, run)
    except Exception as exc:
        db.rollback()
        failed = repo.get(run.id)
        if failed:
            failed.status = "FAILED"
            failed.error_code = "ASSESSMENT_UNAVAILABLE"
            failed.completed_at = datetime.now(timezone.utc)
            db.commit()
        logger.exception("referral_assessment_failed session_id=%s", session_id)
        raise AssessmentUnavailableError() from exc
