from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import User


class UserRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, user_id: str) -> User | None:
        return self.db.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        # Emails are matched case-insensitively; they are stored normalized.
        stmt = select(User).where(func.lower(User.email) == email.strip().lower())
        return self.db.execute(stmt).scalars().first()

    def get_by_student_id(self, student_id: str) -> User | None:
        stmt = select(User).where(User.student_id == student_id)
        return self.db.execute(stmt).scalars().first()

    def create(
        self,
        *,
        email: str,
        password_hash: str,
        full_name: str = "",
        student_number: str = "",
        role: str = "student",
        student_id: str | None = None,
        is_active: bool = True,
    ) -> User:
        user = User(
            email=email.strip().lower(),
            password_hash=password_hash,
            full_name=full_name.strip(),
            student_number=student_number.strip(),
            role=role,
            student_id=student_id,
            is_active=is_active,
        )
        self.db.add(user)
        self.db.flush()
        return user

    def touch_last_login(self, user: User) -> None:
        user.last_login_at = datetime.now(timezone.utc)
        self.db.flush()
