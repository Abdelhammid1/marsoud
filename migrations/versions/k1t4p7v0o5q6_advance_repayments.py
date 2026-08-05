"""MARSOUD-ADVANCE-INSTALMENTS (2026-08-05) — one row per instalment.

Recovering an advance instalment was `adv.remaining -= applied` and
nothing else. With no row behind it:

  · a payroll run redone for the same month deducted a SECOND time from
    the same balance, and nothing could notice
  · "how much have I paid so far?" had no answer but subtraction
  · no link at all between an advance and the payslip that took it

The unique constraint on (advance_id, payroll_run_id) IS the
no-double-deduction rule.

Additive: one new table, nothing existing is touched.

Revision ID: k1t4p7v0o5q6
Revises: j0s3o6u9n4p5
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'k1t4p7v0o5q6'
down_revision = 'j0s3o6u9n4p5'
branch_labels = None
depends_on = None

TABLE = "advance_repayments"


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade():
    if _has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("company_id", sa.Integer,
                  sa.ForeignKey("companies.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("advance_id", sa.Integer,
                  sa.ForeignKey("employee_advances.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("payroll_run_id", sa.Integer,
                  sa.ForeignKey("payroll_runs.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("payroll_line_id", sa.Integer,
                  sa.ForeignKey("payroll_lines.id", ondelete="SET NULL")),
        sa.Column("period_year", sa.Integer, nullable=False, index=True),
        sa.Column("period_month", sa.Integer, nullable=False, index=True),
        sa.Column("amount", sa.Numeric(15, 2), nullable=False,
                  server_default="0"),
        sa.Column("manual", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.Column("created_at", sa.DateTime),
        sa.UniqueConstraint("advance_id", "payroll_run_id",
                            name="uq_advance_repayment_run"),
    )


def downgrade():
    if _has_table(TABLE):
        op.drop_table(TABLE)
