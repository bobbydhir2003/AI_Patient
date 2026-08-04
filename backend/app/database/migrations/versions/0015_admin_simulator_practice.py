"""Admin simulator access: system-admin flag + practice-session isolation

Adds three additive, non-destructive boolean columns so promoted admins can use
the patient simulator without polluting student analytics, and so the seeded
system admin can be distinguished from a promoted one:

- users.is_system_admin            (default false)
- students.is_practice             (default false)
- interview_sessions.is_practice   (default false)

BACKWARD COMPATIBILITY: every existing row defaults to false, so all current
students, sessions and admins behave exactly as before. No data is modified or
deleted.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-04

"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ADD COLUMN (no batch/table-rebuild). Adding a NOT NULL column with a
    # constant server_default is supported natively by both SQLite and PostgreSQL
    # and is far lighter than a batch table copy, which is important on networked
    # / mounted filesystems where a full rebuild can fail with a disk I/O error.
    op.add_column(
        "users",
        sa.Column("is_system_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "students",
        sa.Column("is_practice", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "interview_sessions",
        sa.Column("is_practice", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_students_is_practice", "students", ["is_practice"])
    op.create_index("ix_interview_sessions_is_practice", "interview_sessions", ["is_practice"])


def downgrade() -> None:
    op.drop_index("ix_interview_sessions_is_practice", table_name="interview_sessions")
    op.drop_index("ix_students_is_practice", table_name="students")
    op.drop_column("interview_sessions", "is_practice")
    op.drop_column("students", "is_practice")
    op.drop_column("users", "is_system_admin")
