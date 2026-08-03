"""Load & capacity testing: load_test_jobs table + users.is_load_test (J1)

Adds:
- users.is_load_test  (isolation flag for dedicated virtual-student accounts;
  their sessions/turns are never counted as real academic records)
- load_test_jobs       (metadata + computed summary/capacity results for each
  load test run; only summary metadata is persisted, never raw time-series)

Non-destructive: existing users default to is_load_test = false.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-03

"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("is_load_test", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.create_index("ix_users_is_load_test", "users", ["is_load_test"])

    op.create_table(
        "load_test_jobs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_by", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("environment", sa.String(length=20), nullable=False, server_default="local"),
        sa.Column("test_type", sa.String(length=40), nullable=False, server_default="smoke"),
        sa.Column("provider_mode", sa.String(length=32), nullable=False, server_default="SIMULATED_AI"),
        sa.Column("target_users", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ramp_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("worker_identifier", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("results", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_load_test_jobs_created_by", "load_test_jobs", ["created_by"])
    op.create_index("ix_load_test_jobs_status", "load_test_jobs", ["status"])
    op.create_index("ix_load_test_jobs_created_at", "load_test_jobs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_load_test_jobs_created_at", table_name="load_test_jobs")
    op.drop_index("ix_load_test_jobs_status", table_name="load_test_jobs")
    op.drop_index("ix_load_test_jobs_created_by", table_name="load_test_jobs")
    op.drop_table("load_test_jobs")
    op.drop_index("ix_users_is_load_test", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("is_load_test")
