"""Loads instructor rubric definitions and protected case assessment references."""
import json
from functools import lru_cache
from pathlib import Path

from app.core.exceptions import CaseNotFoundError

_RUBRICS_DIR = Path(__file__).resolve().parent.parent / "rubrics"
_CASE_ASSESS_DIR = Path(__file__).resolve().parent.parent / "case_assessment"

RUBRIC_FILES = ("oars.json", "history.json", "safety.json", "empathy.json")


@lru_cache
def load_rubrics() -> list[dict]:
    rubrics = []
    for name in RUBRIC_FILES:
        with (_RUBRICS_DIR / name).open(encoding="utf-8") as fh:
            rubrics.append(json.load(fh))
    return rubrics


@lru_cache
def load_case_reference(case_id: str) -> dict:
    path = _CASE_ASSESS_DIR / f"{case_id}_assessment.json"
    if not path.exists():
        raise CaseNotFoundError(case_id)
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("case_id") != case_id:
        raise ValueError(f"Case assessment reference {path.name} declares wrong case_id")
    return data


def rubric_version() -> str:
    return load_rubrics()[0].get("version", "1.0")


@lru_cache
def load_referral_rubric() -> dict:
    with (_RUBRICS_DIR / "advanced_referral.json").open(encoding="utf-8") as fh:
        return json.load(fh)


def referral_rubric_version() -> str:
    return load_referral_rubric().get("version", "1.0")
