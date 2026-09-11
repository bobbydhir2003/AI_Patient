"""survey_receipts: case-level Pre/Post survey package (replaces survey_submissions)

Business-rule change: the survey is ONE package per (student, case), with Pre and
Post as its two stages - NOT one row per (session, phase). This migration drops
the never-deployed ``survey_submissions`` table (added in 0020, which reached no
environment beyond local/branch) and creates ``survey_receipts`` with the
case-level ``UNIQUE(student_id, case_id)`` constraint that enforces one package
per case at the database level.

Still additive to real data: ``survey_submissions`` stored only sync/linkage
metadata (never answers), and production was intentionally held at 0018, so no
production survey rows exist to migrate. Answers continue to live ONLY in REDCap.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09
"""
import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0020 always runs immediately before this in the linear chain, so
    # survey_submissions exists here. Drop it (index first) - it stored only
    # sync metadata, never answers, and was never deployed to production.
    op.drop_index("ix_survey_submissions_session_id", table_name="survey_submissions")
    op.drop_table("survey_submissions")

    op.create_table(
        "survey_receipts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "student_id",
            sa.String(length=32),
            sa.ForeignKey("students.id"),
            nullable=False,
        ),
        sa.Column("case_id", sa.String(length=50), nullable=False),
        sa.Column(
            "latest_session_id",
            sa.String(length=32),
            sa.ForeignKey("interview_sessions.id"),
            nullable=True,
        ),
        sa.Column("redcap_record_id", sa.String(length=100), nullable=False),
        sa.Column("pre_sync_status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("pre_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("post_sync_status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("post_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "overall_status", sa.String(length=20), nullable=False, server_default="in_progress"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # The case-level business rule, enforced at the DB level: at most ONE
        # survey package per (student, case). Inlined (not a separate ALTER) so
        # it is created identically on PostgreSQL and SQLite.
        sa.UniqueConstraint("student_id", "case_id", name="uq_survey_receipts_student_case"),
    )
    op.create_index(
        "ix_survey_receipts_student_id", "survey_receipts", ["student_id"], unique=False
    )
    op.create_index("ix_survey_receipts_case_id", "survey_receipts", ["case_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_survey_receipts_case_id", table_name="survey_receipts")
    op.drop_index("ix_survey_receipts_student_id", table_name="survey_receipts")
    op.drop_table("survey_receipts")

    # Recreate survey_submissions exactly as 0020 left it, so downgrade is a
    # faithful inverse of upgrade.
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
        sa.UniqueConstraint("session_id", "phase", name="uq_survey_submissions_session_phase"),
    )
    op.create_index(
        "ix_survey_submissions_session_id", "survey_submissions", ["session_id"], unique=False
    )
