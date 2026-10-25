"""Merge the AI iEXCEL LTI branch with the existing 0021 head.

No schema operations. An environment that intentionally applies every branch
reaches a single head; an environment at 0020 can instead target iexcel_lti_01
directly (which does not execute 0019 or 0021).

Revision ID: iexcel_lti_02
Revises: 0021, iexcel_lti_01
Create Date: 2026-10
"""
revision = "iexcel_lti_02"
down_revision = ("0021", "iexcel_lti_01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
