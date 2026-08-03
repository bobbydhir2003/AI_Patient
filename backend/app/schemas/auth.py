import re
from datetime import datetime

from pydantic import Field, field_validator

from app.schemas.base import CamelModel

# Deliberately permissive but enough to reject obviously invalid addresses.
# Avoids pulling in the optional `email-validator` dependency.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_email(value: str) -> str:
    value = (value or "").strip().lower()
    if not _EMAIL_RE.match(value):
        raise ValueError("A valid email address is required.")
    return value


class RegisterRequest(CamelModel):
    full_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    student_number: str = Field(default="", max_length=100)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _normalize_email(v)


class LoginRequest(CamelModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _normalize_email(v)


class UserOut(CamelModel):
    id: str
    full_name: str
    email: str
    student_number: str
    role: str
    account_status: str = "ACTIVE"
    is_active: bool
    student_id: str | None = None
    created_at: datetime
    last_login_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None


class RegisterResult(CamelModel):
    status: str  # "pending"
    message: str


class RoleChangeIn(CamelModel):
    role: str = Field(pattern="^(student|admin|super_admin)$")


class ReviewNoteIn(CamelModel):
    note: str = Field(default="", max_length=1000)


class TokenResponse(CamelModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    user: UserOut


class LogoutResponse(CamelModel):
    success: bool = True
