"""Assessment runs, domain results, and evidence

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-14

"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assessment_runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("session_id", sa.String(32), sa.ForeignKey("interview_sessions.id"), nullable=False),
        sa.Column("case_id", sa.String(50), nullable=False),
        sa.Column("case_version", sa.String(20), nullable=False, server_default=""),
        sa.Column("rubric_version", sa.String(20), nullable=False, server_default=""),
        sa.Column("model_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(20), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("overall_level", sa.String(30), nullable=True),
        sa.Column("overall_summary", sa.Text(), nullable=True),
        sa.Column("focus_areas", sa.Text(), nullable=True),
        sa.Column("verification_status", sa.String(30), nullable=True),
        sa.Column("error_code", sa.String(60), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_assessment_runs_session_id", "assessment_runs", ["session_id"])
    op.create_table(
        "assessment_domain_results",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("assessment_run_id", sa.String(32), sa.ForeignKey("assessment_runs.id"), nullable=False),
        sa.Column("rubric_domain", sa.String(60), nullable=False),
        sa.Column("performance_level", sa.String(30), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("narrative", sa.Text(), nullable=False, server_default=""),
        sa.Column("strengths", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("areas_for_growth", sa.Text(), nullable=False, server_default="[]"),
    )
    op.create_index(
        "ix_assessment_domain_results_assessment_run_id", "assessment_domain_results", ["assessment_run_id"]
    )
    op.create_table(
        "assessment_evidence",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("domain_result_id", sa.String(32), sa.ForeignKey("assessment_domain_results.id"), nullable=False),
        sa.Column("turn_id", sa.String(32), sa.ForeignKey("conversation_turns.id"), nullable=False),
        sa.Column("turn_label", sa.String(20), nullable=False, server_default=""),
        sa.Column("evidence_type", sa.String(40), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("severity", sa.String(20), nullable=True),
        sa.Column("student_excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("patient_excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("explanation", sa.Text(), nullable=False, server_default=""),
        sa.Column("why_it_matters", sa.Text(), nullable=False, server_default=""),
        sa.Column("suggested_alternative", sa.Text(), nullable=False, server_default=""),
        sa.Column("confidence_level", sa.String(20), nullable=False, server_default="moderate"),
        sa.Column("reviewer_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_assessment_evidence_domain_result_id", "assessment_evidence", ["domain_result_id"])


def downgrade() -> None:
    op.drop_table("assessment_evidence")
    op.drop_table("assessment_domain_results")
    op.drop_table("assessment_runs")
