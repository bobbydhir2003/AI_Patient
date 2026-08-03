import logging

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.assessment import assessment_service
from app.database.connection import get_db
from app.core.rate_limit import rate_limit
from app.dependencies.auth import (
    get_current_user,
    require_assessment_access,
    require_session_access,
)
from app.models import AssessmentRun, InterviewSession
from app.patient_engine.openai_client import OpenAIPatientClient, get_openai_client
from app.schemas.assessment_schema import (
    AssessmentOut,
    AssessmentStatusOut,
    AssessmentTurnOut,
    RubricOut,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["assessments"])

_assessment_rate_limit = rate_limit("assessment", lambda s: s.assessment_rate_limit)


@router.get("/sessions/{session_id}/assessment/status", response_model=AssessmentStatusOut)
def get_assessment_status(
    session: InterviewSession = Depends(require_session_access),
    db: Session = Depends(get_db),
):
    return assessment_service.get_assessment_status(db, session.id)


@router.post(
    "/sessions/{session_id}/assessment",
    response_model=AssessmentOut | AssessmentStatusOut,
    status_code=202,
    dependencies=[Depends(_assessment_rate_limit)],
)
def create_assessment(
    response: Response,
    retry: bool = False,
    session: InterviewSession = Depends(require_session_access),
    db: Session = Depends(get_db),
    client: OpenAIPatientClient = Depends(get_openai_client),
) -> AssessmentOut | AssessmentStatusOut:
    session_id = session.id
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

    from app.core.config import get_settings

    if get_settings().assessment_queue_enabled:
        # B7: enqueue and return immediately; the background worker executes it.
        queued = assessment_service.enqueue_assessment(db, session_id, retry=retry)
        try:
            from app.core.telemetry import get_telemetry

            get_telemetry().live.set_status(session_id, "ASSESSMENT_PENDING")
        except Exception:
            pass
        response.status_code = status.HTTP_202_ACCEPTED
        return queued

    # Fallback (queue disabled): run synchronously in-request.
    result = assessment_service.generate_assessment(db, session_id, client, retry=retry)
    response.status_code = status.HTTP_201_CREATED
    return result


@router.get("/sessions/{session_id}/assessment", response_model=AssessmentOut)
def latest_assessment(
    session: InterviewSession = Depends(require_session_access),
    db: Session = Depends(get_db),
) -> AssessmentOut:
    return assessment_service.get_latest_for_session(db, session.id)


@router.get("/assessments/{assessment_id}", response_model=AssessmentOut)
def get_assessment(
    run: AssessmentRun = Depends(require_assessment_access),
    db: Session = Depends(get_db),
) -> AssessmentOut:
    return assessment_service.get_assessment(db, run.id)


@router.get("/assessments/{assessment_id}/transcript", response_model=list[AssessmentTurnOut])
def assessment_transcript(
    run: AssessmentRun = Depends(require_assessment_access),
    db: Session = Depends(get_db),
) -> list[AssessmentTurnOut]:
    return assessment_service.get_assessment_transcript(db, run.id)


@router.get("/rubrics", response_model=list[RubricOut], dependencies=[Depends(get_current_user)])
def rubrics() -> list[RubricOut]:
    return assessment_service.list_rubrics()
