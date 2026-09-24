"""Admin "Student Data" API: a student-centred view (list -> one student's full
history) plus the admin survey reset. Every route requires an admin; the student
is always identified by the route, never by a request body."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.dependencies.auth import require_admin
from app.models import User
from app.schemas.student_data import (
    PaginatedStudentData,
    StudentDataDetailOut,
    SurveyResetIn,
    SurveyResetOut,
)
from app.services import student_data_service

router = APIRouter(
    prefix="/admin/student-data",
    tags=["admin-student-data"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=PaginatedStudentData)
def list_students(
    search: str = Query("", max_length=120),
    status: str = Query("all", pattern="^(all|active|inactive)$"),
    sort: str = Query("name", pattern="^(name|recent)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PaginatedStudentData:
    return student_data_service.list_students(
        db, search=search, status=status, sort=sort, page=page, page_size=page_size
    )


@router.get("/{student_id}", response_model=StudentDataDetailOut)
def get_student_data(student_id: str, db: Session = Depends(get_db)) -> StudentDataDetailOut:
    return student_data_service.get_student_data(db, student_id)


@router.post("/{student_id}/survey-reset", response_model=SurveyResetOut)
def reset_survey(
    student_id: str,
    payload: SurveyResetIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> SurveyResetOut:
    return student_data_service.reset_survey(db, admin, student_id, scope=payload.scope)
