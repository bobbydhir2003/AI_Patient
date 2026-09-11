"""Merge the independent voice-cleanup and survey-schema branches.

This revision performs no schema operations. It records that both independent
branches are present when an environment intentionally applies both of them.
Production must target revision 0020 explicitly for the survey release; doing
so does not execute 0019 or this merge revision.

Revision ID: 0021
Revises: 0019, 0020
Create Date: 2026-09
"""
revision = "0021"
down_revision = ("0019", "0020")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
