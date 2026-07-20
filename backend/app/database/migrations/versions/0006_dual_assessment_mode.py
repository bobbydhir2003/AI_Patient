"""add dual assessment mode and referral payload

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("assessment_runs") as batch:
        batch.add_column(sa.Column("assessment_mode", sa.String(length=30), nullable=False, server_default="standard"))
        batch.add_column(sa.Column("referral_payload", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("assessment_runs") as batch:
        batch.drop_column("referral_payload")
        batch.drop_column("assessment_mode")
