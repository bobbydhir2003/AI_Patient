"""survey_submissions: Pre/Post experience survey linkage table

Additive, non-destructive. Adds a small linkage/reliability table that records
that a given interview session's Pre- or Post-experience survey was submitted,
under which REDCap record_id (the student's NUID), and whether the REDCap import
succeeded. It does NOT store the survey answers themselves (those live in
REDCap). The UNIQUE(session_id, phase) constraint prevents duplicate REDCap
records on retries/double-clicks.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09
"""
import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "survey_submissions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=32),
            sa.ForeignKey("interview_sessions.id"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(length=10), nullable=False),
        sa.Column("redcap_record_id", sa.String(length=100), nullable=False),
        sa.Column("sync_status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        # Inlined (not a separate ALTER) so it is created identically on both
        # PostgreSQL and SQLite: at most one pre and one post row per session.
        sa.UniqueConstraint("session_id", "phase", name="uq_survey_submissions_session_phase"),
    )
    op.create_index(
        "ix_survey_submissions_session_id", "survey_submissions", ["session_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_survey_submissions_session_id", table_name="survey_submissions")
    op.drop_table("survey_submissions")
