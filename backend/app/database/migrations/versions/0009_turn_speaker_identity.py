"""Multi-participant speaker identity on conversation turns

Adds conversation_turns.speaker_id + speaker_label so a case with more than one
participant (e.g. Camden + his mother) records WHO spoke. Non-destructive:
existing patient turns default to speaker_id='patient'.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-28

"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversation_turns") as batch:
        batch.add_column(sa.Column("speaker_id", sa.String(30), nullable=False, server_default="patient"))
        batch.add_column(sa.Column("speaker_label", sa.String(120), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("conversation_turns") as batch:
        batch.drop_column("speaker_label")
        batch.drop_column("speaker_id")
