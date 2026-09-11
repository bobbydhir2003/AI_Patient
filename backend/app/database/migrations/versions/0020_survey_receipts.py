"""Create the final case-level survey_receipts table.

This is a production-safe survey branch directly from 0018. Production is
verified at 0018, has no survey tables/data, and must not execute the unrelated
0019 patient_voice_settings cleanup. Applying this revision explicitly creates
only survey_receipts and its indexes; patient_voice_settings is untouched.

Survey answers remain exclusively in REDCap. This table stores only linkage,
sync status, and completion metadata, with one package per student + case.

Revision ID: 0020
Revises: 0018
Create Date: 2026-09
"""
import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0018"
branch_labels = ("survey_receipts",)
depends_on = None


def upgrade() -> None:
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
        sa.Column(
            "pre_sync_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("pre_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "post_sync_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("post_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "overall_status",
            sa.String(length=20),
            nullable=False,
            server_default="in_progress",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "student_id",
            "case_id",
            name="uq_survey_receipts_student_case",
        ),
    )
    op.create_index(
        "ix_survey_receipts_student_id",
        "survey_receipts",
        ["student_id"],
        unique=False,
    )
    op.create_index(
        "ix_survey_receipts_case_id",
        "survey_receipts",
        ["case_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_survey_receipts_case_id", table_name="survey_receipts")
    op.drop_index("ix_survey_receipts_student_id", table_name="survey_receipts")
    op.drop_table("survey_receipts")
