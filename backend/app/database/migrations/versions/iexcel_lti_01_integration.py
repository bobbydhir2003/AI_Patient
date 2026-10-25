"""AI iEXCEL Course 1 integration (LTI 1.3 tool): additive tables + one column.

Branches directly from 0020 (the verified production survey revision), so an
environment at 0020 can target iexcel_lti_01 explicitly without executing the
unrelated 0019 cleanup. The revision id is deliberately non-numeric: other AI SP
branches (e.g. integrate-realtime-cleanup) already use 0022-0027, and a later
merge revision joins the heads without renumbering anything. Creates only new tables and one NULLABLE column on
interview_sessions; no existing row, column, or constraint is changed, and every
existing (direct-login) session keeps context_id = NULL.

Revision ID: iexcel_lti_01
Revises: 0020
Create Date: 2026-10
"""
import sqlalchemy as sa
from alembic import op

revision = "iexcel_lti_01"
down_revision = "0020"
branch_labels = ("iexcel_lti",)
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "course_contexts",
        sa.Column("context_id", sa.String(length=64), primary_key=True),
        sa.Column("issuer", sa.String(length=255), nullable=False),
        sa.Column("deployment_id", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("title", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
    )
    op.create_table(
        "platform_identities",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("issuer", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("last_launch_at", _TS, nullable=False),
        sa.UniqueConstraint("issuer", "subject", name="uq_platform_identities_issuer_subject"),
        sa.UniqueConstraint("issuer", "user_id", name="uq_platform_identities_issuer_user"),
    )
    op.create_index("ix_platform_identities_user_id", "platform_identities", ["user_id"])
    op.create_table(
        "lti_login_states",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("state_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("nonce", sa.String(length=100), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("expires_at", _TS, nullable=False),
        sa.Column("used_at", _TS, nullable=True),
    )
    op.create_index("ix_lti_login_states_expires_at", "lti_login_states", ["expires_at"])
    op.create_table(
        "lti_login_codes",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("code_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "context_id",
            sa.String(length=64),
            sa.ForeignKey("course_contexts.context_id"),
            nullable=False,
        ),
        sa.Column("launch_role", sa.String(length=20), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("expires_at", _TS, nullable=False),
        sa.Column("used_at", _TS, nullable=True),
    )
    op.create_index("ix_lti_login_codes_expires_at", "lti_login_codes", ["expires_at"])
    op.create_table(
        "context_case_assignments",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "context_id",
            sa.String(length=64),
            sa.ForeignKey("course_contexts.context_id"),
            nullable=False,
        ),
        sa.Column("case_id", sa.String(length=50), nullable=False),
        sa.Column("assigned_by", sa.String(length=32), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.UniqueConstraint("context_id", "case_id", name="uq_context_case_assignments"),
    )
    op.create_index(
        "ix_context_case_assignments_context_id", "context_case_assignments", ["context_id"]
    )
    op.create_table(
        "context_members",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "context_id",
            sa.String(length=64),
            sa.ForeignKey("course_contexts.context_id"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("first_launch_at", _TS, nullable=False),
        sa.Column("last_launch_at", _TS, nullable=False),
        sa.UniqueConstraint("context_id", "user_id", "role", name="uq_context_members"),
    )
    op.create_index("ix_context_members_context_id", "context_members", ["context_id"])
    op.create_index("ix_context_members_user_id", "context_members", ["user_id"])
    op.create_table(
        "platform_event_outbox",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("event_id", sa.String(length=36), nullable=False, unique=True),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("context_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", _TS, nullable=False),
        sa.Column("last_error", sa.String(length=120), nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("sent_at", _TS, nullable=True),
    )
    op.create_index("ix_platform_event_outbox_context_id", "platform_event_outbox", ["context_id"])
    op.create_index("ix_platform_event_outbox_session_id", "platform_event_outbox", ["session_id"])
    op.create_index("ix_platform_event_outbox_status", "platform_event_outbox", ["status"])

    with op.batch_alter_table("interview_sessions") as batch:
        batch.add_column(sa.Column("context_id", sa.String(length=64), nullable=True))
    op.create_index("ix_interview_sessions_context_id", "interview_sessions", ["context_id"])


def downgrade() -> None:
    op.drop_index("ix_interview_sessions_context_id", table_name="interview_sessions")
    with op.batch_alter_table("interview_sessions") as batch:
        batch.drop_column("context_id")
    for table in (
        "platform_event_outbox",
        "context_members",
        "context_case_assignments",
        "lti_login_codes",
        "lti_login_states",
        "platform_identities",
        "course_contexts",
    ):
        op.drop_table(table)
