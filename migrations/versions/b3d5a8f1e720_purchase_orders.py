"""MARSOUD-PURCHASE-ORDERS-01 — purchase_orders + goods_receipt_notes.

Adds four new tables (purchase_orders, purchase_order_items,
goods_receipt_notes, goods_receipt_items) and one column on the
existing vendor_bills table (purchase_order_id, FK, nullable).

Revision ID: b3d5a8f1e720
Revises: 9c2e4b8f7a11
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "b3d5a8f1e720"
down_revision = "9c2e4b8f7a11"
branch_labels = None
depends_on = None

PO = "purchase_orders"
POI = "purchase_order_items"
GRN = "goods_receipt_notes"
GRNI = "goods_receipt_items"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def _has_col(table, col):
    if not _has_table(table):
        return False
    return col in {c["name"] for c in _inspector().get_columns(table)}


def upgrade():
    # ─ purchase_orders ────────────────────────────────────────
    if not _has_table(PO):
        op.create_table(
            PO,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                        sa.ForeignKey("companies.id"),
                        nullable=False, index=True),
            sa.Column("number", sa.String(20), nullable=False, index=True),
            sa.Column("vendor_id", sa.Integer,
                        sa.ForeignKey("vendors.id"), nullable=False),
            sa.Column("status", sa.String(30), nullable=False,
                        server_default="REQUESTED", index=True),
            sa.Column("currency", sa.String(3),
                        server_default="SAR"),
            sa.Column("issue_date", sa.Date, nullable=False,
                        server_default=sa.func.current_date()),
            sa.Column("expected_date", sa.Date, nullable=True),
            sa.Column("subtotal", sa.Numeric(15, 4),
                        server_default="0"),
            sa.Column("tax_rate", sa.Numeric(5, 2),
                        server_default="0"),
            sa.Column("tax_amount", sa.Numeric(15, 4),
                        server_default="0"),
            sa.Column("total", sa.Numeric(15, 4),
                        server_default="0"),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column("requested_by_id", sa.Integer,
                        sa.ForeignKey("users.id",
                                        name="fk_po_requested_by"),
                        nullable=False),
            sa.Column("approved_by_id", sa.Integer,
                        sa.ForeignKey("users.id",
                                        name="fk_po_approved_by"),
                        nullable=True),
            sa.Column("approved_at", sa.DateTime, nullable=True),
            sa.Column("rejected_reason", sa.Text, nullable=True),
            sa.Column("cancelled_reason", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime,
                        server_default=sa.func.current_timestamp(),
                        nullable=False),
            sa.Column("deleted_at", sa.DateTime, nullable=True),
            sa.Column("deleted_by_id", sa.Integer,
                        sa.ForeignKey("users.id",
                                        name="fk_po_deleted_by"),
                        nullable=True),
            sa.UniqueConstraint("company_id", "number",
                                 name="uq_purchase_order_number"),
        )

    # ─ purchase_order_items ──────────────────────────────────
    if not _has_table(POI):
        op.create_table(
            POI,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("purchase_order_id", sa.Integer,
                        sa.ForeignKey("purchase_orders.id",
                                        name="fk_poi_po"),
                        nullable=False),
            sa.Column("description", sa.String(255), nullable=False),
            sa.Column("line_type", sa.String(20), nullable=False,
                        server_default="INVENTORY"),
            sa.Column("variant_id", sa.Integer,
                        sa.ForeignKey("product_variants.id",
                                        name="fk_poi_variant"),
                        nullable=True),
            sa.Column("warehouse_id", sa.Integer,
                        sa.ForeignKey("warehouses.id",
                                        name="fk_poi_warehouse"),
                        nullable=True),
            sa.Column("unit_id", sa.Integer,
                        sa.ForeignKey("product_units.id",
                                        name="fk_poi_unit"),
                        nullable=True),
            sa.Column("quantity", sa.Numeric(15, 3), nullable=False),
            sa.Column("unit_price", sa.Numeric(15, 4),
                        server_default="0"),
            sa.Column("line_total", sa.Numeric(15, 4),
                        server_default="0"),
            sa.Column("qty_received", sa.Numeric(15, 3),
                        server_default="0", nullable=False),
            sa.Column("qty_invoiced", sa.Numeric(15, 3),
                        server_default="0", nullable=False),
        )

    # ─ goods_receipt_notes ────────────────────────────────────
    if not _has_table(GRN):
        op.create_table(
            GRN,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                        sa.ForeignKey("companies.id"),
                        nullable=False, index=True),
            sa.Column("number", sa.String(20), nullable=False, index=True),
            sa.Column("purchase_order_id", sa.Integer,
                        sa.ForeignKey("purchase_orders.id",
                                        name="fk_grn_po"),
                        nullable=False, index=True),
            sa.Column("received_date", sa.Date,
                        server_default=sa.func.current_date(),
                        nullable=False),
            sa.Column("received_by_id", sa.Integer,
                        sa.ForeignKey("users.id",
                                        name="fk_grn_received_by"),
                        nullable=False),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime,
                        server_default=sa.func.current_timestamp(),
                        nullable=False),
            sa.UniqueConstraint("company_id", "number",
                                 name="uq_grn_number"),
        )

    # ─ goods_receipt_items ────────────────────────────────────
    if not _has_table(GRNI):
        op.create_table(
            GRNI,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("grn_id", sa.Integer,
                        sa.ForeignKey("goods_receipt_notes.id",
                                        name="fk_grni_grn"),
                        nullable=False),
            sa.Column("po_item_id", sa.Integer,
                        sa.ForeignKey("purchase_order_items.id",
                                        name="fk_grni_po_item"),
                        nullable=False),
            sa.Column("quantity_received", sa.Numeric(15, 3),
                        nullable=False),
        )

    # ─ vendor_bills.purchase_order_id ─────────────────────────
    if _has_table("vendor_bills") and not _has_col(
            "vendor_bills", "purchase_order_id"):
        with op.batch_alter_table("vendor_bills") as batch:
            batch.add_column(sa.Column(
                "purchase_order_id", sa.Integer,
                sa.ForeignKey("purchase_orders.id",
                                name="fk_vendor_bill_po"),
                nullable=True, index=True))


def downgrade():
    if _has_table("vendor_bills") and _has_col(
            "vendor_bills", "purchase_order_id"):
        with op.batch_alter_table("vendor_bills") as batch:
            batch.drop_column("purchase_order_id")
    for t in (GRNI, GRN, POI, PO):
        if _has_table(t):
            op.drop_table(t)
