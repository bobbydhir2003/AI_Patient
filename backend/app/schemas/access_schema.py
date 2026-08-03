from datetime import datetime

from pydantic import Field, field_validator

from app.schemas.auth import _normalize_email
from app.schemas.base import CamelModel


class AccessRequestIn(CamelModel):
    email: str = Field(min_length=3, max_length=255)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _normalize_email(v)


class AccessRequestResult(CamelModel):
    # PENDING | ALREADY_PENDING | ALREADY_APPROVED  (never leaks other emails)
    result: str
    message: str


class AccessReviewIn(CamelModel):
    note: str = Field(default="", max_length=1000)


class AccessRequestOut(CamelModel):
    id: str
    email: str
    status: str
    requested_at: datetime
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    reviewer_note: str | None = None
