"""MARSOUD-ERP-01 Phase 1 — inventory schema

Revision ID: m1a4e7c9b3f6
Revises: l9f7d3a5b2c8
Create Date: 2026-06-11 18:00:00

Schema for Phase 1 of the inventory module.

Tables created:
  - warehouses            company-scoped; one MAIN auto-seeded per company.
  - product_variants      every product gets ≥ 1 (default migrated from sku).
  - stock_balances        cached qty + value per (variant, warehouse).
  - stock_movements       immutable audit ledger; balance_qty/value snapshot
                          captured on each row so we can rebuild.

Columns added:
  - products.is_tracked            True → stock-tracked good. False → service.
  - products.default_unit          unit label ("قطعة" etc.).
  - vendor_bill_items.variant_id   FK; required when line_type == INVENTORY.
  - vendor_bill_items.warehouse_id FK; required when line_type == INVENTORY.
  - invoice_items.variant_id       FK; required when product is tracked.
  - invoice_items.warehouse_id     FK; required when product is tracked.
  - invoice_items.unit_cost_at_sale frozen at posting time — never recomputed.
  - companies.stock_strict_mode    True → refuse sale that overdraws stock.

Plus an extra COA seed pass: every existing company gets 1140/5100/3900/5990
if missing (so old companies can use inventory without re-seeding).

Idempotent.
"""
from alembic import op
import sqlalchemy as sa


revision = "m1a4e7c9b3f6"
down_revision = "l9f7d3a5b2c8"
branch_labels = None
depends_on = None


def _ins():
    return sa.inspect(op.get_bind())


def _has_table(t):
    return t in _ins().get_table_names()


def _has_col(t, c):
    if not _has_table(t):
        return False
    return c in {col["name"] for col in _ins().get_columns(t)}


# Accounts we need everywhere. (code, en, ar, type, normal_side, parent_code)
NEEDED_ACCOUNTS = [
    ("1300", "Inventory", "المخزون", "ASSET", "DEBIT", "1100"),
    ("5100", "Cost of Goods Sold", "تكلفة البضاعة المباعة", "EXPENSE", "DEBIT", "5000"),
    ("3900", "Opening Balance Equity", "حساب الافتتاح", "EQUITY", "CREDIT", "3000"),
    ("5990", "Inventory Variance", "فروقات الجرد", "EXPENSE", "DEBIT", "5000"),
]


def _ensure_account(conn, company_id, code, name_en, name_ar,
                    acc_type, normal_side, parent_code):
    """Insert the account row if missing for this company."""
    row = conn.execute(sa.text(
        "SELECT id FROM accounts WHERE company_id=:c AND code=:k"
    ), {"c": company_id, "k": code}).fetchone()
    if row:
        return
    parent_id = None
    if parent_code:
        prow = conn.execute(sa.text(
            "SELECT id FROM accounts WHERE company_id=:c AND code=:k"
        ), {"c": company_id, "k": parent_code}).fetchone()
        parent_id = prow[0] if prow else None
    conn.execute(sa.text(
        "INSERT INTO accounts (company_id, code, name, name_ar, type, "
        "normal_side, parent_id, is_active) "
        "VALUES (:c,:k,:n,:na,:t,:s,:p,1)"
    ), {"c": company_id, "k": code, "n": name_en, "na": name_ar,
        "t": acc_type, "s": normal_side, "p": parent_id})


