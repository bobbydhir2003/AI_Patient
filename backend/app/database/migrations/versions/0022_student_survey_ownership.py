"""Add student-level global survey ownership (one survey PACKAGE per student).

The survey rule changes from one package per (student, case) to ONE package per
student GLOBALLY. The first case whose Pre stage successfully established the
package becomes that student's permanent survey owner; every other case is gated
to "already submitted / skip".

This revision is ADDITIVE and non-destructive:
  - adds two nullable columns to ``students``:
        survey_owner_case_id  (the owning case slug, NULL = not started)
        survey_completed_at   (set when the owning package completed)
  - backfills them deterministically from the EXISTING ``survey_receipts`` rows.
  - NEVER deletes a receipt and NEVER rewrites REDCap data.

We deliberately do NOT add a global UNIQUE index on survey_receipts: existing
students may already hold multiple per-case receipts (the old rule), so such an
index could not be created. Go-forward uniqueness is enforced in the service via
a serialized ownership claim (SELECT ... FOR UPDATE on the student row).

Branches directly from 0020 (the production-safe survey branch), mirroring how
0020 branched from 0018, so production can apply this by targeting 0022 without
executing the unrelated 0019 voice cleanup. 0023 merges this with 0021 so dev's
`alembic upgrade head` remains single-headed.

Deterministic backfill precedence (based on SUCCESSFUL survey activity, not mere
row existence):
  1. If the student has any COMPLETED receipt -> owner = the earliest completed
     package (by post_synced_at, then created_at, then case_id); also set
     survey_completed_at from that receipt.
  2. Else if any receipt has a SUCCESSFUL Pre (pre_sync_status in
     'synced'/'skipped') -> owner = the earliest such receipt; leave
     survey_completed_at NULL (IN_PROGRESS).
  3. Else (only failed/pending Pre attempts) -> leave owner NULL (NOT_STARTED);
     a failed-only receipt never establishes ownership.
All receipts are preserved unchanged in every case. Timestamps are coalesced to
created_at (NOT NULL) for a total, deterministic ordering within a dialect.

Revision ID: 0022
Revises: 0020
Create Date: 2026-09
"""
from collections import defaultdict

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0020"
branch_labels = None
depends_on = None

_PRE_DONE = ("synced", "skipped")


def _sort_key_completed(row):
    return (row.post_synced_at or row.updated_at or row.created_at, row.created_at, row.case_id)


def _sort_key_pre(row):
    return (row.pre_synced_at or row.created_at, row.created_at, row.case_id)


def upgrade() -> None:
    op.add_column(
        "students",
        sa.Column("survey_owner_case_id", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "students",
        sa.Column("survey_completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT student_id, case_id, pre_sync_status, post_sync_status, "
            "overall_status, pre_synced_at, post_synced_at, created_at, updated_at "
            "FROM survey_receipts"
        )
    ).fetchall()

    by_student = defaultdict(list)
    for row in rows:
        by_student[row.student_id].append(row)

    for student_id, receipts in by_student.items():
        owner_case_id = None
        completed_at = None

        completed = [r for r in receipts if r.overall_status == "completed"]
        if completed:
            chosen = sorted(completed, key=_sort_key_completed)[0]
            owner_case_id = chosen.case_id
            completed_at = chosen.post_synced_at or chosen.updated_at or chosen.created_at
        else:
            pre_ok = [r for r in receipts if r.pre_sync_status in _PRE_DONE]
            if pre_ok:
                owner_case_id = sorted(pre_ok, key=_sort_key_pre)[0].case_id

        if owner_case_id is not None:
            bind.execute(
                sa.text(
                    "UPDATE students SET survey_owner_case_id = :owner, "
                    "survey_completed_at = :completed_at WHERE id = :student_id"
                ),
                {"owner": owner_case_id, "completed_at": completed_at, "student_id": student_id},
            )


def downgrade() -> None:
    op.drop_column("students", "survey_completed_at")
    op.drop_column("students", "survey_owner_case_id")
