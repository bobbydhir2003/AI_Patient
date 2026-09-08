"""Drop patient_voice_settings (ElevenLabs per-case voice overrides).

The interactive patient voice is now produced by OpenAI Realtime, whose voice is
resolved per case in app/livekit_agent/realtime_patient_configs.py. The old
ElevenLabs typed-chat TTS path and its editable per-case voice overrides have
been removed, so this table is no longer read or written by any runtime path.

This is a forward-only cleanup: upgrade() drops the table. downgrade() recreates
it (empty) with its original schema so the migration is reversible, but no data
is restored (the overrides were ElevenLabs-only and no longer have a consumer).

Revision ID: 0019
Revises: 0018
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the unique constraint first where the backend supports it, then the
    # table. SQLite (used in tests) recreates the table on constraint ops, so we
    # guard the constraint drop and rely on drop_table for the actual removal.
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        try:
            op.drop_constraint("uq_voice_case_speaker", "patient_voice_settings", type_="unique")
        except Exception:
            pass
    op.drop_table("patient_voice_settings")


def downgrade() -> None:
    op.create_table(
        "patient_voice_settings",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("case_id", sa.String(50), nullable=False),
        sa.Column("speaker_id", sa.String(30), nullable=False, server_default="patient"),
        sa.Column("display_name", sa.String(120), nullable=False, server_default=""),
        sa.Column("voice_id", sa.String(120), nullable=False, server_default=""),
        sa.Column("voice_name", sa.String(120), nullable=False, server_default=""),
        sa.Column("model_id", sa.String(60), nullable=False, server_default=""),
        sa.Column("stability", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("similarity_boost", sa.Float(), nullable=False, server_default="0.75"),
        sa.Column("style", sa.Float(), nullable=False, server_default="0.1"),
        sa.Column("speed", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("speaker_boost", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("preview_text", sa.String(255), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_by", sa.String(255), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint(
        "uq_voice_case_speaker", "patient_voice_settings", ["case_id", "speaker_id"]
    )
