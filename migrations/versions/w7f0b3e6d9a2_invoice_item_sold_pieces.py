"""MARSOUD-DUAL-UOM-WEIGHT-01 pt 2 (Abdelhamid 2026-07-25).

InvoiceItem gains a nullable `sold_pieces` column so POS sales of
weight+piece products can record BOTH dimensions in one atomic
transaction. Default NULL means "not a piece-tracked sale" — the
inventory side falls back to weight-only just like today.

Revision ID: w7f0b3e6d9a2
Revises: v6e9a2c5b8d1
Create Date: 2026-07-25
"""
from alembic import op
import sqlalchemy as sa


revision = 'w7f0b3e6d9a2'
down_revision = 'v6e9a2c5b8d1'
branch_labels = None
depends_on = None


def _has_col(insp, table, col):
    return any(c["name"] == col
               for c in insp.get_columns(table))


def upgrade():
    insp = sa.inspect(op.get_bind())
    if not _has_col(insp, "invoice_items", "sold_pieces"):
        with op.batch_alter_table("invoice_items") as batch:
            batch.add_column(sa.Column(
                "sold_pieces", sa.Numeric(15, 2), nullable=True))


def downgrade():
    insp = sa.inspect(op.get_bind())
    if _has_col(insp, "invoice_items", "sold_pieces"):
        with op.batch_alter_table("invoice_items") as batch:
            batch.drop_column("sold_pieces")
