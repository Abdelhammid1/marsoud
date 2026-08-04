"""MARSOUD-PACK-ONLY-PRICING (2026-08-04).

Products stop carrying a hand-typed per-piece price/cost. The user
enters the box numbers and the system divides, so the box numbers have
to be the ones that persist:

  pack_pieces          box size (1 = sold individually)
  pack_purchase_price  what the box costs from the supplier

default_price and product_variants.unit_cost stay exactly as they are —
they're now computed output rather than input, so no data changes shape.
The box SALE price needs no column: it already belongs on the pack
product_units.sale_price row.

Additive and nullable; existing products keep working and simply have
no box data until someone edits them (a backfill is deliberately out of
scope for this ticket).

Revision ID: f6o9k2q5j0l1
Revises: e5n8j1p4i9k0
Create Date: 2026-08-04
"""
from alembic import op
import sqlalchemy as sa


revision = 'f6o9k2q5j0l1'
down_revision = 'e5n8j1p4i9k0'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    with op.batch_alter_table("products", schema=None) as batch:
        if not _has_col("products", "pack_pieces"):
            batch.add_column(sa.Column("pack_pieces", sa.Integer(),
                                       nullable=True, server_default="1"))
        if not _has_col("products", "pack_purchase_price"):
            batch.add_column(sa.Column("pack_purchase_price",
                                       sa.Numeric(15, 4), nullable=True))


def downgrade():
    with op.batch_alter_table("products", schema=None) as batch:
        if _has_col("products", "pack_purchase_price"):
            batch.drop_column("pack_purchase_price")
        if _has_col("products", "pack_pieces"):
            batch.drop_column("pack_pieces")
