"""Prove no hardcoded educational judgment exists in the assessment system.

The code may validate structure (fields, levels, real turns, case match);
it must never decide the educational result via scores, keyword counting,
or fixed question-to-credit mappings.
"""
import json
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
SRC = BACKEND.parent / "src"

ASSESSMENT_CODE = list((BACKEND / "app" / "assessment").rglob("*.py"))
DATA_FILES = list((BACKEND / "app" / "rubrics").glob("*.json")) + list(
    (BACKEND / "app" / "case_assessment").glob("*.json")
)

FORBIDDEN_CODE_PATTERNS = (
    r"score\s*\+=",
    r"points\s*\+=",
    r"\bscore\b\s*=\s*\d",
    r"if\s+.*\bin\s+question\b.*:",          # keyword-in-question credit
    r"required_question",
    r"if_question_contains",
    r"percent",
)

FORBIDDEN_DATA_KEYS = ("required_question", "if_question_contains", "responses", "answer", "points", "score")


def test_assessment_code_has_no_scoring_logic():
    offenders = []
    for path in ASSESSMENT_CODE:
        content = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN_CODE_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                offenders.append(f"{path.name}: {pattern}")
    assert not offenders, f"Hardcoded scoring logic found: {offenders}"


def test_rubrics_and_references_have_no_answer_mappings():
    for path in DATA_FILES:
        data = json.loads(path.read_text(encoding="utf-8"))
        blob = json.dumps(data).lower()
        for key in FORBIDDEN_DATA_KEYS:
            assert f'"{key}"' not in blob, f"{path.name} contains forbidden key '{key}'"


def test_case_references_are_case_isolated():
    # Build patient display names for every case, then check each assessment
    # reference never mentions another case's patient.
    display_names = {}
    for case_path in (BACKEND / "app" / "cases").glob("*.json"):
        data = json.loads(case_path.read_text(encoding="utf-8"))
        display_names[data["case_id"]] = data["display_name"].lower()
    for path in (BACKEND / "app" / "case_assessment").glob("*.json"):
        case_id = path.name.removesuffix("_assessment.json")
        blob = path.read_text(encoding="utf-8").lower()
        for other_id, other_name in display_names.items():
            if other_id == case_id:
                continue
            assert not re.search(rf"\b{re.escape(other_name)}\b", blob), (
                f"{path.name} mentions {other_name}"
            )


def test_frontend_assessment_ui_shows_no_numeric_scores():
    if not SRC.exists():
        return
    patterns = (r"\bscore\b", r"percent", r"%\s*<", r"\bpoints\b", r"outOf", r"maxScore")
    offenders = []
    for path in (SRC / "components" / "assessment").rglob("*.tsx") if (SRC / "components" / "assessment").exists() else []:
        content = path.read_text(encoding="utf-8")
        for pattern in patterns:
            if re.search(pattern, content, re.IGNORECASE):
                offenders.append(f"{path.name}: {pattern}")
    for page in SRC.glob("pages/AssessmentReviewPage.tsx"):
        content = page.read_text(encoding="utf-8")
        for pattern in patterns:
            if re.search(pattern, content, re.IGNORECASE):
                offenders.append(f"{page.name}: {pattern}")
    assert not offenders, f"Numeric scoring UI found: {offenders}"
