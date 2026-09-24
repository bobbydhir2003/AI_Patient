"""Admin "Student Data" schemas: one student-centred view combining identity,
sessions, assessments, assessment viewing (per visit), survey state and a
derived activity timeline. Admin-only; never used on student routes."""
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.base import CamelModel


# ---------------- list ----------------
class StudentDataListItem(CamelModel):
    id: str
    name: str
    email: str
    student_number: str
    is_active: bool
    has_account: bool
    session_count: int
    completed_count: int
    incomplete_count: int
    # Most recent of: login, interview start/completion, assessment view heartbeat,
    # survey stage submission. Same definition as the detail header.
    last_activity_at: datetime | None = None


class PaginatedStudentData(CamelModel):
    items: list[StudentDataListItem]
    total: int
    page: int
    page_size: int


# ---------------- detail ----------------
class StudentDataProfile(CamelModel):
    id: str
    name: str
    email: str
    student_number: str
    is_active: bool
    has_account: bool
    role: str | None = None
    created_at: datetime
    last_login_at: datetime | None = None
    last_activity_at: datetime | None = None


class StudentDataSummary(CamelModel):
    total_sessions: int
    completed_sessions: int
    incomplete_sessions: int  # status == active (started, never completed)
    archived_sessions: int
    completed_without_assessment: int
    total_interview_seconds: int
    average_interview_seconds: int | None = None  # over completed sessions
    total_student_questions: int
    total_assessments: int  # assessment runs (incl. retries)
    assessed_sessions: int  # sessions with at least one run
    total_view_seconds: int
    total_visits: int
    viewed_sessions: int


class AssessmentVisitOut(CamelModel):
    visit_number: int  # 1-based, oldest first within the session
    source: str  # initial_assessment | student_dashboard | legacy | unknown
    started_at: datetime
    last_heartbeat_at: datetime
    active_seconds: int
    # True when the assessment run this visit viewed was later deleted (the visit
    # history is kept; it belongs to the session, not the run).
    assessment_deleted: bool = False


class StudentDataSessionRow(CamelModel):
    session_id: str
    case_id: str
    case_category: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: int | None = None
    turn_count: int
    student_question_count: int
    has_assessment: bool
    assessment_id: str | None = None
    assessment_status: str | None = None
    overall_level: str | None = None
    assessment_completed_at: datetime | None = None
    active_viewing_seconds: int | None = None
    view_count: int | None = None
    first_viewed_at: datetime | None = None
    last_viewed_at: datetime | None = None
    # Survey state of this session's CASE for this student:
    # completed | pre_completed | not_completed | other_case
    survey_status: str
    visits: list[AssessmentVisitOut] = Field(default_factory=list)


class SurveyStageOut(CamelModel):
    completed: bool
    # pending | synced | failed | skipped (skipped = recorded locally, REDCap off)
    sync_status: str | None = None
    completed_at: datetime | None = None


class SurveyStateOut(CamelModel):
    # not_started | in_progress | completed (the student's ONE global package)
    global_status: str
    owner_case_id: str | None = None
    owner_case_name: str | None = None
    # Case of the receipt the stages below describe (owner case when set).
    case_id: str | None = None
    case_name: str | None = None
    completed_at: datetime | None = None
    pre: SurveyStageOut
    post: SurveyStageOut
    last_response_at: datetime | None = None
    can_reset_pre: bool
    can_reset_post: bool
    can_reset_both: bool
    reset_count: int
    last_reset_at: datetime | None = None


class TimelineEventOut(CamelModel):
    # interview_started | interview_completed | assessment_generated |
    # assessment_viewed | pre_survey_completed | post_survey_completed | survey_reset
    kind: str
    at: datetime
    title: str
    detail: str = ""
    session_id: str | None = None


class StudentDataDetailOut(CamelModel):
    student: StudentDataProfile
    summary: StudentDataSummary
    survey: SurveyStateOut
    sessions: list[StudentDataSessionRow]
    timeline: list[TimelineEventOut]


# ---------------- survey reset ----------------
class SurveyResetIn(CamelModel):
    """Only the scope. The student is identified by the route; nothing else in
    the body is read."""

    scope: Literal["pre", "post", "both"]


class SurveyResetOut(CamelModel):
    success: bool = True
    message: str = ""
    survey: SurveyStateOut
