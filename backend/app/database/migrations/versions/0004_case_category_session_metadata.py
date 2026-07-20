"""Session case category + assessment capability metadata

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-14

"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "interview_sessions",
        sa.Column("case_category", sa.String(20), nullable=False, server_default="standard"),
    )
    op.add_column(
        "interview_sessions",
        sa.Column(
            "assessment_capabilities", sa.Text(), nullable=False,
            server_default='["standard_interview"]',
        ),
    )
    op.add_column(
        "interview_sessions",
        sa.Column("protected_reference_version", sa.String(20), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("interview_sessions", "protected_reference_version")
    op.drop_column("interview_sessions", "assessment_capabilities")
    op.drop_column("interview_sessions", "case_category")
