"""MARSOUD-TKT-HR-DECISIONS-01 — hr_decisions table.

Introduces the "decision document" layer per employee: every promotion,
transfer, warning, penalty, bonus, or termination is persisted here
BEFORE its side-effect fires (JE / employee-status flip / payroll fold),
and stays as an immutable audit row afterwards.

Revision ID: 9c2e4b8f7a11
Revises: 18d11e68dac0
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "9c2e4b8f7a11"
down_revision = "18d11e68dac0"
branch_labels = None
depends_on = None

TABLE = "hr_decisions"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def upgrade():
    if _has_table(TABLE):
        return  # idempotent — safe to re-run
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("company_id", sa.Integer,
                    sa.ForeignKey("companies.id", ondelete="CASCADE"),
                    nullable=False, index=True),
        sa.Column("employee_id", sa.Integer,
                    sa.ForeignKey("employees.id", ondelete="CASCADE"),
                    nullable=False, index=True),
        sa.Column("kind", sa.String(30), nullable=False, index=True),
        sa.Column("status", sa.String(20), nullable=False,
                    server_default="DRAFT", index=True),
        sa.Column("timing", sa.String(20), nullable=False,
                    server_default="IMMEDIATE"),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column("amount", sa.Numeric(15, 2), nullable=True),
        sa.Column("payment_account_id", sa.Integer,
                    sa.ForeignKey("accounts.id",
                                    name="fk_hr_decision_payment_account"),
                    nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text, nullable=True),
        sa.Column("reference", sa.String(100), nullable=True),
        sa.Column("journal_entry_id", sa.Integer,
                    sa.ForeignKey("journal_entries.id",
                                    ondelete="SET NULL",
                                    name="fk_hr_decision_journal_entry"),
                    nullable=True, index=True),
        sa.Column("payroll_run_id", sa.Integer,
                    sa.ForeignKey("payroll_runs.id",
                                    ondelete="SET NULL",
                                    name="fk_hr_decision_payroll_run"),
                    nullable=True, index=True),
        sa.Column("created_by", sa.Integer,
                    sa.ForeignKey("users.id",
                                    name="fk_hr_decision_created_by"),
                    nullable=True),
        sa.Column("created_at", sa.DateTime,
                    server_default=sa.func.current_timestamp()),
        sa.Column("executed_by", sa.Integer,
                    sa.ForeignKey("users.id",
                                    name="fk_hr_decision_executed_by"),
                    nullable=True),
        sa.Column("executed_at", sa.DateTime, nullable=True),
        sa.Column("cancelled_by", sa.Integer,
                    sa.ForeignKey("users.id",
                                    name="fk_hr_decision_cancelled_by"),
                    nullable=True),
        sa.Column("cancelled_at", sa.DateTime, nullable=True),
        sa.Column("cancel_reason", sa.Text, nullable=True),
    )


def downgrade():
    if _has_table(TABLE):
        op.drop_table(TABLE)
