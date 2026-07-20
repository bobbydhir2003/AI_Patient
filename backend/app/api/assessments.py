from fastapi import APIRouter, Depends, BackgroundTasks, Response, status
from sqlalchemy.orm import Session
import logging

from app.assessment import assessment_service
from app.database.connection import get_db
from app.patient_engine.openai_client import OpenAIPatientClient, get_openai_client
from app.schemas.assessment_schema import AssessmentOut, AssessmentTurnOut, RubricOut, AssessmentStatusOut

logger = logging.getLogger(__name__)
router = APIRouter(tags=["assessments"])

@router.get("/sessions/{session_id}/assessment/status", response_model=AssessmentStatusOut)
def get_assessment_status(session_id: str, db: Session = Depends(get_db)):
    return assessment_service.get_assessment_status(db, session_id)


@router.post("/sessions/{session_id}/assessment", response_model=AssessmentOut | AssessmentStatusOut, status_code=202)
def create_assessment(
    session_id: str,
    response: Response,
    retry: bool = False,
    db: Session = Depends(get_db),
    client: OpenAIPatientClient = Depends(get_openai_client),
) -> AssessmentOut | AssessmentStatusOut:
    status_info = assessment_service.get_assessment_status(db, session_id)
    
    if status_info["status"] == "completed":
        response.status_code = status.HTTP_200_OK
        return assessment_service.get_latest_for_session(db, session_id)
        
    if status_info["status"] in ("pending", "processing", "verifying"):
        response.status_code = status.HTTP_202_ACCEPTED
        return status_info
        
    if status_info["status"] == "failed" and not retry:
        response.status_code = status.HTTP_202_ACCEPTED
        return status_info
        
    result = assessment_service.generate_assessment(db, session_id, client, retry=retry)
    response.status_code = status.HTTP_201_CREATED
    return result


@router.get("/sessions/{session_id}/assessment", response_model=AssessmentOut)
def latest_assessment(session_id: str, db: Session = Depends(get_db)) -> AssessmentOut:
    return assessment_service.get_latest_for_session(db, session_id)


@router.get("/assessments/{assessment_id}", response_model=AssessmentOut)
def get_assessment(assessment_id: str, db: Session = Depends(get_db)) -> AssessmentOut:
    return assessment_service.get_assessment(db, assessment_id)


@router.get("/assessments/{assessment_id}/transcript", response_model=list[AssessmentTurnOut])
def assessment_transcript(assessment_id: str, db: Session = Depends(get_db)) -> list[AssessmentTurnOut]:
    return assessment_service.get_assessment_transcript(db, assessment_id)


@router.get("/rubrics", response_model=list[RubricOut])
def rubrics() -> list[RubricOut]:
    return assessment_service.list_rubrics()
