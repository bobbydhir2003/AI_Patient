from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.dependencies.auth import (
    get_current_user,
    require_session_access,
    require_simulator_access,
)
from app.models import InterviewSession, User
from app.schemas.interview_schema import TurnCreateRequest, TurnOut
from app.schemas.session_schema import SessionCreateRequest, SessionResponse
from app.services import session_service

# Every route requires an authenticated account. Ownership on the id-scoped
# routes is enforced by require_session_access (admins may reach any session;
# a student may reach only sessions owned by their linked profile).
router = APIRouter(
    prefix="/sessions",
    tags=["sessions"],
    dependencies=[Depends(get_current_user)],
)


@router.post("", response_model=SessionResponse, status_code=201)
def create_session(
    payload: SessionCreateRequest,
    current_user: User = Depends(require_simulator_access),
    db: Session = Depends(get_db),
) -> SessionResponse:
    # A3: ownership comes from the authenticated account, never the request body.
    # Students and admins/professors (practice) may both create sessions.
    return session_service.create_session(db, payload, current_user)


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(
    session: InterviewSession = Depends(require_session_access),
    db: Session = Depends(get_db),
) -> SessionResponse:
    return session_service.get_session(db, session.id)


@router.post("/{session_id}/complete", response_model=SessionResponse)
def complete_session(
    session: InterviewSession = Depends(require_session_access),
    db: Session = Depends(get_db),
) -> SessionResponse:
    return session_service.complete_session(db, session.id)


@router.get("/{session_id}/turns", response_model=list[TurnOut])
def list_turns(
    session: InterviewSession = Depends(require_session_access),
    db: Session = Depends(get_db),
) -> list[TurnOut]:
    """All saved transcript turns in stable conversation order."""
    return session_service.list_turns(db, session.id)


@router.post("/{session_id}/turns", response_model=TurnOut, status_code=201)
def append_turn(
    payload: TurnCreateRequest,
    session: InterviewSession = Depends(require_session_access),
    db: Session = Depends(get_db),
) -> TurnOut:
    """Idempotent turn save. A4: the speaker is enforced server-side - this
    student-facing endpoint only accepts student-authored turns; patient turns
    are created exclusively by the trusted generation path."""
    return session_service.append_student_turn(db, session.id, payload)
