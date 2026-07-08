"""MARSOUD-EVALUATIONS — monthly performance evaluation layer.

Four new tables on top of the existing Employee / Task / Project /
Lead / SalesCommission surfaces. Nothing existing is touched; the new
tables just *read* the operational tables when actuals are gathered
(later — v1 lets خديجة enter actuals manually).

Design highlights (from Abdelhamid's spec):
  - metric_key is a free-form VARCHAR, NOT an enum. Adding a new
    metric later must never require a code change + redeploy.
  - Every table is company-scoped for multi-tenant isolation.
  - EvaluationCycle status is a state machine: OPEN → SUBMITTED → LOCKED.
    Reopening from LOCKED is a rare admin path, so we don't bake it into
    the enum — the app-layer permission check gates it.
  - EmployeeEvaluation is the *computed* row; targets + actuals feed it.
    Storing the scores means we can lock the cycle and freeze the
    numbers even if a target/actual is later edited (LOCKED status
    blocks that too, but the frozen snapshot is defense-in-depth).

Revision ID: c8e5b2f1d9a4
Revises: b7d4e2c9a1f5
"""
from alembic import op
import sqlalchemy as sa


revision = "c8e5b2f1d9a4"
down_revision = "b7d4e2c9a1f5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "evaluation_cycles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("company_id", sa.Integer,
                    sa.ForeignKey("companies.id"),
                    nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("period_type", sa.String(20), nullable=False),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("status", sa.String(20), nullable=False,
                    server_default="OPEN"),
        sa.Column("created_by_id", sa.Integer,
                    sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False,
                    server_default=sa.func.now()),
    )

    op.create_table(
        "employee_targets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("cycle_id", sa.Integer,
                    sa.ForeignKey("evaluation_cycles.id",
                                     ondelete="CASCADE"),
                    nullable=False, index=True),
        sa.Column("employee_id", sa.Integer,
                    sa.ForeignKey("employees.id", ondelete="CASCADE"),
                    nullable=False, index=True),
        sa.Column("metric_key", sa.String(120), nullable=False),
        sa.Column("target_value", sa.Numeric(15, 4), nullable=False,
                    server_default="0"),
        sa.Column("weight_pct", sa.Numeric(6, 2), nullable=False,
                    server_default="0"),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.UniqueConstraint(
            "cycle_id", "employee_id", "metric_key",
            name="uq_target_cycle_employee_metric",
        ),
    )

    op.create_table(
        "employee_metric_actuals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("cycle_id", sa.Integer,
                    sa.ForeignKey("evaluation_cycles.id",
                                     ondelete="CASCADE"),
                    nullable=False, index=True),
        sa.Column("employee_id", sa.Integer,
                    sa.ForeignKey("employees.id", ondelete="CASCADE"),
                    nullable=False, index=True),
        sa.Column("metric_key", sa.String(120), nullable=False),
        sa.Column("actual_value", sa.Numeric(15, 4), nullable=False,
                    server_default="0"),
        sa.Column("source", sa.String(20), nullable=False,
                    server_default="MANUAL"),
        sa.Column("entered_by_id", sa.Integer,
                    sa.ForeignKey("users.id"), nullable=True),
        sa.Column("entered_at", sa.DateTime, nullable=False,
                    server_default=sa.func.now()),
        sa.Column("evidence_note", sa.Text, nullable=True),
        sa.UniqueConstraint(
            "cycle_id", "employee_id", "metric_key",
            name="uq_actual_cycle_employee_metric",
        ),
    )

    op.create_table(
        "employee_evaluations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("cycle_id", sa.Integer,
                    sa.ForeignKey("evaluation_cycles.id",
                                     ondelete="CASCADE"),
                    nullable=False, index=True),
        sa.Column("employee_id", sa.Integer,
                    sa.ForeignKey("employees.id", ondelete="CASCADE"),
                    nullable=False, index=True),
        sa.Column("target_score", sa.Numeric(6, 2), nullable=False,
                    server_default="0"),
        sa.Column("execution_score", sa.Numeric(6, 2), nullable=False,
                    server_default="0"),
        sa.Column("growth_score", sa.Numeric(6, 2), nullable=False,
                    server_default="0"),
        sa.Column("final_score", sa.Numeric(6, 2), nullable=False,
                    server_default="0"),
        sa.Column("bonus_tier", sa.String(20), nullable=False,
                    server_default="ZERO"),
        sa.Column("reviewed_by_id", sa.Integer,
                    sa.ForeignKey("users.id"), nullable=True),
        sa.Column("review_notes", sa.Text, nullable=True),
        sa.Column("reviewed_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint(
            "cycle_id", "employee_id",
            name="uq_evaluation_cycle_employee",
        ),
    )


def downgrade():
    op.drop_table("employee_evaluations")
    op.drop_table("employee_metric_actuals")
    op.drop_table("employee_targets")
    op.drop_table("evaluation_cycles")
