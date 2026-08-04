"""Student self-service endpoints.

Every route requires an authenticated student and only ever exposes data owned
by that student's linked profile. `require_session_access` performs the
ownership check for single-session routes.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assessment import assessment_service
from app.core.exceptions import AssessmentNotFoundError
from app.database.connection import get_db
from app.dependencies.auth import require_session_access, require_simulator_access
from app.models import InterviewSession, User
from app.schemas.admin import SessionSummaryOut, TranscriptMessageOut
from app.schemas.assessment_schema import AssessmentOut
from app.schemas.auth import UserOut
from app.services import admin_service

router = APIRouter(prefix="/students", tags=["students"])


@router.get("/me", response_model=UserOut)
def my_profile(current_user: User = Depends(require_simulator_access)) -> UserOut:
    return UserOut.model_validate(current_user)


@router.get("/me/sessions", response_model=list[SessionSummaryOut])
def my_sessions(
    current_user: User = Depends(require_simulator_access),
    db: Session = Depends(get_db),
) -> list[SessionSummaryOut]:
    if not current_user.student_id:
        return []
    rows = list(
        db.execute(
            select(InterviewSession)
            .where(InterviewSession.student_id == current_user.student_id)
            .order_by(InterviewSession.started_at.desc())
        ).scalars().all()
    )
    return [admin_service.session_summary(db, s) for s in rows]


@router.get("/me/sessions/{session_id}", response_model=SessionSummaryOut)
def my_session(
    session: InterviewSession = Depends(require_session_access),
    db: Session = Depends(get_db),
) -> SessionSummaryOut:
    return admin_service.session_summary(db, session)


@router.get("/me/sessions/{session_id}/transcript", response_model=list[TranscriptMessageOut])
def my_session_transcript(
    session: InterviewSession = Depends(require_session_access),
    db: Session = Depends(get_db),
) -> list[TranscriptMessageOut]:
    return admin_service.get_session_transcript(db, session.id)


@router.get("/me/sessions/{session_id}/assessment", response_model=AssessmentOut | None)
def my_session_assessment(
    session: InterviewSession = Depends(require_session_access),
    db: Session = Depends(get_db),
) -> AssessmentOut | None:
    try:
        return assessment_service.get_latest_for_session(db, session.id)
    except AssessmentNotFoundError:
        return None
