from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database.connection import get_db
from app.schemas.interview_schema import TurnCreateRequest, TurnOut
from app.schemas.session_schema import SessionCreateRequest, SessionResponse
from app.services import session_service

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionResponse, status_code=201)
def create_session(payload: SessionCreateRequest, db: Session = Depends(get_db)) -> SessionResponse:
    return session_service.create_session(db, payload)


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(session_id: str, db: Session = Depends(get_db)) -> SessionResponse:
    return session_service.get_session(db, session_id)


@router.post("/{session_id}/complete", response_model=SessionResponse)
def complete_session(session_id: str, db: Session = Depends(get_db)) -> SessionResponse:
    return session_service.complete_session(db, session_id)


@router.get("/{session_id}/turns", response_model=list[TurnOut])
def list_turns(session_id: str, db: Session = Depends(get_db)) -> list[TurnOut]:
    """All saved transcript turns in stable conversation order."""
    return session_service.list_turns(db, session_id)


@router.post("/{session_id}/turns", response_model=TurnOut, status_code=201)
def append_turn(
    session_id: str, payload: TurnCreateRequest, db: Session = Depends(get_db)
) -> TurnOut:
    """Idempotent turn save: repeating a client_turn_id returns the saved turn."""
    return session_service.append_turn(db, session_id, payload)
