"""MARSOUD-EVAL-METRIC-LOG — raw-log tier under EmployeeMetricActual.

Follow-up to the evaluation system c8e5b2f1d9a4. Khadeeja needed a
frictionless way to record raw metric values as they happen (a lead
scored, a call closed, a plan delivered) without her having to bucket
them into weeks / months / periods. This migration lays the
storage: one log entry per (cycle, employee, metric, date), and a
`aggregation_method` column on employee_targets so we know how to
collapse the logs into a single `actual_value` at cycle-close time.

Aggregation methods (see services/evaluation.py::aggregate_actuals):
  · AVERAGE — mean of every logged value (weekly leads, etc.)
  · SUM     — sum of every logged value (closed deals per month)
  · LATEST  — the most recent value by entry_date (plan delivered
                              — a single 0/1 datapoint)

Revision ID: d9f4a3b8c5e2
Revises: c8e5b2f1d9a4
"""
from alembic import op
import sqlalchemy as sa


revision = "d9f4a3b8c5e2"
down_revision = "c8e5b2f1d9a4"
branch_labels = None
depends_on = None


def upgrade():
    # 1) New raw-log table
    op.create_table(
        "metric_log_entries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("company_id", sa.Integer,
                    sa.ForeignKey("companies.id"),
                    nullable=False, index=True),
        sa.Column("cycle_id", sa.Integer,
                    sa.ForeignKey("evaluation_cycles.id",
                                     ondelete="CASCADE"),
                    nullable=False, index=True),
        sa.Column("employee_id", sa.Integer,
                    sa.ForeignKey("employees.id",
                                     ondelete="CASCADE"),
                    nullable=False, index=True),
        sa.Column("metric_key", sa.String(120), nullable=False),
        sa.Column("entry_date", sa.Date, nullable=False),
        sa.Column("value", sa.Numeric(15, 4), nullable=False,
                    server_default="0"),
        sa.Column("entered_by_id", sa.Integer,
                    sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False,
                    server_default=sa.func.now()),
    )
    op.create_index(
        "ix_metric_log_cycle_emp_metric",
        "metric_log_entries",
        ["cycle_id", "employee_id", "metric_key"],
    )

    # 2) Add aggregation_method column to employee_targets. Default
    #    to SUM as the least surprising fallback for existing targets
    #    where no method was chosen — sum reflects "total done" which
    #    is the most common interpretation.
    with op.batch_alter_table("employee_targets") as batch_op:
        batch_op.add_column(sa.Column(
            "aggregation_method", sa.String(20),
            nullable=False, server_default="SUM",
        ))


def downgrade():
    with op.batch_alter_table("employee_targets") as batch_op:
        batch_op.drop_column("aggregation_method")
    op.drop_index("ix_metric_log_cycle_emp_metric",
                    table_name="metric_log_entries")
    op.drop_table("metric_log_entries")
