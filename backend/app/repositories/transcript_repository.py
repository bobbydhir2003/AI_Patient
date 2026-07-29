import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ConversationTurn


class TranscriptRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def next_turn_index(self, session_id: str) -> int:
        stmt = select(func.count(ConversationTurn.id)).where(ConversationTurn.session_id == session_id)
        return int(self.db.execute(stmt).scalar_one())

    def append_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        client_turn_id: str | None = None,
        source: str | None = None,
        model_name: str | None = None,
        prompt_version: str | None = None,
        facts_used: list[str] | None = None,
        response_type: str | None = None,
        validation_status: str | None = None,
        speaker_id: str = "patient",
        speaker_label: str = "",
    ) -> ConversationTurn:
        turn = ConversationTurn(
            session_id=session_id,
            turn_index=self.next_turn_index(session_id),
            role=role,
            content=content,
            client_turn_id=client_turn_id,
            source=source,
            model_name=model_name,
            prompt_version=prompt_version,
            facts_used=json.dumps(facts_used) if facts_used is not None else None,
            response_type=response_type,
            validation_status=validation_status,
            speaker_id=speaker_id,
            speaker_label=speaker_label,
        )
        self.db.add(turn)
        self.db.flush()
        return turn

    def list_turns(self, session_id: str) -> list[ConversationTurn]:
        stmt = (
            select(ConversationTurn)
            .where(ConversationTurn.session_id == session_id)
            .order_by(ConversationTurn.turn_index)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_by_client_turn_id(self, session_id: str, client_turn_id: str) -> ConversationTurn | None:
        if not client_turn_id:
            return None
        stmt = select(ConversationTurn).where(
            ConversationTurn.session_id == session_id,
            ConversationTurn.client_turn_id == client_turn_id,
        )
        return self.db.execute(stmt).scalars().first()

    def get_by_index(self, session_id: str, turn_index: int) -> ConversationTurn | None:
        stmt = select(ConversationTurn).where(
            ConversationTurn.session_id == session_id,
            ConversationTurn.turn_index == turn_index,
        )
        return self.db.execute(stmt).scalars().first()

    def count_nonempty_by_role(self, session_id: str, role: str) -> int:
        stmt = select(func.count(ConversationTurn.id)).where(
            ConversationTurn.session_id == session_id,
            ConversationTurn.role == role,
            func.length(func.trim(ConversationTurn.content)) > 0,
        )
        return int(self.db.execute(stmt).scalar_one())
