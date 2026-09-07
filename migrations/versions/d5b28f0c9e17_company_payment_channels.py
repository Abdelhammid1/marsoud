"""MARSOUD-INVOICE-PAYMENT-CHANNELS-01 — InstaPay + e-wallet on Company.

Three nullable String columns so a tenant that hasn't set them stays
byte-identical to today (empty channels don't render on the invoice
PDF at all).

Revision ID: d5b28f0c9e17
Revises: c3a1e7f28b04
Create Date: 2026-09-08
"""
from alembic import op
import sqlalchemy as sa


revision = "d5b28f0c9e17"
down_revision = "c3a1e7f28b04"
branch_labels = None
depends_on = None


def _insp():
    return sa.inspect(op.get_bind())


def _has_col(table, col):
    return col in {c["name"] for c in _insp().get_columns(table)}


def upgrade():
    if not _has_col("companies", "instapay_handle"):
        op.add_column("companies", sa.Column(
            "instapay_handle", sa.String(100), nullable=True))
    if not _has_col("companies", "ewallet_number"):
        op.add_column("companies", sa.Column(
            "ewallet_number", sa.String(30), nullable=True))
    if not _has_col("companies", "ewallet_provider"):
        op.add_column("companies", sa.Column(
            "ewallet_provider", sa.String(60), nullable=True))


def downgrade():
    for col in ("ewallet_provider", "ewallet_number", "instapay_handle"):
        if _has_col("companies", col):
            op.drop_column("companies", col)
