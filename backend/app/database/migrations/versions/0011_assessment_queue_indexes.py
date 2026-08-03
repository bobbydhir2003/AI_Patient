"""Assessment queue indexes (B7)

Adds two indexes on assessment_runs to support the durable, database-backed
assessment queue:
- ix_assessment_runs_status: cheap status aggregates for the dashboard and the
  worker's PENDING poll.
- uq_active_assessment_per_session: PARTIAL UNIQUE index enforcing at most one
  ACTIVE (PENDING/PROCESSING/VERIFYING) run per session. This is the atomic guard
  against the read-then-create double-submit race (B2): a concurrent second
  enqueue hits an IntegrityError and reuses the existing run instead of spending
  twice. Supported on both SQLite (3.8+) and PostgreSQL.

Non-destructive: creates indexes only; no data change. If duplicate active runs
already exist, resolve them before applying (see docs/TRAFFIC.md).

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-02

"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_ACTIVE = "status IN ('PENDING','PROCESSING','VERIFYING')"


def upgrade() -> None:
    op.create_index("ix_assessment_runs_status", "assessment_runs", ["status"])
    op.create_index(
        "uq_active_assessment_per_session",
        "assessment_runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE),
        sqlite_where=sa.text(_ACTIVE),
    )


def downgrade() -> None:
    op.drop_index("uq_active_assessment_per_session", table_name="assessment_runs")
    op.drop_index("ix_assessment_runs_status", table_name="assessment_runs")
