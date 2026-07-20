import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.constants import SESSION_STATUS_COMPLETED
from app.models import InterviewSession


class SessionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, student_id: str, case_id: str, **extra) -> InterviewSession:
        session = InterviewSession(student_id=student_id, case_id=case_id, **extra)
        self.db.add(session)
        self.db.flush()
        return session

    def get(self, session_id: str) -> InterviewSession | None:
        return self.db.get(InterviewSession, session_id)

    def get_disclosed_fact_ids(self, session: InterviewSession) -> set[str]:
        try:
            return set(json.loads(session.disclosed_fact_ids or "[]"))
        except (TypeError, ValueError):
            return set()

    def add_disclosed_fact_ids(self, session: InterviewSession, fact_ids: set[str]) -> None:
        merged = self.get_disclosed_fact_ids(session) | set(fact_ids)
        session.disclosed_fact_ids = json.dumps(sorted(merged))
        self.db.flush()

    def complete_and_lock(self, session: InterviewSession) -> InterviewSession:
        session.status = SESSION_STATUS_COMPLETED
        session.locked = True
        session.completed_at = datetime.now(timezone.utc)
        self.db.flush()
        return session

    def set_active_topic(self, session, topic) -> None:
        session.active_topic = topic
        self.db.flush()
