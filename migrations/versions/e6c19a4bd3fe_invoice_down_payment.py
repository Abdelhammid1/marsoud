"""MARSOUD-INVOICE-INSTALLMENTS-DISPLAY-01 — down_payment_amount column.

One nullable Numeric column so a tenant with no history of up-front
collections stays byte-identical to today (plain invoices keep NULL).

Revision ID: e6c19a4bd3fe
Revises: d5b28f0c9e17
Create Date: 2026-09-08
"""
from alembic import op
import sqlalchemy as sa


revision = "e6c19a4bd3fe"
down_revision = "d5b28f0c9e17"
branch_labels = None
depends_on = None


def _insp():
    return sa.inspect(op.get_bind())


def _has_col(table, col):
    return col in {c["name"] for c in _insp().get_columns(table)}


def upgrade():
    if not _has_col("invoices", "down_payment_amount"):
        op.add_column("invoices", sa.Column(
            "down_payment_amount", sa.Numeric(15, 2), nullable=True))


def downgrade():
    if _has_col("invoices", "down_payment_amount"):
        op.drop_column("invoices", "down_payment_amount")
