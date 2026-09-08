"""Shared student-turn persistence helper.

Extracted from the former ``native_agent.py`` (native-agent mode removed) so the
production ``prompt_agent`` runtime keeps a stable, engine-neutral import for the
one idempotent writer it actually uses. Pure persistence: dedups by
``client_turn_id`` and validates session ownership/lock state. No engine-specific
(native/controlled) logic belongs here.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.constants import ROLE_STUDENT
from app.models import ConversationTurn
from app.repositories.session_repository import SessionRepository
from app.repositories.transcript_repository import TranscriptRepository


class TurnPersistenceError(RuntimeError):
    """A student-turn persistence request failed closed (session unavailable or
    a turn-correlation conflict). Callers treat it as a non-fatal skip."""


def persist_student_turn_once(
    db: Session,
    *,
    session_id: str,
    case_id: str,
    client_turn_id: str,
    text: str,
    source: str,
) -> ConversationTurn:
    session_repo = SessionRepository(db)
    transcript_repo = TranscriptRepository(db)
    session = session_repo.get(session_id)
    if session is None or session.case_id != case_id or session.locked:
        raise TurnPersistenceError("interview session is unavailable")
    existing = transcript_repo.get_by_client_turn_id(session_id, client_turn_id)
    if existing is not None:
        if existing.role != ROLE_STUDENT:
            raise TurnPersistenceError("turn correlation is invalid")
        return existing
    turn = transcript_repo.append_turn(
        session_id, ROLE_STUDENT, text.strip(), client_turn_id=client_turn_id, source=source,
    )
    db.commit()
    return turn
