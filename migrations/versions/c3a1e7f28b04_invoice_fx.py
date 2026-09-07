"""MARSOUD-INVOICE-FX-01 — foreign-currency invoicing columns.

Adds `invoices.fx_rate_at_receipt` and `payments.fx_rate`. Both
nullable — every existing EGP-in-EGP row stays untouched (NULL means
"no FX conversion happened, base-currency straight through").

Revision ID: c3a1e7f28b04
Revises: b8e5f1c2d039
Create Date: 2026-09-08
"""
from alembic import op
import sqlalchemy as sa


revision = "c3a1e7f28b04"
down_revision = "b8e5f1c2d039"
branch_labels = None
depends_on = None


def _insp():
    return sa.inspect(op.get_bind())


def _has_col(table, col):
    return col in {c["name"] for c in _insp().get_columns(table)}


def upgrade():
    if not _has_col("invoices", "fx_rate_at_receipt"):
        op.add_column("invoices", sa.Column(
            "fx_rate_at_receipt", sa.Numeric(15, 6), nullable=True))
    if not _has_col("payments", "fx_rate"):
        op.add_column("payments", sa.Column(
            "fx_rate", sa.Numeric(15, 6), nullable=True))


def downgrade():
    if _has_col("payments", "fx_rate"):
        op.drop_column("payments", "fx_rate")
    if _has_col("invoices", "fx_rate_at_receipt"):
        op.drop_column("invoices", "fx_rate_at_receipt")
