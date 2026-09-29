"""Independent Post-survey tracking (admin "Reset Post" reopens Post to any case).

ADDITIVE and non-destructive. Adds four nullable columns:
  students.survey_post_case_id       case whose receipt holds the CURRENT Post
  students.survey_post_reopened_at   set by an admin Post reset: Post may then be
                                     collected from any case (first success wins)
  survey_receipts.pre_redcap_record_id / post_redcap_record_id
                                     REDCap record holding each stage's answers

Deterministic backfill (no receipt/snapshot is deleted, no REDCap write):
  1. pre/post_redcap_record_id = the record_id in effect when that stage synced:
     the redcap_record_id of the EARLIEST pre/post reset snapshot for the same
     (student, case) taken after the stage's synced_at, else the receipt's
     current redcap_record_id. (Admin resets rotate the receipt's record_id and
     snapshot the old one, so this recovers where each stage really lives.)
  2. survey_post_case_id = survey_owner_case_id when the owner receipt's Post is
     done (synced/skipped). NULL otherwise - the runtime falls back to the owner,
     so every legacy student behaves exactly as before.
  3. survey_post_reopened_at = the latest 'post' reset snapshot time for students
     whose owner receipt currently has Pre done and Post NOT done after an admin
     Post reset (i.e. a pending Post retake), so that pending retake follows the
     new Post-reset semantics.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09
"""
from collections import defaultdict

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

_DONE = ("synced", "skipped")


def _record_at(receipt, synced_at, snapshots):
    """The record_id in effect at ``synced_at`` for this receipt."""
    later = [
        s for s in snapshots
        if s.scope in ("pre", "post") and s.reset_at is not None and s.reset_at > synced_at
    ]
    if later:
        return min(later, key=lambda s: s.reset_at).redcap_record_id
    return receipt.redcap_record_id


def upgrade() -> None:
    op.add_column("students", sa.Column("survey_post_case_id", sa.String(length=50), nullable=True))
    op.add_column(
        "students",
        sa.Column("survey_post_reopened_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "survey_receipts", sa.Column("pre_redcap_record_id", sa.String(length=100), nullable=True)
    )
    op.add_column(
        "survey_receipts", sa.Column("post_redcap_record_id", sa.String(length=100), nullable=True)
    )

    bind = op.get_bind()
    receipts = bind.execute(
        sa.text(
            "SELECT id, student_id, case_id, redcap_record_id, pre_sync_status, "
            "pre_synced_at, post_sync_status, post_synced_at FROM survey_receipts"
        )
    ).fetchall()
    snaps = bind.execute(
        sa.text(
            "SELECT student_id, case_id, scope, redcap_record_id, reset_at "
            "FROM survey_receipt_resets"
        )
    ).fetchall()
    snaps_by_key = defaultdict(list)
    for s in snaps:
        snaps_by_key[(s.student_id, s.case_id)].append(s)

    # 1. Per-stage record ids.
    for r in receipts:
        key_snaps = snaps_by_key.get((r.student_id, r.case_id), [])
        pre_id = (
            _record_at(r, r.pre_synced_at, key_snaps)
            if r.pre_sync_status in _DONE and r.pre_synced_at is not None
            else (r.redcap_record_id if r.pre_sync_status in _DONE else None)
        )
        post_id = (
            _record_at(r, r.post_synced_at, key_snaps)
            if r.post_sync_status in _DONE and r.post_synced_at is not None
            else (r.redcap_record_id if r.post_sync_status in _DONE else None)
        )
        if pre_id is not None or post_id is not None:
            bind.execute(
                sa.text(
                    "UPDATE survey_receipts SET pre_redcap_record_id = :pre, "
                    "post_redcap_record_id = :post WHERE id = :id"
                ),
                {"pre": pre_id, "post": post_id, "id": r.id},
            )

    # 2./3. Student-level Post pointer + pending Post-reset reopen flag.
    by_key = {(r.student_id, r.case_id): r for r in receipts}
    owners = bind.execute(
        sa.text("SELECT id, survey_owner_case_id FROM students WHERE survey_owner_case_id IS NOT NULL")
    ).fetchall()
    for st in owners:
        r = by_key.get((st.id, st.survey_owner_case_id))
        if r is None:
            continue
        if r.post_sync_status in _DONE:
            bind.execute(
                sa.text("UPDATE students SET survey_post_case_id = :c WHERE id = :id"),
                {"c": st.survey_owner_case_id, "id": st.id},
            )
        elif r.pre_sync_status in _DONE:
            post_resets = [
                s for s in snaps_by_key.get((st.id, st.survey_owner_case_id), [])
                if s.scope == "post" and s.reset_at is not None
            ]
            if post_resets:
                bind.execute(
                    sa.text("UPDATE students SET survey_post_reopened_at = :at WHERE id = :id"),
                    {"at": max(s.reset_at for s in post_resets), "id": st.id},
                )


def downgrade() -> None:
    op.drop_column("survey_receipts", "post_redcap_record_id")
    op.drop_column("survey_receipts", "pre_redcap_record_id")
    op.drop_column("students", "survey_post_reopened_at")
    op.drop_column("students", "survey_post_case_id")
