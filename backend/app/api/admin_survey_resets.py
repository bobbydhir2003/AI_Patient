"""Admin "Survey Resets" API: survey state for every real student plus a bulk
reset. Every route requires an admin. Single-student resets reuse
POST /admin/student-data/{student_id}/survey-reset (no competing endpoint)."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.dependencies.auth import require_admin
from app.models import User
from app.schemas.survey_resets import BulkSurveyResetIn, BulkSurveyResetOut, SurveyResetListOut
from app.services import survey_reset_service

router = APIRouter(
    prefix="/admin/survey-resets",
    tags=["admin-survey-resets"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=SurveyResetListOut)
def list_survey_resets(
    search: str = Query("", max_length=120),
    status: str = Query("all", pattern="^(all|completed|in_progress|not_started|reset_before)$"),
    case_id: str = Query("", max_length=50),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
) -> SurveyResetListOut:
    return survey_reset_service.list_survey_resets(
        db, search=search, status=status, case_id=case_id, page=page, page_size=page_size
    )


@router.post("/bulk", response_model=BulkSurveyResetOut)
def bulk_reset(
    payload: BulkSurveyResetIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> BulkSurveyResetOut:
    return survey_reset_service.bulk_reset(db, admin, scope=payload.scope)
