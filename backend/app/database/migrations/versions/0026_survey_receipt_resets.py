"""Create survey_receipt_resets (admin survey-reset history).

Append-only snapshot of a survey receipt at the moment an admin resets it, so the
earlier REDCap record_id (where the pre-reset answers live) is never lost. No data
is backfilled: there were no resets before this table existed.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09
"""
import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "survey_receipt_resets",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "student_id",
            sa.String(length=32),
            sa.ForeignKey("students.id"),
            nullable=False,
        ),
        sa.Column("case_id", sa.String(length=50), nullable=False),
        sa.Column("scope", sa.String(length=10), nullable=False),
        sa.Column("redcap_record_id", sa.String(length=100), nullable=False),
        sa.Column("pre_sync_status", sa.String(length=20), nullable=False),
        sa.Column("pre_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("post_sync_status", sa.String(length=20), nullable=False),
        sa.Column("post_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("overall_status", sa.String(length=20), nullable=False),
        sa.Column(
            "was_owner_case", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("latest_session_id", sa.String(length=32), nullable=True),
        sa.Column("receipt_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reset_by_user_id", sa.String(length=32), nullable=True),
        sa.Column("reset_by_email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_survey_receipt_resets_student_id",
        "survey_receipt_resets",
        ["student_id"],
        unique=False,
    )
    op.create_index(
        "ix_survey_receipt_resets_reset_at",
        "survey_receipt_resets",
        ["reset_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_survey_receipt_resets_reset_at", table_name="survey_receipt_resets")
    op.drop_index("ix_survey_receipt_resets_student_id", table_name="survey_receipt_resets")
    op.drop_table("survey_receipt_resets")
