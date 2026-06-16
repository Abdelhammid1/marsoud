import enum
from datetime import datetime, date
from app import db


class InvoiceStatus(enum.Enum):
    DRAFT = "DRAFT"
    SENT = "SENT"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    OVERDUE = "OVERDUE"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    VOIDED = "VOIDED"   # ERP-02 — POS order fully reversed


class DiscountType(enum.Enum):
    NONE = "NONE"
    PERCENT = "PERCENT"
    FIXED = "FIXED"


class Invoice(db.Model):
    __tablename__ = "invoices"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    number = db.Column(db.String(20), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True)
    shift_id = db.Column(db.Integer, db.ForeignKey("cashier_shifts.id"), nullable=True, index=True)
    issue_date = db.Column(db.Date, default=date.today, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    currency = db.Column(db.String(3), default="SAR")
    subtotal = db.Column(db.Numeric(15, 4), default=0)
    invoice_discount_type = db.Column(db.Enum(DiscountType), default=DiscountType.NONE)
    invoice_discount_value = db.Column(db.Numeric(15, 4), default=0)
    invoice_discount_amount = db.Column(db.Numeric(15, 4), default=0)  # resolved value
    taxable_base = db.Column(db.Numeric(15, 4), default=0)
    tax_rate = db.Column(db.Numeric(5, 2), default=15.00)
    tax_amount = db.Column(db.Numeric(15, 4), default=0)
    total = db.Column(db.Numeric(15, 4), default=0)
    paid_amount = db.Column(db.Numeric(15, 4), default=0)
    status = db.Column(db.Enum(InvoiceStatus), default=InvoiceStatus.DRAFT, nullable=False)
    notes = db.Column(db.Text)              # customer-facing
    internal_notes = db.Column(db.Text)     # private to the company
    send_reminders = db.Column(db.Boolean, default=True)

    # ERP-02 — POS-specific fields. Columns exist in DB from migration
    # n2b8f5d4e1a9 (POS phase 2).
    source = db.Column(db.String(20), default="MANUAL", nullable=False, index=True)
    cashier_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    cash_received = db.Column(db.Numeric(15, 4))
    voided_at = db.Column(db.DateTime)
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    void_reason = db.Column(db.Text)

    # MARSOUD-51 — optional PDF fields
    po_reference = db.Column(db.String(100))
    sales_rep_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    payment_terms_days = db.Column(db.Integer)

    created_at = db.Column(db.DateTime, default=datetime.now)

    cashier = db.relationship("User", foreign_keys=[cashier_id])
    voided_by = db.relationship("User", foreign_keys=[voided_by_id])
    sales_rep = db.relationship("User", foreign_keys=[sales_rep_id])

    company = db.relationship("Company", backref=db.backref("invoices", lazy="dynamic"))
    customer = db.relationship("Customer", backref=db.backref("invoices", lazy="dynamic"))
    items = db.relationship("InvoiceItem", backref="invoice", cascade="all, delete-orphan")
    payments = db.relationship("Payment", backref="invoice", cascade="all, delete-orphan")

    __table_args__ = (
        db.UniqueConstraint("company_id", "number", name="uq_company_invoice_number"),
    )

    @property
    def balance(self):
        return float(self.total or 0) - float(self.paid_amount or 0)

    @property
    def is_pos(self):
        return self.source == "POS"

    @property
    def is_voided(self):
        return self.status == InvoiceStatus.VOIDED

    @property
    def change_due(self):
        if self.cash_received is None:
            return None
        return float(self.cash_received) - float(self.total or 0)

    def recalc(self):
        """Compute totals respecting line-level and invoice-level discounts.
        Tax is applied AFTER discounts (per Saudi/Egyptian VAT law).

        Flow: line_subtotal → line_discount → items_total → invoice_discount
              → taxable_base → tax_amount → total
        """
        items_total = 0.0
        for item in self.items:
            line_sub = float(item.quantity or 0) * float(item.unit_price or 0)
            line_disc = _resolve_discount(item.discount_type, item.discount_value, line_sub)
            item.line_total = line_sub - line_disc
            items_total += item.line_total
        self.subtotal = items_total

        inv_disc = _resolve_discount(self.invoice_discount_type, self.invoice_discount_value, items_total)
        self.invoice_discount_amount = inv_disc
        self.taxable_base = items_total - inv_disc
        self.tax_amount = float(self.taxable_base) * float(self.tax_rate or 0) / 100.0
        self.total = float(self.taxable_base) + float(self.tax_amount)


def _resolve_discount(dtype, value, base):
    """Convert a discount spec to an absolute amount, clamped to [0, base]."""
    if not dtype or dtype == DiscountType.NONE or not value:
        return 0.0
    v = float(value)
    if dtype == DiscountType.PERCENT:
        amt = base * v / 100.0
    else:
        amt = v
    return max(0.0, min(amt, base))


class InvoiceItem(db.Model):
    __tablename__ = "invoice_items"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"))
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(10, 2), default=1)
    unit_price = db.Column(db.Numeric(15, 4), default=0)
    discount_type = db.Column(db.Enum(DiscountType), default=DiscountType.NONE)
    discount_value = db.Column(db.Numeric(15, 4), default=0)
    line_total = db.Column(db.Numeric(15, 4), default=0)

    # ERP-01 — inventory line targets + frozen-at-sale cost basis.
    # Columns exist in DB from migration m1a4e7c9b3f6.
    variant_id = db.Column(db.Integer, db.ForeignKey("product_variants.id"))
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"))
    unit_cost_at_sale = db.Column(db.Numeric(15, 4), default=0, nullable=False)

    product = db.relationship("Product")
    variant = db.relationship("ProductVariant", foreign_keys=[variant_id])
    warehouse = db.relationship("Warehouse", foreign_keys=[warehouse_id])

    @property
    def gross(self):
        return float(self.quantity or 0) * float(self.unit_price or 0)

    @property
    def total(self):
        # Backward-compat — pre-discount gross
        return self.gross


class InvoiceReminderSent(db.Model):
    """Tracks which reminder thresholds have already fired for an invoice.

    threshold_kind: 'before' (days before due) or 'overdue' (days after due).
    threshold_days: integer. Together with kind they uniquely identify a reminder
    type so it doesn't fire twice.
    """
    __tablename__ = "invoice_reminders_sent"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False, index=True)
    threshold_kind = db.Column(db.String(10), nullable=False)
    threshold_days = db.Column(db.Integer, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    invoice = db.relationship("Invoice", backref=db.backref("reminders_sent", cascade="all, delete-orphan"))

    __table_args__ = (
        db.UniqueConstraint("invoice_id", "threshold_kind", "threshold_days",
                            name="uq_invoice_reminder_threshold"),
    )


class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    amount = db.Column(db.Numeric(15, 4), nullable=False)
    payment_date = db.Column(db.Date, default=date.today, nullable=False)
    payment_method_id = db.Column(db.Integer, db.ForeignKey("payment_methods.id"))
    method = db.Column(db.String(30), default="cash")  # historical fallback
    notes = db.Column(db.Text)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.now)

    payment_method = db.relationship("PaymentMethod")
