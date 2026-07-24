"""MARSOUD-DUAL-UOM-WEIGHT-01 (Abdelhamid 2026-07-24).

Gold/silver shops sell the same product BOTH by weight AND by
piece, with piece weight variable per sale. Add two nullable
columns (tracks_piece_count flag on Product, piece_count counter
on StockBalance) + one InventoryCount table for reconciliation.

Every column defaults such that products that DON'T opt in stay
100% backwards compatible.

Revision ID: v6e9a2c5b8d1
Revises: u5d8f1b4c7e0
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa


revision = 'v6e9a2c5b8d1'
down_revision = 'u5d8f1b4c7e0'
branch_labels = None
depends_on = None


def _has_col(insp, table, col):
    return any(c["name"] == col
               for c in insp.get_columns(table))


def upgrade():
    insp = sa.inspect(op.get_bind())
    if not _has_col(insp, "products", "tracks_piece_count"):
        with op.batch_alter_table("products") as batch:
            batch.add_column(sa.Column(
                "tracks_piece_count", sa.Boolean(),
                nullable=False, server_default=sa.text("0")))
    if not _has_col(insp, "stock_balances", "piece_count"):
        with op.batch_alter_table("stock_balances") as batch:
            batch.add_column(sa.Column(
                "piece_count", sa.Numeric(15, 2),
                nullable=False, server_default="0"))
    if "inventory_counts" not in insp.get_table_names():
        op.create_table(
            "inventory_counts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("variant_id", sa.Integer(),
                      sa.ForeignKey("product_variants.id"),
                      nullable=False),
            sa.Column("warehouse_id", sa.Integer(),
                      sa.ForeignKey("warehouses.id"),
                      nullable=False),
            sa.Column("book_qty", sa.Numeric(15, 2), nullable=False),
            sa.Column("book_pieces", sa.Numeric(15, 2),
                      nullable=False, server_default="0"),
            sa.Column("counted_qty", sa.Numeric(15, 2),
                      nullable=False),
            sa.Column("counted_pieces", sa.Numeric(15, 2),
                      nullable=False, server_default="0"),
            sa.Column("variance_qty", sa.Numeric(15, 2),
                      nullable=False, server_default="0"),
            sa.Column("variance_pieces", sa.Numeric(15, 2),
                      nullable=False, server_default="0"),
            sa.Column("status", sa.String(20),
                      nullable=False, server_default="DRAFT"),
            sa.Column("counted_by_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("counted_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
            sa.Column("adjustment_movement_id", sa.Integer(),
                      sa.ForeignKey("stock_movements.id"),
                      nullable=True),
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    if "inventory_counts" in insp.get_table_names():
        op.drop_table("inventory_counts")
    if _has_col(insp, "stock_balances", "piece_count"):
        with op.batch_alter_table("stock_balances") as batch:
            batch.drop_column("piece_count")
    if _has_col(insp, "products", "tracks_piece_count"):
        with op.batch_alter_table("products") as batch:
            batch.drop_column("tracks_piece_count")
