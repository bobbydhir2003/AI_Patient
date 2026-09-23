"""Create assessment_view_sessions (silent active assessment-viewing time).

One aggregate row per interview session. Time is credited server-side from
last_heartbeat_at (the client never sends a duration). ON DELETE CASCADE on the
interview_sessions FK keeps timing rows from ever orphaning when a session /
student tree is purged; the deletion services also delete them explicitly.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09
"""
import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assessment_view_sessions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "interview_session_id",
            sa.String(length=32),
            sa.ForeignKey("interview_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assessment_run_id",
            sa.String(length=32),
            sa.ForeignKey("assessment_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("student_id", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.String(length=50), nullable=False),
        sa.Column("active_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_viewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "interview_session_id", name="uq_assessment_view_interview_session"
        ),
    )
    op.create_index(
        "ix_assessment_view_sessions_interview_session_id",
        "assessment_view_sessions",
        ["interview_session_id"],
        unique=False,
    )
    op.create_index(
        "ix_assessment_view_sessions_student_id",
        "assessment_view_sessions",
        ["student_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assessment_view_sessions_student_id", table_name="assessment_view_sessions"
    )
    op.drop_index(
        "ix_assessment_view_sessions_interview_session_id",
        table_name="assessment_view_sessions",
    )
    op.drop_table("assessment_view_sessions")
