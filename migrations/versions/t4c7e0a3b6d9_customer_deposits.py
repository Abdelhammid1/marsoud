"""MARSOUD-CUSTOMER-DEPOSIT-01 (Abdelhamid 2026-07-24).

Advance payments from customers, tracked BEFORE an invoice exists.
One additive table — no impact on any existing data.

Revision ID: t4c7e0a3b6d9
Revises: s3b6d9e2c5a8
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa


revision = 't4c7e0a3b6d9'
down_revision = 's3b6d9e2c5a8'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "customer_deposits" not in insp.get_table_names():
        op.create_table(
            "customer_deposits",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("customer_id", sa.Integer(),
                      sa.ForeignKey("customers.id"),
                      nullable=False, index=True),
            sa.Column("doc_number", sa.String(30), nullable=False),
            sa.Column("amount", sa.Numeric(15, 2), nullable=False),
            sa.Column("payment_method_id", sa.Integer(),
                      sa.ForeignKey("payment_methods.id"),
                      nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False,
                      server_default="ACTIVE", index=True),
            sa.Column("applied_invoice_id", sa.Integer(),
                      sa.ForeignKey("invoices.id"), nullable=True),
            sa.Column("journal_entry_id", sa.Integer(),
                      sa.ForeignKey("journal_entries.id"),
                      nullable=True),
            sa.Column("refund_journal_entry_id", sa.Integer(),
                      sa.ForeignKey("journal_entries.id"),
                      nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_by_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    if "customer_deposits" in insp.get_table_names():
        op.drop_table("customer_deposits")
