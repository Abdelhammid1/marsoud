"""MARSOUD-ERP-01 — inventory models.

Schema layer for the Phase 1 inventory module. The integrity guarantees
(atomicity, row locking, moving-average cost, sale-time cost freeze) all
live in `app/services/inventory.py`. These models just hold data.
"""
import enum
import json
from datetime import datetime
from app import db


class Warehouse(db.Model):
    """A physical storage location. Every company has at least one
    (the MAIN warehouse, auto-seeded by the migration).

    A warehouse is referenced by every stock movement so we can answer
    "where is this unit?" in Phase 3 when transfers ship. In Phase 1 we
    always use the single default warehouse.
    """
    __tablename__ = "warehouses"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    code = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    is_default = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("company_id", "code",
                            name="uq_warehouse_company_code"),
    )

    company = db.relationship("Company", foreign_keys=[company_id])


class ProductVariant(db.Model):
    """A specific sellable SKU. Every Product has at least one (the migration
    creates a default one for existing products). Multi-variant products
    (size/color) get additional rows.

    Stock + cost always live on the variant, never on Product — that way
    queries don't need to UNION two sources.
    """
    __tablename__ = "product_variants"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    product_id = db.Column(db.Integer,
                           db.ForeignKey("products.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    sku = db.Column(db.String(80), nullable=False)
    barcode = db.Column(db.String(80))
    name = db.Column(db.String(200), default="", nullable=False)
    attrs_json = db.Column(db.Text)
    unit_cost = db.Column(db.Numeric(15, 4), default=0, nullable=False)
    reorder_level = db.Column(db.Numeric(15, 2), default=0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("company_id", "sku",
                            name="uq_variant_company_sku"),
        db.UniqueConstraint("company_id", "barcode",
                            name="uq_variant_company_barcode"),
    )

    product = db.relationship(
        "Product", foreign_keys=[product_id],
        backref=db.backref("variants",
                           order_by="ProductVariant.id.asc()",
                           cascade="all, delete-orphan"))

    @property
    def attrs(self):
        if not self.attrs_json:
            return {}
        try:
            return json.loads(self.attrs_json)
        except (TypeError, ValueError):
            return {}

    @property
    def display_name(self):
        """Product name + variant attrs ("Shirt — أحمر / M")."""
        if self.name:
            return f"{self.product.name} — {self.name}"
        return self.product.name

    def balance_in(self, warehouse):
        """Get-or-create-virtual the StockBalance row for this variant
        in a warehouse. Returns a zero-balance instance when no row
        exists yet — we don't auto-insert because the apply_stock_movement
        service handles the upsert atomically under lock.
        """
        bal = StockBalance.query.filter_by(
            variant_id=self.id, warehouse_id=warehouse.id,
        ).first()
        if bal:
            return bal
        return StockBalance(variant_id=self.id, warehouse_id=warehouse.id,
                            qty=0, value=0)

    @property
    def total_qty(self):
        from sqlalchemy import func
        v = db.session.query(func.sum(StockBalance.qty)).filter(
            StockBalance.variant_id == self.id,
        ).scalar()
        return float(v or 0)

    @property
    def total_value(self):
        from sqlalchemy import func
        v = db.session.query(func.sum(StockBalance.value)).filter(
            StockBalance.variant_id == self.id,
        ).scalar()
        return float(v or 0)

    @property
    def average_cost(self):
        q, v = self.total_qty, self.total_value
        return v / q if q > 0 else float(self.unit_cost or 0)

    @property
    def is_low_stock(self):
        return self.total_qty < float(self.reorder_level or 0)


class StockBalance(db.Model):
    """Cached qty + value per (variant, warehouse). Source of truth is the
    StockMovement ledger — this is just a denormalized aggregate that we
    keep up to date inside the same transaction as each movement.

    `recompute_balance(variant, warehouse)` in the service layer rebuilds
    this from movements when we need to heal.
    """
    __tablename__ = "stock_balances"
    variant_id = db.Column(db.Integer,
                           db.ForeignKey("product_variants.id",
                                         ondelete="CASCADE"),
                           primary_key=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"),
                             primary_key=True)
    qty = db.Column(db.Numeric(15, 2), default=0, nullable=False)
    value = db.Column(db.Numeric(15, 4), default=0, nullable=False)
    # MARSOUD-DUAL-UOM-WEIGHT-01 (Abdelhamid 2026-07-24) — parallel
    # counter for products where tracks_piece_count=True. Independent
    # of qty (the weight); no impact on cost math (avg_cost is
    # weight-based only).
    piece_count = db.Column(db.Numeric(15, 2), default=0,
                             nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    variant = db.relationship("ProductVariant", foreign_keys=[variant_id])
    warehouse = db.relationship("Warehouse", foreign_keys=[warehouse_id])

    @property
    def avg_cost(self):
        q = float(self.qty or 0)
        return float(self.value or 0) / q if q > 0 else 0.0


class StockMovementKind(str, enum.Enum):
    OPENING = "OPENING"          # First-time stock entry (Cr 3900)
    RECEIPT = "RECEIPT"          # From a vendor bill INVENTORY line
    SALE = "SALE"                # From an invoice; cost frozen at this moment
    RETURN = "RETURN"            # From a refund — restocks at original sale cost
    ADJUSTMENT = "ADJUSTMENT"    # Manual count fix; variance posts to 5990
    REVERSAL = "REVERSAL"        # Pure technical reversal (e.g. void POS)
    TRANSFER_OUT = "TRANSFER_OUT"  # ERP-03 — source side of a warehouse transfer
    TRANSFER_IN = "TRANSFER_IN"    # ERP-03 — destination side of a warehouse transfer
    # MARSOUD-REFUNDS-01 — goods sent back to the vendor. Outflow at the
    # cost the goods were received at (weighted average, same as SALE).
    PURCHASE_RETURN = "PURCHASE_RETURN"
    # MARSOUD-MANUFACTURING-01 — raw material pulled into a work order.
    PRODUCTION_ISSUE = "PRODUCTION_ISSUE"
    # MARSOUD-MANUFACTURING-01 — finished good yielded by a work order.
    PRODUCTION_RECEIPT = "PRODUCTION_RECEIPT"

    @property
    def label_ar(self):
        return {
            "OPENING": "رصيد افتتاحي",
            "RECEIPT": "استلام من مورد",
            "SALE": "بيع",
            "RETURN": "مرتجع",
            "ADJUSTMENT": "تسوية جرد",
            "REVERSAL": "إلغاء",
            "TRANSFER_OUT": "تحويل خارج",
            "TRANSFER_IN": "تحويل داخل",
            "PURCHASE_RETURN": "مرتجع مشتريات",
            "PRODUCTION_ISSUE": "صرف للإنتاج",
            "PRODUCTION_RECEIPT": "استلام إنتاج تام",
        }[self.value]

    @property
    def is_inflow(self):
        return self.value in (
            "OPENING", "RECEIPT", "RETURN", "TRANSFER_IN",
            "PRODUCTION_RECEIPT",
        )


class StockMovement(db.Model):
    """Immutable audit ledger of every change to inventory.

    Every row records: who did it (actor_id), when (created_at), what
    business event triggered it (source_type + source_id), what GL entry
    it produced (journal_entry_id), and the balance state captured AFTER
    the row was applied (balance_qty_after, balance_value_after).

    The "captured balance after" is what makes this ledger usable for
    point-in-time audit even after later movements have moved the balance.

    No update method. No delete method. Reversals create a new row with
    the opposite qty_delta and kind=REVERSAL.
    """
    __tablename__ = "stock_movements"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    variant_id = db.Column(db.Integer,
                           db.ForeignKey("product_variants.id",
                                         ondelete="CASCADE"),
                           nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"),
                             nullable=False, index=True)
    qty_delta = db.Column(db.Numeric(15, 2), nullable=False)
    unit_cost_at_time = db.Column(db.Numeric(15, 4), default=0, nullable=False)
    kind = db.Column(db.String(20), nullable=False, index=True)
    source_type = db.Column(db.String(40), index=True)
    source_id = db.Column(db.Integer, index=True)
    journal_entry_id = db.Column(db.Integer,
                                 db.ForeignKey("journal_entries.id"))
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reason = db.Column(db.Text)
    balance_qty_after = db.Column(db.Numeric(15, 2), default=0, nullable=False)
    balance_value_after = db.Column(db.Numeric(15, 4), default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False, index=True)

    variant = db.relationship("ProductVariant", foreign_keys=[variant_id])
    warehouse = db.relationship("Warehouse", foreign_keys=[warehouse_id])
    journal_entry = db.relationship("JournalEntry",
                                    foreign_keys=[journal_entry_id])
    actor = db.relationship("User", foreign_keys=[actor_id])

    @property
    def kind_enum(self):
        try:
            return StockMovementKind(self.kind)
        except ValueError:
            return None

    @property
    def signed_value(self):
        """qty_delta × unit_cost_at_time — the dollar value moved."""
        return float(self.qty_delta or 0) * float(self.unit_cost_at_time or 0)


# ─── ERP-03 — warehouse transfers ────────────────────────────────────────
class StockTransferStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    CANCELLED = "CANCELLED"

    @property
    def label_ar(self):
        return {"DRAFT": "مسودة", "POSTED": "منفّذ",
                "CANCELLED": "ملغى"}[self.value]


class StockTransfer(db.Model):
    """A transfer of stock between two warehouses. Posting does NOT
    create a GL entry — value didn't change, only location.
    """
    __tablename__ = "stock_transfers"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    number = db.Column(db.String(20), nullable=False)
    from_warehouse_id = db.Column(db.Integer,
                                  db.ForeignKey("warehouses.id"),
                                  nullable=False)
    to_warehouse_id = db.Column(db.Integer,
                                db.ForeignKey("warehouses.id"),
                                nullable=False)
    status = db.Column(db.String(20), default="DRAFT",
                       nullable=False, index=True)
    notes = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    posted_at = db.Column(db.DateTime)
    posted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    cancelled_at = db.Column(db.DateTime)
    cancelled_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    cancel_reason = db.Column(db.Text)

    __table_args__ = (
        db.UniqueConstraint("company_id", "number",
                            name="uq_transfer_company_number"),
    )

    from_warehouse = db.relationship("Warehouse",
                                     foreign_keys=[from_warehouse_id])
    to_warehouse = db.relationship("Warehouse",
                                   foreign_keys=[to_warehouse_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    posted_by = db.relationship("User", foreign_keys=[posted_by_id])

    @property
    def status_enum(self):
        try:
            return StockTransferStatus(self.status)
        except ValueError:
            return None

    @property
    def total_qty(self):
        return sum(float(i.qty or 0) for i in self.items)

    @property
    def total_value(self):
        return sum(float(i.qty or 0) * float(i.unit_cost_at_time or 0)
                   for i in self.items)


class StockTransferItem(db.Model):
    __tablename__ = "stock_transfer_items"
    id = db.Column(db.Integer, primary_key=True)
    transfer_id = db.Column(db.Integer,
                            db.ForeignKey("stock_transfers.id",
                                          ondelete="CASCADE"),
                            nullable=False, index=True)
    variant_id = db.Column(db.Integer,
                           db.ForeignKey("product_variants.id"),
                           nullable=False, index=True)
    qty = db.Column(db.Numeric(15, 2), nullable=False)
    unit_cost_at_time = db.Column(db.Numeric(15, 4), default=0, nullable=False)
    out_movement_id = db.Column(db.Integer,
                                db.ForeignKey("stock_movements.id"))
    in_movement_id = db.Column(db.Integer,
                               db.ForeignKey("stock_movements.id"))

    transfer = db.relationship(
        "StockTransfer", foreign_keys=[transfer_id],
        backref=db.backref("items", cascade="all, delete-orphan"))
    variant = db.relationship("ProductVariant", foreign_keys=[variant_id])
    out_movement = db.relationship(
        "StockMovement", foreign_keys=[out_movement_id])
    in_movement = db.relationship(
        "StockMovement", foreign_keys=[in_movement_id])


# ─── ERP-03 — FIFO lots ─────────────────────────────────────────────────
class StockLot(db.Model):
    """One row per inflow event (RECEIPT, RETURN, TRANSFER_IN, OPENING).
    Sales walk these oldest-first when Company.cost_method == FIFO.
    """
    __tablename__ = "stock_lots"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    variant_id = db.Column(db.Integer,
                           db.ForeignKey("product_variants.id",
                                         ondelete="CASCADE"),
                           nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"),
                             nullable=False, index=True)
    received_at = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False, index=True)
    qty_remaining = db.Column(db.Numeric(15, 2), default=0, nullable=False)
    unit_cost = db.Column(db.Numeric(15, 4), default=0, nullable=False)
    source_movement_id = db.Column(db.Integer,
                                   db.ForeignKey("stock_movements.id"))

    variant = db.relationship("ProductVariant", foreign_keys=[variant_id])
    warehouse = db.relationship("Warehouse", foreign_keys=[warehouse_id])
    source_movement = db.relationship(
        "StockMovement", foreign_keys=[source_movement_id])


# ─── ERP-03 — cashier shifts ─────────────────────────────────────────────
class CashierShiftStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"

    @property
    def label_ar(self):
        return {"OPEN": "مفتوحة", "CLOSED": "مقفلة"}[self.value]


class CashierShift(db.Model):
    """An optional end-of-day cash reconciliation envelope. When
    Company.shift_required_for_pos is True, the cashier must have an
    OPEN shift before /pos/ is accessible. Each POS order stamps its
    shift_id so the Z-report can summarize.
    """
    __tablename__ = "cashier_shifts"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    cashier_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                           nullable=False, index=True)
    opened_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    closed_at = db.Column(db.DateTime)
    opening_cash = db.Column(db.Numeric(15, 2), default=0, nullable=False)
    closing_cash = db.Column(db.Numeric(15, 2))
    expected_cash = db.Column(db.Numeric(15, 2))
    variance = db.Column(db.Numeric(15, 2))
    status = db.Column(db.String(10), default="OPEN", nullable=False, index=True)
    notes = db.Column(db.Text)

    cashier = db.relationship("User", foreign_keys=[cashier_id])
    invoices = db.relationship("Invoice",
                               foreign_keys="Invoice.shift_id",
                               backref="shift")

    @property
    def status_enum(self):
        try:
            return CashierShiftStatus(self.status)
        except ValueError:
            return None

    @property
    def is_open(self):
        return self.status == "OPEN"


# ─── MARSOUD-DUAL-UOM-WEIGHT-01 (Abdelhamid 2026-07-24) ──────────
INV_COUNT_DRAFT = "DRAFT"
INV_COUNT_CONFIRMED = "CONFIRMED"


class InventoryCount(db.Model):
    """Physical stock count vs the book balance for a single
    (variant, warehouse). Confirmation posts a stock adjustment for
    the weight variance + a direct piece_count adjustment for the
    piece variance."""
    __tablename__ = "inventory_counts"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    variant_id = db.Column(db.Integer,
                            db.ForeignKey("product_variants.id"),
                            nullable=False)
    warehouse_id = db.Column(db.Integer,
                              db.ForeignKey("warehouses.id"),
                              nullable=False)
    book_qty = db.Column(db.Numeric(15, 2), nullable=False)
    book_pieces = db.Column(db.Numeric(15, 2), nullable=False,
                              default=0)
    counted_qty = db.Column(db.Numeric(15, 2), nullable=False)
    counted_pieces = db.Column(db.Numeric(15, 2), nullable=False,
                                 default=0)
    variance_qty = db.Column(db.Numeric(15, 2), nullable=False,
                              default=0)
    variance_pieces = db.Column(db.Numeric(15, 2), nullable=False,
                                 default=0)
    status = db.Column(db.String(20), nullable=False,
                       default=INV_COUNT_DRAFT)
    counted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                              nullable=True)
    counted_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)
    confirmed_at = db.Column(db.DateTime, nullable=True)
    adjustment_movement_id = db.Column(
        db.Integer, db.ForeignKey("stock_movements.id"), nullable=True)

    variant = db.relationship("ProductVariant",
                                foreign_keys=[variant_id])
    warehouse = db.relationship("Warehouse",
                                  foreign_keys=[warehouse_id])
    adjustment_movement = db.relationship(
        "StockMovement", foreign_keys=[adjustment_movement_id])
