"""Idempotent turn saving: client_turn_id + source on conversation_turns

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-14

"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversation_turns", sa.Column("client_turn_id", sa.String(64), nullable=True))
    op.add_column("conversation_turns", sa.Column("source", sa.String(20), nullable=True))
    # Unique index (works on SQLite and PostgreSQL; NULLs remain distinct,
    # so pre-existing rows without client ids are unaffected).
    op.create_index(
        "uq_turn_client_id", "conversation_turns", ["session_id", "client_turn_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_turn_client_id", table_name="conversation_turns")
    op.drop_column("conversation_turns", "source")
    op.drop_column("conversation_turns", "client_turn_id")
