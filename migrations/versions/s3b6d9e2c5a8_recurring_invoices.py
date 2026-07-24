"""MARSOUD-RECURRING-INVOICE-01 (Abdelhamid 2026-07-24).

Auto-generate invoices from a template on a schedule. Direct
mirror of the recurring-journals system.

Revision ID: s3b6d9e2c5a8
Revises: r2a5c8e1d4b7
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa


revision = 's3b6d9e2c5a8'
down_revision = 'r2a5c8e1d4b7'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "recurring_invoices" not in insp.get_table_names():
        op.create_table(
            "recurring_invoices",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("customer_id", sa.Integer(),
                      sa.ForeignKey("customers.id"), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("items_json", sa.Text(), nullable=False),
            sa.Column("tax_rate", sa.Numeric(5, 2),
                      nullable=False, server_default="15.00"),
            sa.Column("frequency", sa.String(20), nullable=False),
            sa.Column("next_run_date", sa.Date(),
                      nullable=False, index=True),
            sa.Column("end_date", sa.Date(), nullable=True),
            sa.Column("is_active", sa.Boolean(),
                      nullable=False, server_default=sa.text("1")),
            sa.Column("is_deleted", sa.Boolean(),
                      nullable=False, server_default=sa.text("0")),
            sa.Column("created_by_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
        )
    if "recurring_invoice_logs" not in insp.get_table_names():
        op.create_table(
            "recurring_invoice_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("recurring_id", sa.Integer(),
                      sa.ForeignKey("recurring_invoices.id",
                                     ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("action", sa.String(20), nullable=False),
            sa.Column("period_posted", sa.Date(), nullable=True),
            sa.Column("invoice_id", sa.Integer(),
                      sa.ForeignKey("invoices.id"), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("created_by_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
        )
        # Duplicate-run guard: at most one EXECUTE per (recurring_id,
        # period). Matches the pattern used for journals.
        op.create_index(
            "ux_rec_invoice_log_period",
            "recurring_invoice_logs",
            ["recurring_id", "period_posted", "action"],
            unique=True,
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    for t in ("recurring_invoice_logs", "recurring_invoices"):
        if t in insp.get_table_names():
            op.drop_table(t)
