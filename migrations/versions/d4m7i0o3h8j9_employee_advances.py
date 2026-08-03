"""MARSOUD-ADVANCES (2026-08-03) — employee advances.

Two additive tables. `advance_requests` is the employee-facing ask
(mirrors leave_requests); `employee_advances` is the real balance that
payroll deducts against.

Nothing existing changes: PayrollLine.advance_deduction keeps its
meaning, it just gets prefilled from the active advance instead of being
typed from memory.

Revision ID: d4m7i0o3h8j9
Revises: c3l6h9n2g7i8
Create Date: 2026-08-03
"""
from alembic import op
import sqlalchemy as sa


revision = 'd4m7i0o3h8j9'
down_revision = 'c3l6h9n2g7i8'
branch_labels = None
depends_on = None


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade():
    # advance_requests must precede employee_advances (FK target).
    if not _has_table("advance_requests"):
        op.create_table(
            "advance_requests",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("employee_id", sa.Integer(),
                      sa.ForeignKey("employees.id"),
                      nullable=False, index=True),
            sa.Column("amount", sa.Numeric(15, 2), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="PENDING", index=True),
            sa.Column("reviewed_by", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(), nullable=True),
            sa.Column("review_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True,
                      server_default=sa.func.current_timestamp()),
            sa.Column("created_by", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
        )

    if not _has_table("employee_advances"):
        op.create_table(
            "employee_advances",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("employee_id", sa.Integer(),
                      sa.ForeignKey("employees.id"),
                      nullable=False, index=True),
            sa.Column("amount", sa.Numeric(15, 2), nullable=False),
            sa.Column("remaining", sa.Numeric(15, 2), nullable=False),
            sa.Column("months", sa.Integer(), nullable=False,
                      server_default="1"),
            sa.Column("monthly_installment", sa.Numeric(15, 2),
                      nullable=False),
            sa.Column("disbursed_on", sa.Date(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="ACTIVE", index=True),
            sa.Column("source", sa.String(16), nullable=False,
                      server_default="DIRECT"),
            sa.Column("request_id", sa.Integer(),
                      sa.ForeignKey("advance_requests.id"), nullable=True),
            sa.Column("journal_entry_id", sa.Integer(),
                      sa.ForeignKey("journal_entries.id"), nullable=True),
            sa.Column("reversal_entry_id", sa.Integer(),
                      sa.ForeignKey("journal_entries.id"), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("approved_by", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_by", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("cancelled_by", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("cancelled_at", sa.DateTime(), nullable=True),
            sa.Column("cancel_reason", sa.Text(), nullable=True),
            sa.Column("settled_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True,
                      server_default=sa.func.current_timestamp()),
        )


def downgrade():
    if _has_table("employee_advances"):
        op.drop_table("employee_advances")
    if _has_table("advance_requests"):
        op.drop_table("advance_requests")
