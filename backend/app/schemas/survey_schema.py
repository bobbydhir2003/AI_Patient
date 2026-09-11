"""Pre/Post experience survey request & response schemas.

The request models use the EXACT REDCap variable names as their field names
(snake_case, no camelCase aliasing) so the wire contract is the REDCap data
dictionary itself, and ``extra="forbid"`` rejects any field name that is not a
recognised REDCap variable. Likert values are validated as integers 1-5;
open-ended answers are length-limited (sanitised) strings.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.base import CamelModel

# 1-5 Likert (1 = Strongly disagree ... 5 = Strongly agree), matching the shared
# REDCap radio coding for every Likert item in both instruments.
Likert = Annotated[int, Field(ge=1, le=5)]

# Open-ended answers are optional and length-limited to keep payloads bounded.
MAX_OPEN_ENDED_LEN = 5000
OpenEnded = Annotated[str, Field(default="", max_length=MAX_OPEN_ENDED_LEN)]


class _StrictSurveyModel(BaseModel):
    # Exact REDCap variable names on the wire (no camelCase); reject anything
    # that is not an explicitly declared REDCap field.
    model_config = ConfigDict(extra="forbid")


class PreSurveyIn(_StrictSurveyModel):
    pre_conf_begin: Likert
    pre_conf_questions: Likert
    pre_conf_unexpected: Likert
    pre_conf_interview: Likert
    pre_helpful_draft: Likert


class PostSurveyIn(_StrictSurveyModel):
    # 12 Likert items
    post_conf_begin: Likert
    post_conf_questions: Likert
    post_conf_unexpected: Likert
    post_conf_interview: Likert
    post_helpful_draft: Likert
    post_realistic: Likert
    post_consistent: Likert
    post_strengths_weaknesses: Likert
    post_safe_mistakes: Likert
    post_feedback_accurate: Likert
    post_feedback_actionable: Likert
    post_use_again: Likert
    # 5 open-ended items
    post_oe_most_helpful: OpenEnded
    post_oe_unrealistic: OpenEnded
    post_oe_one_change: OpenEnded
    post_oe_feedback_type: OpenEnded
    post_oe_feedback_missing: OpenEnded


class SurveyStatusOut(CamelModel):
    """Case-level survey state, used by the frontend to gate the survey flow
    (skip a completed package, resume an in-progress one, or collect normally)
    and to surface a missing-NUID condition early. Reflects the ONE (student, case)
    receipt, resolved server-side from the session - not a per-session/per-phase
    row. Never includes the answers or the REDCap token."""

    session_id: str
    case_name: str
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


class SurveySubmitResult(CamelModel):
    """Result of a Pre/Post survey STAGE submission for the case-level package."""

    phase: str  # "pre" | "post"
    sync_status: str  # pending | synced | failed | skipped
    # Case-level lifecycle after this stage: "in_progress" | "completed".
    overall_status: str
    already_submitted: bool = False
