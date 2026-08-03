"""User account status + review audit fields (D1)

Separates the approval lifecycle (account_status) from role. Adds:
- users.account_status  (PENDING | ACTIVE | REJECTED | DISABLED)
- users.reviewed_by / reviewed_at / review_note

BACKWARD COMPATIBILITY: existing users are set to ACTIVE so already-approved
accounts keep working. is_active is preserved and kept in lock-step with
account_status by the application (ACTIVE => is_active true). Non-destructive.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-03

"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("account_status", sa.String(length=20), nullable=False, server_default="ACTIVE"))
        batch.add_column(sa.Column("reviewed_by", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("review_note", sa.String(length=1000), nullable=True))
    op.create_index("ix_users_account_status", "users", ["account_status"])
    # Existing inactive (archived) accounts become DISABLED rather than ACTIVE.
    op.execute("UPDATE users SET account_status = 'DISABLED' WHERE is_active = false")


def downgrade() -> None:
    op.drop_index("ix_users_account_status", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("review_note")
        batch.drop_column("reviewed_at")
        batch.drop_column("reviewed_by")
        batch.drop_column("account_status")
