"""MARSOUD-COMM-CASH-BASIS-01 — commission fx_rate column.

Cash-basis commissions on a foreign-currency invoice record the
exchange rate the payment came in at, so a report can retrace how
`amount` (always base currency) was derived from the payment slice
(foreign currency).  NULL for base-currency payments — historical
rows stay untouched and read as "no FX conversion applied".

Revision ID: f7a3c8b9d012
Revises: e6c19a4bd3fe
Create Date: 2026-09-20
"""
from alembic import op
import sqlalchemy as sa


revision = "f7a3c8b9d012"
down_revision = "e6c19a4bd3fe"
branch_labels = None
depends_on = None


def _insp():
    return sa.inspect(op.get_bind())


def _has_col(table, col):
    return col in {c["name"] for c in _insp().get_columns(table)}


def upgrade():
    if not _has_col("sales_commissions", "fx_rate"):
        op.add_column("sales_commissions", sa.Column(
            "fx_rate", sa.Numeric(15, 6), nullable=True))


def downgrade():
    if _has_col("sales_commissions", "fx_rate"):
        op.drop_column("sales_commissions", "fx_rate")
