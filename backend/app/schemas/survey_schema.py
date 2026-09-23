"""Pre/Post experience survey request & response schemas.

The request models use the EXACT REDCap variable names as their field names
(snake_case, no camelCase aliasing) so the wire contract is the REDCap data
dictionary itself, and ``extra="forbid"`` rejects any field name that is not a
recognised REDCap variable. Likert values are validated as integers 1-5;
open-ended answers are length-limited (sanitised) strings.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.base import CamelModel

# 1-5 Likert (1 = Strongly disagree ... 5 = Strongly agree), matching the shared
# REDCap radio coding for every Likert item in both instruments.
Likert = Annotated[int, Field(ge=1, le=5)]

# Open-ended answers are length-limited to keep payloads bounded.
MAX_OPEN_ENDED_LEN = 5000
# Optional variant (retained for any non-required open-ended field).
OpenEnded = Annotated[str, Field(default="", max_length=MAX_OPEN_ENDED_LEN)]
# Required variant for VISIBLE open-ended questions: whitespace is stripped first,
# then min_length=1 rejects an empty or whitespace-only answer (422). The stored/
# forwarded value is the trimmed text.
RequiredOpenEnded = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_OPEN_ENDED_LEN)
]


class _StrictSurveyModel(BaseModel):
    # Exact REDCap variable names on the wire (no camelCase); reject anything
    # that is not an explicitly declared REDCap field.
    model_config = ConfigDict(extra="forbid")


class PreSurveyIn(_StrictSurveyModel):
    # 4 required Likert items.
    pre_conf_begin: Likert
    pre_conf_questions: Likert
    pre_conf_unexpected: Likert
    pre_conf_interview: Likert
    # Retired from the UI but kept as a RECOGNISED, OPTIONAL REDCap field so the
    # existing variable/historical data stay valid and an older client that still
    # sends it validates. Omitted -> excluded from the REDCap payload
    # (submit_pre uses model_dump(exclude_none=True)); never re-required.
    pre_helpful_draft: Likert | None = None
    # Open-ended feedback, shown as the last visible pre-survey question. Required
    # (non-blank), like every other visible question.
    pre_feedback: RequiredOpenEnded


class PostSurveyIn(_StrictSurveyModel):
    # 11 Likert items
    post_conf_begin: Likert
    post_conf_questions: Likert
    post_conf_unexpected: Likert
    post_conf_interview: Likert
    post_realistic: Likert
    post_consistent: Likert
    post_strengths_weaknesses: Likert
    post_safe_mistakes: Likert
    post_feedback_accurate: Likert
    post_feedback_actionable: Likert
    post_use_again: Likert
    # 5 open-ended items (all visible -> all required, non-blank)
    post_oe_most_helpful: RequiredOpenEnded
    post_oe_unrealistic: RequiredOpenEnded
    post_oe_one_change: RequiredOpenEnded
    post_oe_feedback_type: RequiredOpenEnded
    post_oe_feedback_missing: RequiredOpenEnded


class SurveyStatusOut(CamelModel):
    """Case-level survey state, used by the frontend to gate the survey flow
    (skip a completed package, resume an in-progress one, or collect normally)
    and to surface a missing-NUID condition early. Reflects the ONE (student, case)
    receipt, resolved server-side from the session - not a per-session/per-phase
    row. Never includes the answers or the REDCap token."""

    session_id: str
    case_name: str
    # REDCap case number (1-4) for this case slug, or None if the case has no
    # survey mapping. Read-only, server-derived - shown on the Pre-Survey.
    case_number: int | None = None
    # The authenticated student's NUID, resolved server-side for READ-ONLY
    # display/confirmation on the Pre-Survey. The client never submits this back;
    # the Pre/Post request bodies reject any identity field (extra="forbid").
    nuid: str = ""
    nuid_on_file: bool
    # Case-level lifecycle: "not_started" (no receipt yet) | "in_progress" |
    # "completed". This is the field the frontend keys its gating decision on.
    overall_status: str
    # A stage is "submitted" once it has reached REDCap or was skipped
    # (unconfigured). Kept for the frontend's resume decision (Pre done but
    # package still in progress -> continue to interview without re-asking Pre).
    pre_submitted: bool
    post_submitted: bool
    pre_sync_status: str | None = None
    post_sync_status: str | None = None
    # ---- Global (one survey PACKAGE per student) fields ----
    # The student's global survey lifecycle, independent of this case's own
    # receipt: "not_started" | "in_progress" | "completed". The frontend gates
    # EVERY survey screen on this so a non-owning case shows the skip state.
    global_survey_status: str = "not_started"
    # The single case that owns the student's survey package (slug), or None.
    survey_owner_case_id: str | None = None
    # Human-readable display name of the owning case (e.g. "Carly"), resolved
    # server-side from the slug for clear survey messaging. None when no owner.
    survey_owner_case_name: str | None = None
    # True when this session's case IS the owning case (collect its Pre/Post per
    # the receipt's own stage state); False -> this case must skip.
    is_survey_owner_case: bool = False
    # Convenience flag: the whole global package is completed.
    global_survey_completed: bool = False


class SurveySubmitResult(CamelModel):
    """Result of a Pre/Post survey STAGE submission for the case-level package."""

    phase: str  # "pre" | "post"
    sync_status: str  # pending | synced | failed | skipped
    # Case-level lifecycle after this stage: "in_progress" | "completed".
    overall_status: str
    already_submitted: bool = False
