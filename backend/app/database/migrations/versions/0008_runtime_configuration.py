"""Runtime configuration: editable AI/voice settings + encrypted credentials

Adds api_credentials, system_settings, patient_voice_settings,
configuration_history, and an interview_sessions.config_snapshot column. All
non-destructive; existing rows are preserved. Secrets are stored only as
encrypted tokens (application-level Fernet), never plaintext.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-28

"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_credentials",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("service", sa.String(30), nullable=False),
        sa.Column("encrypted_secret", sa.Text(), nullable=False, server_default=""),
        sa.Column("masked_value", sa.String(60), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_test_status", sa.String(20), nullable=False, server_default="never"),
        sa.Column("last_test_message", sa.String(255), nullable=False, server_default=""),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint("uq_api_credentials_service", "api_credentials", ["service"])

    op.create_table(
        "system_settings",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("category", sa.String(30), nullable=False, server_default=""),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("value_type", sa.String(20), nullable=False, server_default="str"),
        sa.Column("is_secret", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("apply_mode", sa.String(20), nullable=False, server_default="immediate"),
        sa.Column("description", sa.String(255), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(255), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint("uq_system_settings_key", "system_settings", ["key"])

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

    op.create_table(
        "configuration_history",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("configuration_type", sa.String(30), nullable=False),
        sa.Column("configuration_key", sa.String(120), nullable=False, server_default=""),
        sa.Column("entity_id", sa.String(120), nullable=False, server_default=""),
        sa.Column("previous_value", sa.Text(), nullable=False, server_default=""),
        sa.Column("new_value", sa.Text(), nullable=False, server_default=""),
        sa.Column("changed_by", sa.String(255), nullable=False, server_default=""),
        sa.Column("change_reason", sa.String(255), nullable=False, server_default=""),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_config_history_type", "configuration_history", ["configuration_type"])
    op.create_index("ix_config_history_changed_at", "configuration_history", ["changed_at"])

    with op.batch_alter_table("interview_sessions") as batch:
        batch.add_column(
            sa.Column("config_snapshot", sa.Text(), nullable=False, server_default="{}")
        )


def downgrade() -> None:
    with op.batch_alter_table("interview_sessions") as batch:
        batch.drop_column("config_snapshot")
    op.drop_table("configuration_history")
    op.drop_table("patient_voice_settings")
    op.drop_table("system_settings")
    op.drop_table("api_credentials")
