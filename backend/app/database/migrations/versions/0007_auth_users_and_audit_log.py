"""Authentication users, audit log, and student account fields

Adds the `users` and `audit_logs` tables and two columns to `students`
(email, is_active). Existing rows are preserved: students keep all their data
and default to email='' and is_active=true, which the admin panel treats as an
active profile with no login account yet.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-20

"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- extend existing students (non-destructive; defaults backfill rows) ---
    with op.batch_alter_table("students") as batch:
        batch.add_column(sa.Column("email", sa.String(255), nullable=False, server_default=""))
        batch.add_column(
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true())
        )

    # --- users (authentication accounts) ---
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("full_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("student_number", sa.String(100), nullable=False, server_default=""),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="student"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("student_id", sa.String(32), sa.ForeignKey("students.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_student_id", "users", ["student_id"])

    # --- audit_logs (append-only admin action trail) ---
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("admin_user_id", sa.String(32), nullable=True),
        sa.Column("admin_email", sa.String(255), nullable=False, server_default=""),
        sa.Column("action_type", sa.String(50), nullable=False),
        sa.Column("record_type", sa.String(40), nullable=False),
        sa.Column("record_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_admin_user_id", "audit_logs", ["admin_user_id"])
    op.create_index("ix_audit_logs_action_type", "audit_logs", ["action_type"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_admin_user_id", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("ix_users_student_id", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")

    with op.batch_alter_table("students") as batch:
        batch.drop_column("is_active")
        batch.drop_column("email")