def upgrade():
    conn = op.get_bind()

    # ── Warehouses ─────────────────────────────────────────────────────
    if not _has_table("warehouses"):
        op.create_table(
            "warehouses",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("code", sa.String(20), nullable=False),
            sa.Column("name", sa.String(150), nullable=False),
            sa.Column("is_default", sa.Boolean, nullable=False,
                      server_default=sa.text("0")),
            sa.Column("is_active", sa.Boolean, nullable=False,
                      server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime,
                      server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("company_id", "code", name="uq_warehouse_company_code"),
        )

    # ── Product extends ────────────────────────────────────────────────
    if not _has_col("products", "is_tracked"):
        with op.batch_alter_table("products") as batch:
            batch.add_column(sa.Column(
                "is_tracked", sa.Boolean, nullable=False,
                server_default=sa.text("1"),
            ))
    if not _has_col("products", "default_unit"):
        with op.batch_alter_table("products") as batch:
            batch.add_column(sa.Column(
                "default_unit", sa.String(20),
                nullable=False, server_default="قطعة",
            ))

    # ── Product variants ───────────────────────────────────────────────
    if not _has_table("product_variants"):
        op.create_table(
            "product_variants",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("product_id", sa.Integer,
                      sa.ForeignKey("products.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("sku", sa.String(80), nullable=False),
            sa.Column("barcode", sa.String(80), nullable=True),
            sa.Column("name", sa.String(200), nullable=False,
                      server_default=""),
            sa.Column("attrs_json", sa.Text, nullable=True),
            sa.Column("unit_cost", sa.Numeric(15, 4), nullable=False,
                      server_default="0"),
            sa.Column("reorder_level", sa.Numeric(15, 2), nullable=False,
                      server_default="0"),
            sa.Column("is_active", sa.Boolean, nullable=False,
                      server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime,
                      server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("company_id", "sku", name="uq_variant_company_sku"),
            sa.UniqueConstraint("company_id", "barcode", name="uq_variant_company_barcode"),
        )

    # ── Stock balances ─────────────────────────────────────────────────
    if not _has_table("stock_balances"):
        op.create_table(
            "stock_balances",
            sa.Column("variant_id", sa.Integer,
                      sa.ForeignKey("product_variants.id",
                                    ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("warehouse_id", sa.Integer,
                      sa.ForeignKey("warehouses.id"),
                      primary_key=True),
            sa.Column("qty", sa.Numeric(15, 2), nullable=False,
                      server_default="0"),
            sa.Column("value", sa.Numeric(15, 4), nullable=False,
                      server_default="0"),
            sa.Column("updated_at", sa.DateTime,
                      server_default=sa.func.now(), nullable=False),
        )

    # ── Stock movements (the audit ledger) ─────────────────────────────
    if not _has_table("stock_movements"):
        op.create_table(
            "stock_movements",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("variant_id", sa.Integer,
                      sa.ForeignKey("product_variants.id",
                                    ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("warehouse_id", sa.Integer,
                      sa.ForeignKey("warehouses.id"),
                      nullable=False, index=True),
            sa.Column("qty_delta", sa.Numeric(15, 2), nullable=False),
            sa.Column("unit_cost_at_time", sa.Numeric(15, 4),
                      nullable=False, server_default="0"),
            sa.Column("kind", sa.String(20), nullable=False, index=True),
            sa.Column("source_type", sa.String(40), index=True),
            sa.Column("source_id", sa.Integer, index=True),
            sa.Column("journal_entry_id", sa.Integer,
                      sa.ForeignKey("journal_entries.id")),
            sa.Column("actor_id", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("reason", sa.Text),
            sa.Column("balance_qty_after", sa.Numeric(15, 2), nullable=False,
                      server_default="0"),
            sa.Column("balance_value_after", sa.Numeric(15, 4),
                      nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime,
                      server_default=sa.func.now(), nullable=False, index=True),
        )

    # ── VendorBillItem extends ─────────────────────────────────────────
    if not _has_col("vendor_bill_items", "variant_id"):
        with op.batch_alter_table("vendor_bill_items") as batch:
            batch.add_column(sa.Column(
                "variant_id", sa.Integer,
                sa.ForeignKey("product_variants.id",
                              name="fk_vbi_variant_id"),
                nullable=True,
            ))
    if not _has_col("vendor_bill_items", "warehouse_id"):
        with op.batch_alter_table("vendor_bill_items") as batch:
            batch.add_column(sa.Column(
                "warehouse_id", sa.Integer,
                sa.ForeignKey("warehouses.id",
                              name="fk_vbi_warehouse_id"),
                nullable=True,
            ))

    # ── InvoiceItem extends ────────────────────────────────────────────
    if not _has_col("invoice_items", "variant_id"):
        with op.batch_alter_table("invoice_items") as batch:
            batch.add_column(sa.Column(
                "variant_id", sa.Integer,
                sa.ForeignKey("product_variants.id",
                              name="fk_ii_variant_id"),
                nullable=True,
            ))
    if not _has_col("invoice_items", "warehouse_id"):
        with op.batch_alter_table("invoice_items") as batch:
            batch.add_column(sa.Column(
                "warehouse_id", sa.Integer,
                sa.ForeignKey("warehouses.id",
                              name="fk_ii_warehouse_id"),
                nullable=True,
            ))
    if not _has_col("invoice_items", "unit_cost_at_sale"):
        with op.batch_alter_table("invoice_items") as batch:
            batch.add_column(sa.Column(
                "unit_cost_at_sale", sa.Numeric(15, 4),
                nullable=False, server_default="0",
            ))

    # ── Company.stock_strict_mode ──────────────────────────────────────
    if not _has_col("companies", "stock_strict_mode"):
        with op.batch_alter_table("companies") as batch:
            batch.add_column(sa.Column(
                "stock_strict_mode", sa.Boolean,
                nullable=False, server_default=sa.text("1"),
            ))

    # ── Backfill: ensure 1140/5100/3900/5990 exist in every company ────
    rows = conn.execute(sa.text("SELECT id FROM companies")).fetchall()
    for (cid,) in rows:
        for code, en, ar, t, side, parent in NEEDED_ACCOUNTS:
            _ensure_account(conn, cid, code, en, ar, t, side, parent)

    # ── Backfill: seed one MAIN warehouse per company ──────────────────
    for (cid,) in rows:
        row = conn.execute(sa.text(
            "SELECT id FROM warehouses WHERE company_id=:c"
        ), {"c": cid}).fetchone()
        if row:
            continue
        conn.execute(sa.text(
            "INSERT INTO warehouses (company_id, code, name, is_default, is_active) "
            "VALUES (:c, 'MAIN', 'المخزن الرئيسي', 1, 1)"
        ), {"c": cid})

    # ── Backfill: one default variant per product, mirroring sku ───────
    prod_rows = conn.execute(sa.text(
        "SELECT id, company_id, sku, name FROM products"
    )).fetchall()
    for prod_id, company_id, sku, name in prod_rows:
        already = conn.execute(sa.text(
            "SELECT id FROM product_variants WHERE product_id=:p"
        ), {"p": prod_id}).fetchone()
        if already:
            continue
        # SKU fallback: if product.sku is null, derive "PROD-<id>"
        variant_sku = (sku or f"PROD-{prod_id}").strip()
        # Avoid sku collision per company — append product id if needed.
        clash = conn.execute(sa.text(
            "SELECT id FROM product_variants "
            "WHERE company_id=:c AND sku=:s"
        ), {"c": company_id, "s": variant_sku}).fetchone()
        if clash:
            variant_sku = f"{variant_sku}-{prod_id}"
        conn.execute(sa.text(
            "INSERT INTO product_variants "
            "(company_id, product_id, sku, name, unit_cost, reorder_level, is_active) "
            "VALUES (:c, :p, :s, :n, 0, 0, 1)"
        ), {"c": company_id, "p": prod_id, "s": variant_sku,
            "n": (name or "")[:200]})


def downgrade():
    for t in ("stock_movements", "stock_balances",
              "product_variants", "warehouses"):
        if _has_table(t):
            try:
                op.drop_table(t)
            except Exception:
                pass
    # Best-effort column drops (SQLite needs batch mode).
    for t, c in [
        ("invoice_items", "unit_cost_at_sale"),
        ("invoice_items", "warehouse_id"),
        ("invoice_items", "variant_id"),
        ("vendor_bill_items", "warehouse_id"),
        ("vendor_bill_items", "variant_id"),
        ("products", "default_unit"),
        ("products", "is_tracked"),
        ("companies", "stock_strict_mode"),
    ]:
        if _has_col(t, c):
            try:
                with op.batch_alter_table(t) as batch:
                    batch.drop_column(c)
            except Exception:
                pass
