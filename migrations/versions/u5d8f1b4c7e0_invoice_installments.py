"""MARSOUD-INSTALLMENT-PLAN-01 (Abdelhamid 2026-07-24).

Split an invoice into scheduled installments each with its own
due date + reminder tracking. Two additive tables.

Revision ID: u5d8f1b4c7e0
Revises: t4c7e0a3b6d9
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa


revision = 'u5d8f1b4c7e0'
down_revision = 't4c7e0a3b6d9'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "invoice_installments" not in insp.get_table_names():
        op.create_table(
            "invoice_installments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("invoice_id", sa.Integer(),
                      sa.ForeignKey("invoices.id",
                                     ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("sequence_no", sa.Integer(), nullable=False),
            sa.Column("amount", sa.Numeric(15, 2), nullable=False),
            sa.Column("due_date", sa.Date(), nullable=False,
                      index=True),
            sa.Column("status", sa.String(20), nullable=False,
                      server_default="PENDING", index=True),
            sa.Column("paid_payment_id", sa.Integer(),
                      sa.ForeignKey("payments.id"), nullable=True),
            sa.Column("paid_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
        )
        op.create_index(
            "ux_installment_seq",
            "invoice_installments",
            ["invoice_id", "sequence_no"],
            unique=True,
        )
    if "installment_reminder_sent" not in insp.get_table_names():
        op.create_table(
            "installment_reminder_sent",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("installment_id", sa.Integer(),
                      sa.ForeignKey("invoice_installments.id",
                                     ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("threshold_kind", sa.String(20),
                      nullable=False),
            sa.Column("threshold_days", sa.Integer(),
                      nullable=False),
            sa.Column("sent_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
        )
        op.create_index(
            "ux_installment_reminder",
            "installment_reminder_sent",
            ["installment_id", "threshold_kind", "threshold_days"],
            unique=True,
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    for t in ("installment_reminder_sent", "invoice_installments"):
        if t in insp.get_table_names():
            op.drop_table(t)
