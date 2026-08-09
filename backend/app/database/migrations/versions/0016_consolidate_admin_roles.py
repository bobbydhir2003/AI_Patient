"""Consolidate admin roles: super_admin/system_admin -> admin

The application now has exactly TWO roles: ``student`` and ``admin``. Every admin
holds ALL administrative powers, so the former ``super_admin`` (and any legacy
``system_admin``) tier is folded into the normal ``admin`` role.

DATA-ONLY, NON-DESTRUCTIVE:
- Existing accounts whose role is ``super_admin`` (or ``system_admin``) are
  updated in place to ``admin``. They KEEP admin access — no administrator loses
  access because of this change.
- The ``users.is_system_admin`` column is intentionally LEFT IN PLACE for
  backward compatibility with the production schema. Application authorization no
  longer reads it; dropping it is deferred to avoid any risk to the live AWS
  PostgreSQL database. (Uncomment the drop below if/when you want to remove it.)
- No tables are dropped or recreated. All student/admin accounts, patient voice
  settings, API credentials, configuration history, access requests, load-test
  jobs and interview/session/assessment data are preserved.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-08

"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Promote/relabel every legacy elevated role to the single admin role.
    #    Idempotent and safe to re-run: a plain UPDATE with a WHERE filter.
    op.execute(
        sa.text(
            "UPDATE users SET role = 'admin' "
            "WHERE role IN ('super_admin', 'system_admin')"
        )
    )
    # 2) is_system_admin is now a no-op flag. We keep the COLUMN (production
    #    compatibility) but clear it so nothing looks "special". Optional and
    #    harmless — authorization ignores this column entirely.
    op.execute(sa.text("UPDATE users SET is_system_admin = FALSE"))

    # If you later decide to physically remove the legacy column, do it here:
    # op.drop_column("users", "is_system_admin")


def downgrade() -> None:
    # Irreversible by design: the original super_admin/system_admin distinction
    # is not recorded once consolidated, and re-introducing it would recreate the
    # very tiering this migration removes. No-op keeps downgrade safe.
    pass
