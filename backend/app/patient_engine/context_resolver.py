"""Resolves conversation context for prompt construction."""
from dataclasses import dataclass, field

from app.core.constants import MAX_HISTORY_TURNS, ROLE_PATIENT, ROLE_STUDENT
from app.models import ConversationTurn


@dataclass
class InterviewContext:
    case_id: str
    topics: list[str]
    history: list[dict] = field(default_factory=list)  # [{"role": ..., "content": ...}]
    disclosed_fact_ids: set[str] = field(default_factory=set)
    active_topic: str | None = None
    turn_count: int = 0


def resolve_context(
    case_id: str,
    topics: list[str],
    turns: list[ConversationTurn],
    disclosed_fact_ids: set[str],
    active_topic: str | None = None,
) -> InterviewContext:
    recent = turns[-MAX_HISTORY_TURNS:]
    history = [
        {"role": "user" if t.role == ROLE_STUDENT else "assistant", "content": t.content}
        for t in recent
        if t.role in (ROLE_STUDENT, ROLE_PATIENT)
    ]
    return InterviewContext(
        case_id=case_id,
        topics=topics,
        history=history,
        disclosed_fact_ids=set(disclosed_fact_ids),
        active_topic=active_topic,
        turn_count=len(turns),
    )
