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


def get_assessment(db: Session, assessment_id: str):
    return standard_assessment_service.get_assessment(db, assessment_id)


def get_latest_for_session(db: Session, session_id: str):
    return standard_assessment_service.get_latest_for_session(db, session_id)


def get_assessment_transcript(db: Session, assessment_id: str):
    return standard_assessment_service.get_assessment_transcript(db, assessment_id)
