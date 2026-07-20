"""Progressive-disclosure rules: what may the patient reveal on this turn?"""
from app.core.constants import (
    DISCLOSURE_OPEN,
    DISCLOSURE_PROBE,
    DISCLOSURE_SENSITIVE,
    MAX_FACTS_PER_TURN,
)
from app.schemas.case_schema import CaseDefinition, CaseFact

_SENSITIVE_TOPICS = {"emotional_wellbeing", "family_social", "goals_motivation"}


class DisclosureManager:
    def __init__(self, case: CaseDefinition, disclosed_fact_ids: set[str]) -> None:
        self.case = case
        self.disclosed = set(disclosed_fact_ids)

    def eligible_facts(self, candidate_facts: list[CaseFact], topics: list[str]) -> list[CaseFact]:
        """Facts the patient may use this turn (new facts capped per turn)."""
        topic_set = set(topics)
        eligible: list[CaseFact] = []
        new_count = 0
        for fact in candidate_facts:
            if fact.disclosure == DISCLOSURE_OPEN:
                allowed = True
            elif fact.disclosure == DISCLOSURE_PROBE:
                allowed = fact.topic in topic_set
            elif fact.disclosure == DISCLOSURE_SENSITIVE:
                allowed = fact.topic in topic_set and fact.topic in _SENSITIVE_TOPICS
            else:
                allowed = False
            if not allowed:
                continue
            is_new = fact.id not in self.disclosed
            if is_new and new_count >= MAX_FACTS_PER_TURN:
                continue
            if is_new:
                new_count += 1
            eligible.append(fact)
        return eligible

    def mark_disclosed(self, facts: list[CaseFact]) -> set[str]:
        ids = {f.id for f in facts}
        self.disclosed |= ids
        return ids
