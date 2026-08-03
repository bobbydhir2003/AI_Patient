"""Resolves conversation context for prompt construction.

B8 - bounded actor context. Only the last N transcript turns (ACTOR_MAX_RECENT_TURNS)
are ever sent to the model, with a secondary character cap (ACTOR_CONTEXT_CHAR_LIMIT).
This keeps per-turn traffic cost flat as an interview grows long. It bounds ONLY
the rolling conversation history - the stable persona, patient facts, disclosure
logic, safety rules, speaker routing and case behavior are built separately in
prompt_builder from the full case definition and are never truncated here.
"""
from dataclasses import dataclass, field

from app.core.config import get_settings
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
    try:
        settings = get_settings()
        max_turns = settings.actor_max_recent_turns or MAX_HISTORY_TURNS
        char_limit = settings.actor_context_char_limit
    except Exception:  # settings unavailable (never block the interview)
        max_turns, char_limit = MAX_HISTORY_TURNS, 0

    recent = turns[-max_turns:]
    history = [
        {"role": "user" if t.role == ROLE_STUDENT else "assistant", "content": t.content}
        for t in recent
        if t.role in (ROLE_STUDENT, ROLE_PATIENT)
    ]
    # Secondary soft cap: drop the OLDEST history entries until under the char
    # budget (keeps the most recent context, which matters most for continuity).
    if char_limit and char_limit > 0:
        total = sum(len(h["content"]) for h in history)
        while history and total > char_limit:
            total -= len(history[0]["content"])
            history.pop(0)
    return InterviewContext(
        case_id=case_id,
        topics=topics,
        history=history,
        disclosed_fact_ids=set(disclosed_fact_ids),
        active_topic=active_topic,
        turn_count=len(turns),
    )
