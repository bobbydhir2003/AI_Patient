"""ai_usage_events.purpose: distinguish assessment vs interview spend

Additive, non-destructive. Adds a nullable `purpose` column (and its index) so
each recorded AI usage event can be attributed to what produced it
("interview", "assessment_generate", "assessment_verify",
"assessment_correction"). Existing rows keep purpose = NULL.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_usage_events",
        sa.Column("purpose", sa.String(length=40), nullable=True),
    )
    op.create_index(
        "ix_ai_usage_events_purpose", "ai_usage_events", ["purpose"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_ai_usage_events_purpose", table_name="ai_usage_events")
    op.drop_column("ai_usage_events", "purpose")
