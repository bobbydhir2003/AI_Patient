"""Merge the survey-ownership branch (0022) with the prior merge head (0021).

No schema operations. Like 0021, this only keeps `alembic upgrade head`
single-headed for environments that intentionally apply both branches. Production
targets 0022 explicitly for the global-survey release; doing so does not execute
0019, 0021, or this merge revision.

Revision ID: 0023
Revises: 0021, 0022
Create Date: 2026-09
"""
revision = "0023"
down_revision = ("0021", "0022")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
