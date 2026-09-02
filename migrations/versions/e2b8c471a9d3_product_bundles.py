"""MARSOUD-PRODUCT-BUNDLES-01 — products.is_bundle + bundle_components
+ invoice_items.bundle_ref + invoice_items.bundle_product_id.

Revision ID: e2b8c471a9d3
Revises: c7e910a2b4f5
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "e2b8c471a9d3"
down_revision = "c7e910a2b4f5"
branch_labels = None
depends_on = None

BC = "bundle_components"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def _has_col(table, col):
    if not _has_table(table):
        return False
    return col in {c["name"] for c in _inspector().get_columns(table)}


def upgrade():
    # ─ products.is_bundle ───────────────────────────────────
    if _has_table("products") and not _has_col("products", "is_bundle"):
        with op.batch_alter_table("products") as batch:
            batch.add_column(sa.Column(
                "is_bundle", sa.Boolean, nullable=False,
                server_default=sa.false()))

    # ─ bundle_components ────────────────────────────────────
    if not _has_table(BC):
        op.create_table(
            BC,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                        sa.ForeignKey("companies.id"),
                        nullable=False, index=True),
            sa.Column("bundle_product_id", sa.Integer,
                        sa.ForeignKey("products.id",
                                        name="fk_bundle_component_product"),
                        nullable=False, index=True),
            sa.Column("component_variant_id", sa.Integer,
                        sa.ForeignKey("product_variants.id",
                                        name="fk_bundle_component_variant"),
                        nullable=False),
            sa.Column("qty_per_bundle", sa.Numeric(15, 3),
                        nullable=False, server_default="1"),
            sa.UniqueConstraint("bundle_product_id",
                                 "component_variant_id",
                                 name="uq_bundle_component"),
        )

    # ─ invoice_items.bundle_ref / .bundle_product_id ────────
    if _has_table("invoice_items") and not _has_col(
            "invoice_items", "bundle_ref"):
        with op.batch_alter_table("invoice_items") as batch:
            batch.add_column(sa.Column(
                "bundle_ref", sa.String(20), nullable=True, index=True))
    if _has_table("invoice_items") and not _has_col(
            "invoice_items", "bundle_product_id"):
        with op.batch_alter_table("invoice_items") as batch:
            batch.add_column(sa.Column(
                "bundle_product_id", sa.Integer,
                sa.ForeignKey("products.id",
                                name="fk_invoice_item_bundle_product"),
                nullable=True))


def downgrade():
    for col in ("bundle_product_id", "bundle_ref"):
        if _has_table("invoice_items") and _has_col("invoice_items", col):
            with op.batch_alter_table("invoice_items") as batch:
                batch.drop_column(col)
    if _has_table(BC):
        op.drop_table(BC)
    if _has_table("products") and _has_col("products", "is_bundle"):
        with op.batch_alter_table("products") as batch:
            batch.drop_column("is_bundle")
