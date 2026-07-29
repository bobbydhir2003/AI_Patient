"""Admin notification read-state

Adds users.notifications_read_at so the notification badge can show a REAL
unread count (admin-activity events newer than this timestamp), not a hardcoded
number. Non-destructive; existing users default to NULL (everything unread until
first "mark all read").

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-28

"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("notifications_read_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("notifications_read_at")
