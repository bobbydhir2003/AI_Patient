"""Selects the case facts relevant to the current student question."""
from app.schemas.case_schema import CaseDefinition, CaseFact

# Topics that always stay available so the patient remains self-consistent.
_CORE_TOPICS = ("condition",)


def select_facts(case: CaseDefinition, topics: list[str]) -> list[CaseFact]:
    wanted = set(topics) | set(_CORE_TOPICS)
    selected = [f for f in case.facts if f.topic in wanted]
    # Keep original (curated) order from the case file.
    return selected


def facts_by_id(case: CaseDefinition, fact_ids: set[str]) -> list[CaseFact]:
    return [f for f in case.facts if f.id in fact_ids]
