"""invoices.pending_plan_json for save-as-quote installment persistence

MARSOUD-INVOICE-CUSTOM-INSTALLMENTS-01 (2026-10-10) — holds the
serialized `{down_payment_*, rows:[{due_date, amount}]}` payload when
"Save as quote" lands with either a custom or equal installments
schedule.  Cleared on Send once the plan + down-payment are applied.
Lets the operator author a custom-schedule quote, email it, then
convert it to an invoice later without re-entering the dates / amounts.

NULL for every existing invoice; code treats absence as "no stored
plan".

Revision ID: c3f8e42a9b17
Revises: a1b2c3d4e5f6
Create Date: 2026-10-10 09:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "c3f8e42a9b17"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("invoices") as batch:
        batch.add_column(sa.Column("pending_plan_json", sa.Text(),
                                     nullable=True))


def downgrade():
    with op.batch_alter_table("invoices") as batch:
        batch.drop_column("pending_plan_json")
