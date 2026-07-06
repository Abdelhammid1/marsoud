"""MARSOUD-UOM-PRICE — per-unit sale price on ProductUnit.

Before this migration Product.default_price was applied to every
selectable unit at POS/invoice time, so switching between piece and
carton in the cart didn't change the line price. Users reported this
as "the piece sells at the same price as the carton" — the price never
scaled with the unit conversion factor.

This migration adds a nullable `sale_price` column on `product_units`.
Meaning:
  - sale_price IS NULL   → derived at read time as
                            Product.default_price × unit.conversion_factor
                            (backward-compatible fallback: for the base
                            row this equals default_price exactly, so
                            products with a single base unit see no
                            behavior change)
  - sale_price IS NOT NULL → override, used as-is

We do NOT backfill values — leaving the column NULL lets tenants opt
in to explicit per-unit prices from the units management UI without
freezing today's (possibly wrong) computed price.

Revision ID: a6c9d3e7b4f2
Revises: z5b8e4d9c2a6, d8_a2f4c9b7e3d
"""
from alembic import op
import sqlalchemy as sa


revision = "a6c9d3e7b4f2"
# Merges the two open heads (lead ticket work + user/employee FK flip)
# so the tree stays linear from here on.
down_revision = ("z5b8e4d9c2a6", "d8_a2f4c9b7e3d")
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("product_units") as batch_op:
        batch_op.add_column(sa.Column(
            "sale_price", sa.Numeric(15, 4), nullable=True,
        ))


def downgrade():
    with op.batch_alter_table("product_units") as batch_op:
        batch_op.drop_column("sale_price")
