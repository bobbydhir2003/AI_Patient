"""AI usage & cost telemetry: ai_usage_events table

Additive, non-destructive. Creates the source-of-truth table for per-request AI
provider usage (OpenAI tokens, ElevenLabs characters) with the historical unit
prices used at record time, plus the indexes the dashboard aggregation relies on.

Revision ID: 0017
Revises: 0016
Create Date: 2025-01
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_usage_events",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("session_id", sa.String(length=32), nullable=True),
        sa.Column("student_id", sa.String(length=32), nullable=True),
        sa.Column("case_id", sa.String(length=50), nullable=True),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("model", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("characters_generated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("audio_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("input_unit_price", sa.Float(), nullable=False, server_default="0"),
        sa.Column("output_unit_price", sa.Float(), nullable=False, server_default="0"),
        sa.Column("provider_unit_price", sa.Float(), nullable=False, server_default="0"),
        sa.Column("pricing_version", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("provider_request_id", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ai_usage_events_session_id", "ai_usage_events", ["session_id"])
    op.create_index("ix_ai_usage_events_student_id", "ai_usage_events", ["student_id"])
    op.create_index("ix_ai_usage_events_case_id", "ai_usage_events", ["case_id"])
    op.create_index("ix_ai_usage_events_provider", "ai_usage_events", ["provider"])
    op.create_index("ix_ai_usage_events_created_at", "ai_usage_events", ["created_at"])
    op.create_index("ix_ai_usage_events_estimated_cost_usd", "ai_usage_events", ["estimated_cost_usd"])
    op.create_index("ix_ai_usage_provider_created", "ai_usage_events", ["provider", "created_at"])
    op.create_index("ix_ai_usage_session_provider", "ai_usage_events", ["session_id", "provider"])


def downgrade() -> None:
    op.drop_index("ix_ai_usage_session_provider", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_provider_created", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_estimated_cost_usd", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_created_at", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_provider", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_case_id", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_student_id", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_session_id", table_name="ai_usage_events")
    op.drop_table("ai_usage_events")
