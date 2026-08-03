"""Assessment-mode dispatcher.

Standard cases keep the original four-rubric pipeline. Referral cases use the
separate universal seven-domain AI referral pipeline. Routing is based only on
session metadata, never individual case ids or patient names.
"""
from sqlalchemy.orm import Session

from app.assessment import referral_assessment_service, standard_assessment_service
from app.core.exceptions import SessionNotFoundError
from app.patient_engine.openai_client import OpenAIPatientClient
from app.repositories.session_repository import SessionRepository


from app.assessment.assessment_repository import AssessmentRepository

def list_rubrics():
    return standard_assessment_service.list_rubrics()


def get_assessment_status(db: Session, session_id: str) -> dict:
    repo = AssessmentRepository(db)
    existing = repo.latest_for_session(session_id)
    if not existing:
        return {
            "session_id": session_id,
            "status": "not_started",
            "stage": "saving_transcript",
        }
    
    status_map = {
        "PENDING": ("pending", "preparing"),
        "PROCESSING": ("processing", "evaluating"),
        "VERIFYING": ("verifying", "building_report"),
        "COMPLETE": ("completed", "completed"),
        "NEEDS_REVIEW": ("completed", "completed"),
        "FAILED": ("failed", "failed")
    }
    st, stage = status_map.get(existing.status, ("processing", "evaluating"))
    return {
        "session_id": session_id,
        "assessment_id": existing.id,
        "status": st,
        "stage": stage,
        "assessment_mode": existing.assessment_mode,
        "error_code": existing.error_code,
    }


def generate_assessment(db: Session, session_id: str, client: OpenAIPatientClient, retry: bool = False):
    session = SessionRepository(db).get(session_id)
    if session is None:
        raise SessionNotFoundError(session_id)
    if session.case_category == "referral":
        return referral_assessment_service.generate(db, session_id, client, retry=retry)
    return standard_assessment_service.generate_assessment(db, session_id, client, retry=retry)


def enqueue_assessment(db: Session, session_id: str, retry: bool = False) -> dict:
    """B7: create a QUEUED (PENDING) assessment run and return immediately.

    A durable, database-backed queue: the background worker picks the run up and
    executes it, so the HTTP request returns in milliseconds instead of blocking
    30-120s on OpenAI. Idempotent - an existing active/complete run is reused, and
    a partial unique index makes concurrent double-submits collapse to one run.
    """
    from sqlalchemy.exc import IntegrityError

    from app.assessment.assessment_repository import AssessmentRepository
    from app.core.constants import MIN_STUDENT_TURNS_FOR_ASSESSMENT
    from app.core.exceptions import (
        AssessmentNotPossibleError,
        SessionNotCompletedError,
    )
    from app.repositories.transcript_repository import TranscriptRepository

    session = SessionRepository(db).get(session_id)
    if session is None:
        raise SessionNotFoundError(session_id)
    if session.status != "completed" or not session.locked:
        raise SessionNotCompletedError(session_id)

    repo = AssessmentRepository(db)
    existing = repo.latest_for_session(session_id)
    if existing is not None:
        if existing.status in ("COMPLETE", "NEEDS_REVIEW"):
            return get_assessment_status(db, session_id)
        if existing.status in ("PENDING", "PROCESSING", "VERIFYING"):
            return get_assessment_status(db, session_id)  # already queued/running - no duplicate
        if existing.status == "FAILED" and not retry:
            return get_assessment_status(db, session_id)

    # Fast, cheap validation so a plainly un-assessable session never queues.
    if TranscriptRepository(db).count_nonempty_by_role(session_id, "student") < MIN_STUDENT_TURNS_FOR_ASSESSMENT:
        raise AssessmentNotPossibleError(
            "The interview contains no student questions, so there is nothing to assess."
        )

    mode = "advanced_referral" if session.case_category == "referral" else "standard"
    try:
        repo.create_run(session_id=session_id, case_id=session.case_id,
                        assessment_mode=mode, status="PENDING")
        db.commit()
    except IntegrityError:  # concurrent enqueue won the race - reuse its run
        db.rollback()
    return get_assessment_status(db, session_id)


def execute_run(db: Session, run, client: OpenAIPatientClient):
    """Run the pipeline for an already-created run (called by the worker)."""
    session = SessionRepository(db).get(run.session_id)
    if session is not None and session.case_category == "referral":
        return referral_assessment_service.execute_existing(db, run, client)
    return standard_assessment_service.execute_existing(db, run, client)


def queue_stats(db: Session) -> dict:
    """Cheap, indexed aggregate for the dashboard (no heavy scans)."""
    from datetime import datetime, timezone

    from sqlalchemy import func, select

    from app.models import AssessmentRun

    rows = db.execute(
        select(AssessmentRun.status, func.count())
        .where(AssessmentRun.status.in_(["PENDING", "PROCESSING", "VERIFYING"]))
        .group_by(AssessmentRun.status)
    ).all()
    counts = {s: c for s, c in rows}
    pending = counts.get("PENDING", 0)
    processing = counts.get("PROCESSING", 0) + counts.get("VERIFYING", 0)

    oldest = db.execute(
        select(func.min(AssessmentRun.created_at)).where(AssessmentRun.status == "PENDING")
    ).scalar()
    oldest_wait_s = None
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        oldest_wait_s = int((datetime.now(timezone.utc) - oldest).total_seconds())

    return {"pending": pending, "processing": processing, "oldest_wait_seconds": oldest_wait_s}


def get_assessment(db: Session, assessment_id: str):
    return standard_assessment_service.get_assessment(db, assessment_id)


def get_latest_for_session(db: Session, session_id: str):
    return standard_assessment_service.get_latest_for_session(db, session_id)


def get_assessment_transcript(db: Session, assessment_id: str):
    return standard_assessment_service.get_assessment_transcript(db, assessment_id)
