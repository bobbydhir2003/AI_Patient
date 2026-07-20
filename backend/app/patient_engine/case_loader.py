"""Loads and validates the structured case files in app/cases/."""
import json
from functools import lru_cache
from pathlib import Path

from app.core.constants import CASE_IDS
from app.core.exceptions import CaseNotFoundError
from app.schemas.case_schema import CaseDefinition

CASES_DIR = Path(__file__).resolve().parent.parent / "cases"


@lru_cache
def load_all_cases() -> dict[str, CaseDefinition]:
    cases: dict[str, CaseDefinition] = {}
    for case_id in CASE_IDS:
        path = CASES_DIR / f"{case_id}.json"
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        case = CaseDefinition.model_validate(data)
        if case.case_id != case_id:
            raise ValueError(f"Case file {path.name} declares case_id '{case.case_id}'")
        cases[case_id] = case
    return cases


def load_case(case_id: str) -> CaseDefinition:
    cases = load_all_cases()
    if case_id not in cases:
        raise CaseNotFoundError(case_id)
    return cases[case_id]
