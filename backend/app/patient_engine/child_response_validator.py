"""Validates that Camden (age 4) sounds like a real preschooler.

Applied AFTER generation for the `camden` speaker. If the reply is too long or
contains clinical language a 4-year-old would not use, it is shortened and, when
medical content remains, replaced with a safe child deflection to his mother -
so an inappropriate answer is never sent to the student.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_CHILD_WORDS = 25

# Terms a 4-year-old would not realistically say -> deflect to the mother.
_MEDICAL_TERMS = re.compile(
    r"\b(leukemia|leukaemia|chemo\w*|diagnos\w+|vincristine|dexamethasone|"
    r"medication|dosage|dose|milligram|mg|symptom\w*|treatment|therapy|prognos\w+|"
    r"appointment|schedule|insurance|transportation|neuropathy|mobility|endurance|"
    r"participation|physical activity|side effects?|oncolog\w+|steroid\w*)\b",
    re.IGNORECASE,
)

_CHILD_DEFLECTION = "I don't know. You can ask my mom."


@dataclass(frozen=True)
class ChildCheck:
    text: str
    valid: bool  # whether the ORIGINAL text passed unchanged
    changed: bool  # whether we shortened/deflected
    reason: str


def _first_sentences(text: str, max_words: int) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    out: list[str] = []
    count = 0
    for p in parts:
        w = len(p.split())
        if out and count + w > max_words:
            break
        out.append(p)
        count += w
        if count >= max_words:
            break
    result = " ".join(out).strip() or text.strip()
    # Hard word cap as a backstop for run-on text with no sentence breaks.
    words = result.split()
    if len(words) > max_words:
        result = " ".join(words[:max_words])
    return result


def validate_child_response(text: str) -> ChildCheck:
    original = (text or "").strip()
    if not original:
        return ChildCheck(_CHILD_DEFLECTION, False, True, "empty -> deflect")

    # Clinical language a preschooler wouldn't use -> deflect to the mother.
    if _MEDICAL_TERMS.search(original):
        return ChildCheck(_CHILD_DEFLECTION, False, True, "medical content -> child deflection")

    words = original.split()
    if len(words) > MAX_CHILD_WORDS or original.count("\n") > 1:
        shortened = _first_sentences(original, MAX_CHILD_WORDS)
        if _MEDICAL_TERMS.search(shortened):
            return ChildCheck(_CHILD_DEFLECTION, False, True, "too long + medical -> deflection")
        return ChildCheck(shortened, False, True, "too long -> shortened")

    return ChildCheck(original, True, False, "ok")
