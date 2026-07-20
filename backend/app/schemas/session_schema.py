from datetime import datetime

from pydantic import Field

from app.schemas.base import CamelModel
from app.schemas.interview_schema import MessageOut


class SessionCreateRequest(CamelModel):
    student_name: str = Field(min_length=1, max_length=200)
    student_id: str = Field(default="", max_length=100)
    case_id: str


class SessionResponse(CamelModel):
    session_id: str
    case_id: str
    case_category: str = "standard"
    assessment_capabilities: list[str] = Field(default_factory=lambda: ["standard_interview"])
    protected_reference_version: str = ""
    student_name: str
    student_id: str = ""
    status: str
    locked: bool
    started_at: datetime
    completed_at: datetime | None = None
    messages: list[MessageOut] = Field(default_factory=list)
