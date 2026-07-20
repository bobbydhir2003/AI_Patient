"""Turn generation metadata + session active topic

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-14

"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversation_turns", sa.Column("model_name", sa.String(length=100), nullable=True))
    op.add_column("conversation_turns", sa.Column("prompt_version", sa.String(length=20), nullable=True))
    op.add_column("conversation_turns", sa.Column("facts_used", sa.Text(), nullable=True))
    op.add_column("conversation_turns", sa.Column("response_type", sa.String(length=30), nullable=True))
    op.add_column("conversation_turns", sa.Column("validation_status", sa.String(length=20), nullable=True))
    op.add_column("interview_sessions", sa.Column("active_topic", sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column("interview_sessions", "active_topic")
    op.drop_column("conversation_turns", "validation_status")
    op.drop_column("conversation_turns", "response_type")
    op.drop_column("conversation_turns", "facts_used")
    op.drop_column("conversation_turns", "prompt_version")
    op.drop_column("conversation_turns", "model_name")
