"""Initial tables: students, interview_sessions, conversation_turns

Revision ID: 0001
Revises:
Create Date: 2026-07-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "students",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("student_number", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "interview_sessions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("student_id", sa.String(length=32), sa.ForeignKey("students.id"), nullable=False),
        sa.Column("case_id", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("disclosed_fact_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_interview_sessions_student_id", "interview_sessions", ["student_id"])
    op.create_index("ix_interview_sessions_case_id", "interview_sessions", ["case_id"])
    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("session_id", sa.String(length=32), sa.ForeignKey("interview_sessions.id"), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "turn_index", name="uq_turn_per_session"),
    )
    op.create_index("ix_conversation_turns_session_id", "conversation_turns", ["session_id"])


def downgrade() -> None:
    op.drop_table("conversation_turns")
    op.drop_table("interview_sessions")
    op.drop_table("students")
