"""Pre/Post experience survey endpoints.

All routes are session-scoped and go through ``require_session_access`` so:
- the caller must be authenticated, and
- a student may only reach a session owned by their own linked profile (an
  unowned/nonexistent session id gets the same 404 - no existence leak).

The REDCap ``record_id`` (the student's NUID) is resolved server-side from the
session's owner inside survey_service; the client never supplies it and it never
appears in the URL. The REDCap token is never returned.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.dependencies.auth import require_session_access
from app.models import InterviewSession
from app.schemas.survey_schema import (
    PostSurveyIn,
    PreSurveyIn,
    SurveyStatusOut,
    SurveySubmitResult,
)
from app.services import survey_service

# Shares the /interviews prefix with the interviews router (FastAPI allows
# multiple routers under one prefix); grouped separately for cohesion.
router = APIRouter(prefix="/interviews", tags=["surveys"])


@router.get("/{session_id}/surveys/status", response_model=SurveyStatusOut)
def survey_status(
    session: InterviewSession = Depends(require_session_access),
    db: Session = Depends(get_db),
) -> SurveyStatusOut:
    return survey_service.get_survey_status(db, session)


@router.post("/{session_id}/surveys/pre", response_model=SurveySubmitResult)
def submit_pre_survey(
    payload: PreSurveyIn,
    session: InterviewSession = Depends(require_session_access),
    db: Session = Depends(get_db),
) -> SurveySubmitResult:
    return survey_service.submit_pre(db, session, payload)


@router.post("/{session_id}/surveys/post", response_model=SurveySubmitResult)
def submit_post_survey(
    payload: PostSurveyIn,
    session: InterviewSession = Depends(require_session_access),
    db: Session = Depends(get_db),
) -> SurveySubmitResult:
    return survey_service.submit_post(db, session, payload)
