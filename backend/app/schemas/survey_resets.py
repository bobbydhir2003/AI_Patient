"""Admin "Survey Resets" schemas: per-student survey stage state for REAL
students plus the bulk reset request/result. Admin-only; never used on student
routes. Single-student resets reuse the Student Data SurveyResetIn/Out."""
from datetime import datetime
from typing import Literal

from app.schemas.base import CamelModel

# completed   -> stage reached REDCap (or was skipped: REDCap unconfigured)
# failed      -> stage was submitted but the REDCap import failed (retryable)
# pending     -> the survey package exists but this stage is not done yet (incl. after a reset)
# not_started -> the student has no survey package at all
SurveyStageStatus = Literal["completed", "failed", "pending", "not_started"]


class SurveyResetSummary(CamelModel):
    # All over eligible REAL students (practice profiles excluded), never filtered
    # by the page's search/status/case filters.
    total_students: int
    pre_completed: int
    post_completed: int
    # Students with at least one resettable survey state (any receipt or survey
    # ownership), i.e. a "Reset Full" would apply.
    available_for_reset: int
    # How many students each bulk scope would actually reset right now.
    pre_resettable: int
    post_resettable: int
    both_resettable: int


class SurveyResetCaseOption(CamelModel):
    id: str
    name: str


class SurveyResetStudentRow(CamelModel):
    student_id: str
    name: str
    email: str
    student_number: str
    is_active: bool
    # Case whose receipt the stage statuses describe (the survey owner case when set).
    survey_case_id: str | None = None
    survey_case_name: str | None = None
    pre_status: SurveyStageStatus
    post_status: SurveyStageStatus
    pre_completed_at: datetime | None = None
    post_completed_at: datetime | None = None
    can_reset_pre: bool
    can_reset_post: bool
    can_reset_both: bool
    reset_count: int
    last_reset_at: datetime | None = None
    last_activity_at: datetime | None = None


class SurveyResetListOut(CamelModel):
    summary: SurveyResetSummary
    case_options: list[SurveyResetCaseOption]
    items: list[SurveyResetStudentRow]
    total: int
    page: int
    page_size: int


class BulkSurveyResetIn(CamelModel):
    """Only the scope: the target set is always computed server-side (eligible
    real students). Any other body field is ignored."""

    scope: Literal["pre", "post", "both"]


class BulkSurveyResetOut(CamelModel):
    scope: str
    eligible: int  # real students considered
    reset: int     # actually reset
    skipped: int   # nothing to reset for this scope
    failed: int    # unexpected error; that student was left unchanged
    message: str
