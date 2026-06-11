"""MARSOUD-ERP-01 Phase 3 — transfers + lots + shifts + company toggles

Revision ID: o3c9f6d8e2a4
Revises: n2b8f5d4e1a9
Create Date: 2026-06-11 23:00:00

ALL Phase 3 schema in one migration (nothing was pushed yet, per the
user's instruction "if there is a migrations it will be in one shot").

New tables:
  - stock_transfers       header (from_warehouse → to_warehouse) + status.
  - stock_transfer_items  line: variant + qty + snapshot cost + the two
                          movement IDs (out / in) that resulted from posting.
  - stock_lots            per-inflow lot for FIFO costing.
  - cashier_shifts        cashier opens at start of day, closes at end with
                          variance vs expected cash.

Columns added:
  - companies.cost_method            AVERAGE / FIFO. Default AVERAGE.
  - companies.shift_required_for_pos Boolean default False.
  - companies.barcode_format         CODE128 default (plumbed for EAN/QR).
  - invoices.shift_id                FK → cashier_shifts.id, nullable.

Backfill:
  - Every existing StockBalance row with qty > 0 → insert one synthetic
    StockLot at the balance's current moving-average cost. So switching
    cost_method to FIFO mid-life doesn't lose history.

Idempotent.
"""
from alembic import op
import sqlalchemy as sa


revision = "o3c9f6d8e2a4"
down_revision = "n2b8f5d4e1a9"
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


