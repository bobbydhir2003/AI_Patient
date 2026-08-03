"""Email access requests (C5)

Adds the access_requests table backing the admin-gated email access flow. One row
per normalized email (unique), with review audit fields. Non-destructive: creates
a new table only; no existing data is touched. Registration behavior is unchanged
unless REQUIRE_ACCESS_APPROVAL=true.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-03

"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_requests",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewer_note", sa.Text(), nullable=True),
    )
    op.create_index("ix_access_requests_email", "access_requests", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_access_requests_email", table_name="access_requests")
    op.drop_table("access_requests")
