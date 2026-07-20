from datetime import datetime

from pydantic import Field

from app.schemas.base import CamelModel


# ---------------- Dashboard ----------------
class RecentActivityItem(CamelModel):
    session_id: str
    student_id: str
    student_name: str
    case_id: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None


class AssessmentLevelCount(CamelModel):
    """One qualitative assessment level and how many assessments reached it."""

    level: str
    count: int


class NeedsAttentionOut(CamelModel):
    """Actionable review queues derived from live session/assessment data."""

    incomplete_sessions: int
    completed_without_assessment: int
    students_multiple_incomplete: int
    # Explicit alias: the camel generator would render "...Over24H" (capital H).
    sessions_active_over_24h: int = Field(alias="sessionsActiveOver24h")


class RecentSessionItem(CamelModel):
    """Recent session enriched with the student number and assessment level."""

    session_id: str
    student_id: str
    student_name: str
    student_number: str
    case_id: str
    case_category: str
    status: str
    has_assessment: bool
    overall_level: str | None = None
    started_at: datetime
    completed_at: datetime | None = None


class RecentStudentItem(CamelModel):
    id: str
    name: str
    student_number: str
    session_count: int
    assessment_count: int
    last_activity_at: datetime | None = None


class DashboardOut(CamelModel):
    total_students: int
    active_students: int
    inactive_students: int
    total_sessions: int
    completed_sessions: int
    incomplete_sessions: int
    archived_sessions: int
    total_assessments: int
    recent_activity: list[RecentActivityItem] = Field(default_factory=list)
    # --- richer sections powering the instructor dashboard ---
    assessment_levels: list[AssessmentLevelCount] = Field(default_factory=list)
    needs_attention: NeedsAttentionOut | None = None
    recent_sessions: list[RecentSessionItem] = Field(default_factory=list)
    recent_students: list[RecentStudentItem] = Field(default_factory=list)


# ---------------- Global search ----------------
class SearchStudentHit(CamelModel):
    id: str
    name: str
    email: str
    student_number: str
    is_active: bool


class SearchSessionHit(CamelModel):
    session_id: str
    student_id: str
    student_name: str
    case_id: str
    status: str
    started_at: datetime


class SearchResultsOut(CamelModel):
    students: list[SearchStudentHit] = Field(default_factory=list)
    sessions: list[SearchSessionHit] = Field(default_factory=list)


# ---------------- Students ----------------
class StudentListItem(CamelModel):
    id: str
    name: str
    email: str
    student_number: str
    is_active: bool
    has_account: bool
    session_count: int
    completed_count: int
    assessment_count: int
    created_at: datetime
    last_activity_at: datetime | None = None


class PaginatedStudents(CamelModel):
    items: list[StudentListItem]
    total: int
    page: int
    page_size: int


class StudentDetailOut(CamelModel):
    id: str
    name: str
    email: str
    student_number: str
    is_active: bool
    has_account: bool
    account_email: str | None = None
    role: str | None = None
    created_at: datetime
    last_login_at: datetime | None = None
    session_count: int
    completed_count: int
    assessment_count: int


# ---------------- Sessions ----------------
class SessionSummaryOut(CamelModel):
    session_id: str
    student_id: str
    student_name: str
    case_id: str
    case_category: str
    status: str
    locked: bool
    turn_count: int
    student_turn_count: int
    duration_seconds: int | None = None
    has_assessment: bool
    overall_level: str | None = None
    started_at: datetime
    completed_at: datetime | None = None


class PaginatedSessions(CamelModel):
    items: list[SessionSummaryOut]
    total: int
    page: int
    page_size: int


class TranscriptMessageOut(CamelModel):
    id: str
    session_id: str
    speaker: str  # student | patient
    content: str
    source: str | None = None
    turn_index: int
    created_at: datetime


# ---------------- Mutations ----------------
class StudentStatusUpdate(CamelModel):
    is_active: bool


class DeleteConfirmation(CamelModel):
    confirm: str = Field(default="", max_length=16)


class MutationResult(CamelModel):
    success: bool = True
    message: str = ""


# ---------------- Audit log ----------------
class AuditLogOut(CamelModel):
    id: str
    admin_user_id: str | None = None
    admin_email: str
    action_type: str
    record_type: str
    record_id: str
    description: str
    created_at: datetime


class PaginatedAuditLogs(CamelModel):
    items: list[AuditLogOut]
    total: int
    page: int
    page_size: int