def upgrade():
    conn = op.get_bind()

    # ── Company columns ────────────────────────────────────────────────
    if not _has_col("companies", "cost_method"):
        with op.batch_alter_table("companies") as batch:
            batch.add_column(sa.Column(
                "cost_method", sa.String(10),
                nullable=False, server_default="AVERAGE",
            ))
    if not _has_col("companies", "shift_required_for_pos"):
        with op.batch_alter_table("companies") as batch:
            batch.add_column(sa.Column(
                "shift_required_for_pos", sa.Boolean,
                nullable=False, server_default=sa.text("0"),
            ))
    if not _has_col("companies", "barcode_format"):
        with op.batch_alter_table("companies") as batch:
            batch.add_column(sa.Column(
                "barcode_format", sa.String(20),
                nullable=False, server_default="CODE128",
            ))

    # ── stock_transfers ────────────────────────────────────────────────
    if not _has_table("stock_transfers"):
        op.create_table(
            "stock_transfers",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("number", sa.String(20), nullable=False),
            sa.Column("from_warehouse_id", sa.Integer,
                      sa.ForeignKey("warehouses.id"), nullable=False),
            sa.Column("to_warehouse_id", sa.Integer,
                      sa.ForeignKey("warehouses.id"), nullable=False),
            sa.Column("status", sa.String(20), nullable=False,
                      server_default="DRAFT", index=True),
            sa.Column("notes", sa.Text),
            sa.Column("created_by_id", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("created_at", sa.DateTime,
                      server_default=sa.func.now(), nullable=False),
            sa.Column("posted_at", sa.DateTime),
            sa.Column("posted_by_id", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("cancelled_at", sa.DateTime),
            sa.Column("cancelled_by_id", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("cancel_reason", sa.Text),
            sa.UniqueConstraint("company_id", "number",
                                name="uq_transfer_company_number"),
        )

    # ── stock_transfer_items ───────────────────────────────────────────
    if not _has_table("stock_transfer_items"):
        op.create_table(
            "stock_transfer_items",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("transfer_id", sa.Integer,
                      sa.ForeignKey("stock_transfers.id",
                                    ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("variant_id", sa.Integer,
                      sa.ForeignKey("product_variants.id"),
                      nullable=False, index=True),
            sa.Column("qty", sa.Numeric(15, 2), nullable=False),
            sa.Column("unit_cost_at_time", sa.Numeric(15, 4),
                      nullable=False, server_default="0"),
            sa.Column("out_movement_id", sa.Integer,
                      sa.ForeignKey("stock_movements.id")),
            sa.Column("in_movement_id", sa.Integer,
                      sa.ForeignKey("stock_movements.id")),
        )

    # ── stock_lots (FIFO) ──────────────────────────────────────────────
    if not _has_table("stock_lots"):
        op.create_table(
            "stock_lots",
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
            sa.Column("received_at", sa.DateTime,
                      server_default=sa.func.now(), nullable=False, index=True),
            sa.Column("qty_remaining", sa.Numeric(15, 2),
                      nullable=False, server_default="0"),
            sa.Column("unit_cost", sa.Numeric(15, 4),
                      nullable=False, server_default="0"),
            sa.Column("source_movement_id", sa.Integer,
                      sa.ForeignKey("stock_movements.id")),
        )

    # ── cashier_shifts ─────────────────────────────────────────────────
    if not _has_table("cashier_shifts"):
        op.create_table(
            "cashier_shifts",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("cashier_id", sa.Integer,
                      sa.ForeignKey("users.id"),
                      nullable=False, index=True),
            sa.Column("opened_at", sa.DateTime,
                      server_default=sa.func.now(), nullable=False),
            sa.Column("closed_at", sa.DateTime),
            sa.Column("opening_cash", sa.Numeric(15, 2),
                      nullable=False, server_default="0"),
            sa.Column("closing_cash", sa.Numeric(15, 2)),
            sa.Column("expected_cash", sa.Numeric(15, 2)),
            sa.Column("variance", sa.Numeric(15, 2)),
            sa.Column("status", sa.String(10), nullable=False,
                      server_default="OPEN", index=True),
            sa.Column("notes", sa.Text),
        )

    # ── invoices.shift_id ──────────────────────────────────────────────
    if not _has_col("invoices", "shift_id"):
        with op.batch_alter_table("invoices") as batch:
            batch.add_column(sa.Column(
                "shift_id", sa.Integer,
                sa.ForeignKey("cashier_shifts.id",
                              name="fk_invoices_shift_id"),
                nullable=True,
            ))

    # ── Backfill: one synthetic lot per non-zero balance ───────────────
    # Only run when stock_lots is empty (idempotency safety).
    if _has_table("stock_lots") and _has_table("stock_balances"):
        existing = conn.execute(
            sa.text("SELECT COUNT(*) FROM stock_lots")
        ).scalar() or 0
        if existing == 0:
            rows = conn.execute(sa.text("""
                SELECT b.variant_id, b.warehouse_id, b.qty, b.value,
                       b.updated_at, v.company_id
                FROM stock_balances b
                JOIN product_variants v ON v.id = b.variant_id
                WHERE b.qty > 0
            """)).fetchall()
            for r in rows:
                qty = float(r[2] or 0)
                if qty <= 0:
                    continue
                unit_cost = float(r[3] or 0) / qty
                conn.execute(sa.text("""
                    INSERT INTO stock_lots
                    (company_id, variant_id, warehouse_id, received_at,
                     qty_remaining, unit_cost)
                    VALUES (:c, :v, :w, :r, :q, :uc)
                """), {
                    "c": r[5], "v": r[0], "w": r[1],
                    "r": r[4], "q": qty, "uc": unit_cost,
                })


def downgrade():
    for t in ("cashier_shifts", "stock_lots",
              "stock_transfer_items", "stock_transfers"):
        if _has_table(t):
            try:
                op.drop_table(t)
            except Exception:
                pass
    for tbl, col in [
        ("invoices", "shift_id"),
        ("companies", "barcode_format"),
        ("companies", "shift_required_for_pos"),
        ("companies", "cost_method"),
    ]:
        if _has_col(tbl, col):
            try:
                with op.batch_alter_table(tbl) as batch:
                    batch.drop_column(col)
            except Exception:
                pass
