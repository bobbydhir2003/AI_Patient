"""Deterministic speaker routing for multi-participant cases (Camden + mother).

Dr. Dexter's requirement: the mother is the primary historian; Camden (age 4)
answers only when directly/clearly addressed. This layer decides WHO should
speak BEFORE any model call, so it adds no latency for the common cases.

Resolution order (first match wins):
  1. Explicit "both of you" -> both
  2. Explicit mother address (and NOT naming Camden) -> mother
  3. Medical / history / logistics topic (or a timeline marker) -> mother
     (topic overrides address: "what medicine is he taking?" is always the mother,
      even right after the student was talking to Camden)
  4. Names Camden / a child nickname -> camden
  5. Direct simple child question (2nd person + child-appropriate cue) -> camden
  6. Short follow-up while Camden is the active addressee -> camden
  7. Default -> mother
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.patient_engine import topic_classifier

SPEAKER_MOTHER = "mother"
SPEAKER_CAMDEN = "camden"
SPEAKER_BOTH = "both"

# Topics only the mother (historian) can reliably answer.
_MOTHER_TOPICS = {
    "condition", "medications", "exam_findings", "healthcare_access",
    "school_work", "sleep", "nutrition", "family_social", "wellness_profile",
}
# Topics a 4-year-old can speak to about his immediate experience.
_CAMDEN_TOPICS = {"symptoms_pain", "activity_exercise", "goals_motivation", "emotional_wellbeing"}

_CAMDEN_NAME = re.compile(r"\b(camden|cammy|cam)\b", re.IGNORECASE)
_MOTHER_ADDRESS = re.compile(
    r"\b(mom|mum|mommy|mother|parent|caregiver|ma'?am|mrs\.?\s*anderson)\b|as (his|a) (mom|mother)",
    re.IGNORECASE,
)
_BOTH = re.compile(r"\b(both of (you|your)|you both|you two|hear from both|from both of)\b", re.IGNORECASE)
_TIMELINE = re.compile(
    r"\b(when did|when was|how long|since when|over time|first notice|first notic|started|"
    r"began|history of|timeline|how often|how many)\b",
    re.IGNORECASE,
)
# Simple, immediate-experience cues a child would field.
_CHILD_CUE = re.compile(
    r"\b(hurt|hurts|owie|tired|sleepy|scared|afraid|play|playing|toys?|game|games|fun|"
    r"favorite|favourite|like to|do you like|do you want|feel|feeling|happy|sad|"
    r"stuff(ies|ed)?|outside|color|colour|draw)\b",
    re.IGNORECASE,
)
_SECOND_PERSON = re.compile(r"\b(you|your|you're|ya)\b", re.IGNORECASE)
_FOLLOWUP = re.compile(r"^\s*(when|why|really|and|oh|how come|what about|where|who|and then)\b[\w\s]{0,20}\??\s*$", re.IGNORECASE)
# Topic-less continuations ("tell me more", "go on") stay with the active speaker.
_CONTINUATION = re.compile(
    r"^\s*(can you )?(tell me more|go on|continue|say more|anything else|what else|and then\??|more\??)\s*\??\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RoutingResult:
    speaker: str          # camden | mother | both
    confidence: float     # internal only - never sent to the student UI
    reason: str           # internal only


def _is_short_followup(m: str) -> bool:
    words = m.split()
    return len(words) <= 5 and bool(_FOLLOWUP.match(m))


def _is_direct_child_question(m: str, topics: list[str]) -> bool:
    if not _SECOND_PERSON.search(m):
        return False
    if any(t in _CAMDEN_TOPICS for t in topics):
        return True
    return bool(_CHILD_CUE.search(m))


def route(
    message: str,
    *,
    previous_speaker: str | None = None,
    topics: list[str] | None = None,
) -> RoutingResult:
    m = (message or "").strip()
    if not m:
        return RoutingResult(SPEAKER_MOTHER, 0.5, "empty message -> default mother")

    topics = topics if topics is not None else topic_classifier.classify(m)
    names_camden = bool(_CAMDEN_NAME.search(m))
    addresses_mother = bool(_MOTHER_ADDRESS.search(m))
    is_history = bool(_TIMELINE.search(m))
    topic_is_mother = any(t in _MOTHER_TOPICS for t in topics) or is_history

    # 1) explicitly wants both
    if _BOTH.search(m):
        return RoutingResult(SPEAKER_BOTH, 0.95, "student explicitly addressed both")

    # 2) explicit mother address (and not naming Camden)
    if addresses_mother and not names_camden:
        return RoutingResult(SPEAKER_MOTHER, 0.95, "student addressed the mother")

    # 3) medical / history / logistics topic -> mother (overrides child address)
    if topic_is_mother:
        return RoutingResult(SPEAKER_MOTHER, 0.9, f"historian topic {topics}/timeline={is_history}")

    # 4) names Camden -> Camden
    if names_camden:
        return RoutingResult(SPEAKER_CAMDEN, 0.9, "student named Camden")

    # 5) direct simple child question
    if _is_direct_child_question(m, topics):
        return RoutingResult(SPEAKER_CAMDEN, 0.8, "direct child question")

    # 6) short follow-up / topic-less continuation keeps the ACTIVE addressee
    #    (both directions). Topic-based routing above already claimed medical Qs.
    if previous_speaker in (SPEAKER_CAMDEN, SPEAKER_MOTHER) and (
        _is_short_followup(m) or _CONTINUATION.match(m)
    ):
        return RoutingResult(previous_speaker, 0.75, f"follow-up keeps active speaker ({previous_speaker})")

    # 7) default: the mother is the primary historian
    return RoutingResult(SPEAKER_MOTHER, 0.6, "default to primary historian (mother)")


# --------------------------- participant helpers ---------------------------
def is_multi_participant(case) -> bool:
    return len(getattr(case, "participants", []) or []) > 1


def previous_patient_speaker(turns) -> str | None:
    """The speaker_id of the most recent patient turn (drives follow-up routing)."""
    for t in reversed(turns or []):
        if getattr(t, "role", None) == "patient":
            sid = getattr(t, "speaker_id", None)
            return sid if sid in (SPEAKER_CAMDEN, SPEAKER_MOTHER) else None
    return None


def participant_meta(case, speaker_id: str) -> tuple[str, str, str]:
    """Return (speaker_id, display_label, voice_key) for a participant.

    Falls back to a generic single-speaker 'Patient' when the case is not
    multi-participant or the id is unknown."""
    for p in getattr(case, "participants", []) or []:
        if p.id == speaker_id:
            return p.id, p.display_name, (p.voice_key or "patient")
    return "patient", "Patient", "patient"


def resolve_for_case(case, message: str, turns, topics=None) -> RoutingResult:
    """Route only for multi-participant cases; single-speaker cases keep the
    generic single 'patient' speaker (no behavior change)."""
    if not is_multi_participant(case):
        return RoutingResult("patient", 1.0, "single-speaker case")
    return route(message, previous_speaker=previous_patient_speaker(turns), topics=topics)
