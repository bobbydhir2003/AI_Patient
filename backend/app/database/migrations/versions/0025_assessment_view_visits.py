"""Per-visit assessment viewing (reliable view counting).

Adds assessment_view_visits (one row per real page visit, keyed by a client
visit_id) and assessment_view_sessions.last_viewed_at. Backfills ONE synthetic
"legacy" visit per existing summary row so historical total active time is
preserved exactly (historical per-visit counts cannot be reconstructed, so we do
NOT fabricate multiple legacy visits).

Each visit also records its SOURCE (initial_assessment | student_dashboard, set
once by the client when the visit is created; "unknown" for an old client that
sent none). The synthetic backfilled visit is source="legacy" - it is also the
server default, so any row inserted without a source is never mislabelled.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09
"""
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assessment_view_visits",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("visit_id", sa.String(length=64), nullable=False),
        sa.Column(
            "interview_session_id",
            sa.String(length=32),
            sa.ForeignKey("interview_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assessment_run_id",
            sa.String(length=32),
            sa.ForeignKey("assessment_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("student_id", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.String(length=50), nullable=False),
        sa.Column(
            "source", sa.String(length=32), nullable=False, server_default="legacy"
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "interview_session_id", "visit_id", name="uq_assessment_view_visit"
        ),
    )
    op.create_index(
        "ix_assessment_view_visits_interview_session_id",
        "assessment_view_visits",
        ["interview_session_id"],
        unique=False,
    )
    op.create_index(
        "ix_assessment_view_visits_student_id",
        "assessment_view_visits",
        ["student_id"],
        unique=False,
    )

    op.add_column(
        "assessment_view_sessions",
        sa.Column("last_viewed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ---- Backfill: one synthetic "legacy" visit per existing summary row, and
    # set last_viewed_at = last_heartbeat_at. Preserves total active time exactly.
    bind = op.get_bind()
    summaries = sa.table(
        "assessment_view_sessions",
        sa.column("interview_session_id", sa.String),
        sa.column("assessment_run_id", sa.String),
        sa.column("user_id", sa.String),
        sa.column("student_id", sa.String),
        sa.column("case_id", sa.String),
        sa.column("active_seconds", sa.Integer),
        sa.column("first_viewed_at", sa.DateTime(timezone=True)),
        sa.column("last_heartbeat_at", sa.DateTime(timezone=True)),
    )
    visits = sa.table(
        "assessment_view_visits",
        sa.column("id", sa.String),
        sa.column("visit_id", sa.String),
        sa.column("interview_session_id", sa.String),
        sa.column("assessment_run_id", sa.String),
        sa.column("user_id", sa.String),
        sa.column("student_id", sa.String),
        sa.column("case_id", sa.String),
        sa.column("source", sa.String),
        sa.column("started_at", sa.DateTime(timezone=True)),
        sa.column("last_heartbeat_at", sa.DateTime(timezone=True)),
        sa.column("active_seconds", sa.Integer),
        sa.column("ended_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(timezone.utc)
    rows = bind.execute(sa.select(summaries)).mappings().all()
    for r in rows:
        bind.execute(
            visits.insert().values(
                id=uuid.uuid4().hex,
                visit_id="legacy",
                interview_session_id=r["interview_session_id"],
                assessment_run_id=r["assessment_run_id"],
                user_id=r["user_id"],
                student_id=r["student_id"],
                case_id=r["case_id"],
                source="legacy",
                started_at=r["first_viewed_at"],
                last_heartbeat_at=r["last_heartbeat_at"],
                active_seconds=r["active_seconds"],
                ended_at=None,
                created_at=now,
                updated_at=now,
            )
        )
    # last_viewed_at defaults to the summary's last_heartbeat_at for existing rows.
    bind.execute(
        sa.text(
            "UPDATE assessment_view_sessions SET last_viewed_at = last_heartbeat_at"
        )
    )


def downgrade() -> None:
    op.drop_column("assessment_view_sessions", "last_viewed_at")
    op.drop_index(
        "ix_assessment_view_visits_student_id", table_name="assessment_view_visits"
    )
    op.drop_index(
        "ix_assessment_view_visits_interview_session_id",
        table_name="assessment_view_visits",
    )
    op.drop_table("assessment_view_visits")
