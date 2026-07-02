"""MARSOUD-UNIT-CONVERSION-01 — product units with conversion factors.

Adds:
  - product_units table (per product, unique unit_name)
  - invoice_items.unit_id + base_quantity
  - vendor_bill_items.unit_id + base_quantity
  - Backfill: every existing tracked product gets one is_base=True row
    using its default_unit + conversion_factor=1

Revision ID: d5_a9b2c8f4e3d
Revises: d4_f5a8b3c9e2d
"""
from alembic import op
import sqlalchemy as sa


revision = "d5_a9b2c8f4e3d"
down_revision = "d4_f5a8b3c9e2d"
branch_labels = None
depends_on = None


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_table("product_units"):
        op.create_table(
            "product_units",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("product_id", sa.Integer(),
                      sa.ForeignKey("products.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("unit_name", sa.String(50), nullable=False),
            sa.Column("conversion_factor", sa.Numeric(15, 6),
                      nullable=False, server_default="1"),
            sa.Column("is_base", sa.Boolean(),
                      nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(),
                      server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint("product_id", "unit_name",
                                name="uq_product_unit_name"),
        )

    if not _has_col("invoice_items", "unit_id"):
        with op.batch_alter_table("invoice_items") as batch:
            batch.add_column(sa.Column(
                "unit_id", sa.Integer(),
                sa.ForeignKey(
                    "product_units.id",
                    name="fk_invoice_items_unit_id_product_units",
                ), nullable=True,
            ))
    if not _has_col("invoice_items", "base_quantity"):
        with op.batch_alter_table("invoice_items") as batch:
            batch.add_column(sa.Column(
                "base_quantity", sa.Numeric(15, 4), nullable=True,
            ))

    if not _has_col("vendor_bill_items", "unit_id"):
        with op.batch_alter_table("vendor_bill_items") as batch:
            batch.add_column(sa.Column(
                "unit_id", sa.Integer(),
                sa.ForeignKey(
                    "product_units.id",
                    name="fk_vendor_bill_items_unit_id_product_units",
                ), nullable=True,
            ))
    if not _has_col("vendor_bill_items", "base_quantity"):
        with op.batch_alter_table("vendor_bill_items") as batch:
            batch.add_column(sa.Column(
                "base_quantity", sa.Numeric(15, 4), nullable=True,
            ))

    # Backfill: create one is_base row per tracked product using its
    # default_unit. Idempotent — skips products that already have a
    # base unit (from a partial prior run or a manual seed).
    bind = op.get_bind()
    products = bind.execute(sa.text(
        "SELECT id, company_id, default_unit FROM products "
        "WHERE is_tracked = 1"
    )).fetchall()
    for prod_id, company_id, default_unit in products:
        already = bind.execute(sa.text(
            "SELECT id FROM product_units "
            "WHERE product_id = :p AND is_base = 1"
        ), {"p": prod_id}).fetchone()
        if already:
            continue
        unit_name = (default_unit or "قطعة").strip() or "قطعة"
        bind.execute(sa.text(
            "INSERT INTO product_units "
            "(company_id, product_id, unit_name, conversion_factor, is_base) "
            "VALUES (:c, :p, :n, 1, 1)"
        ), {"c": company_id, "p": prod_id, "n": unit_name})


def downgrade():
    if _has_col("vendor_bill_items", "base_quantity"):
        with op.batch_alter_table("vendor_bill_items") as batch:
            batch.drop_column("base_quantity")
    if _has_col("vendor_bill_items", "unit_id"):
        with op.batch_alter_table("vendor_bill_items") as batch:
            batch.drop_column("unit_id")
    if _has_col("invoice_items", "base_quantity"):
        with op.batch_alter_table("invoice_items") as batch:
            batch.drop_column("base_quantity")
    if _has_col("invoice_items", "unit_id"):
        with op.batch_alter_table("invoice_items") as batch:
            batch.drop_column("unit_id")
    if _has_table("product_units"):
        op.drop_table("product_units")
