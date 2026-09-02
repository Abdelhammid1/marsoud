import enum
from datetime import datetime, date
from app import db


class VendorBillStatus(enum.Enum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    OVERDUE = "OVERDUE"
    CANCELLED = "CANCELLED"
    # MARSOUD-VBILL-REFUND-STATUS — a purchase return posted the journal
    # but left the bill looking live, so its full value kept inflating
    # إجمالي المشتريات. Mirrors InvoiceStatus on the customer side.
    REFUNDED = "REFUNDED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"


class VendorBillPaymentMethod(enum.Enum):
    CASH = "CASH"
    BANK = "BANK"
    CREDIT = "CREDIT"   # سيُحتسب على المورد (Accounts Payable)


class BillLineType(enum.Enum):
    EXPENSE = "EXPENSE"
    FIXED_ASSET = "FIXED_ASSET"
    INVENTORY = "INVENTORY"


class VendorBill(db.Model):
    """A purchase / vendor bill — one document containing mixed expense/asset/inventory lines."""
    __tablename__ = "vendor_bills"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    number = db.Column(db.String(20), index=True, nullable=False)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"))  # required when payment_method=CREDIT
    supplier_invoice_number = db.Column(db.String(50))
    issue_date = db.Column(db.Date, default=date.today, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    payment_method = db.Column(db.Enum(VendorBillPaymentMethod), default=VendorBillPaymentMethod.CASH, nullable=False)
    currency = db.Column(db.String(3), default="SAR")

    subtotal = db.Column(db.Numeric(15, 4), default=0)
    tax_rate = db.Column(db.Numeric(5, 2), default=0)
    tax_amount = db.Column(db.Numeric(15, 4), default=0)
    total = db.Column(db.Numeric(15, 4), default=0)
    paid_amount = db.Column(db.Numeric(15, 4), default=0)
    status = db.Column(db.Enum(VendorBillStatus), default=VendorBillStatus.DRAFT, nullable=False)

    notes = db.Column(db.Text)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # MARSOUD-52 — soft delete, DRAFT only
    deleted_at = db.Column(db.DateTime, nullable=True)
    deleted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    # MARSOUD-VBILL-OVERDUE-01 (2026-08-06) — cron materialises recurring
    # vendor-bill forecasts into real POSTED bills the moment their due
    # date arrives, mirroring process_recurring_invoices on the customer
    # side. These two columns are the idempotency guarantee: a unique
    # index on (recurring_bill_id, recurring_occurrence_date) means the
    # cron cannot double-post if it fires twice on the same day.
    # NULL on both columns for hand-entered bills — SQLite + Postgres
    # both treat NULL as distinct in UNIQUE, so those rows are exempt.
    recurring_bill_id = db.Column(
        db.Integer, db.ForeignKey("recurring_bills.id"),
        nullable=True, index=True)
    recurring_occurrence_date = db.Column(db.Date, nullable=True)

    # Postpone audit trail — set by postpone_bill(). previous_due_date
    # holds the ORIGINAL date the bill was moved off; a second postpone
    # overwrites with the second-to-last date, which is fine because
    # every postpone also stamps postponed_at, and the full history
    # is in the standard UserActivityLog trail.
    previous_due_date = db.Column(db.Date, nullable=True)
    postpone_reason = db.Column(db.Text, nullable=True)
    postponed_by = db.Column(db.Integer, db.ForeignKey("users.id"),
                             nullable=True)
    postponed_at = db.Column(db.DateTime, nullable=True)

    # MARSOUD-PURCHASE-ORDERS-01 — optional back-link to the PO that
    # spawned this bill. NULL for hand-entered bills. Populated by
    # `?from_po=<id>` prefill flow, and used by the `_apply_bill_to_po`
    # hook in `services/vendor_bills.py:post_vendor_bill` to bump
    # qty_invoiced + refuse over-invoicing.
    purchase_order_id = db.Column(
        db.Integer, db.ForeignKey("purchase_orders.id"),
        nullable=True, index=True)

    company = db.relationship("Company", backref=db.backref("vendor_bills", lazy="dynamic"))
    vendor = db.relationship("Vendor", backref=db.backref("bills", lazy="dynamic"))
    items = db.relationship("VendorBillItem", backref="bill", cascade="all, delete-orphan")
    payments = db.relationship("VendorBillPayment", backref="bill", cascade="all, delete-orphan")
    deleted_by = db.relationship("User", foreign_keys=[deleted_by_id])
    postponer = db.relationship("User", foreign_keys=[postponed_by])
    # MARSOUD-PURCHASE-ORDERS-01 — reverse side of the FK above.
    purchase_order = db.relationship(
        "PurchaseOrder", foreign_keys=[purchase_order_id])
    recurring_bill = db.relationship(
        "RecurringBill", foreign_keys=[recurring_bill_id])

    __table_args__ = (
        db.UniqueConstraint("company_id", "number", name="uq_vendor_bill_number"),
        db.Index("uq_vendor_bill_recurring_occurrence",
                 "recurring_bill_id", "recurring_occurrence_date",
                 unique=True),
    )

    @property
    def balance(self):
        return float(self.total or 0) - float(self.paid_amount or 0)

    def recalc(self):
        self.subtotal = sum(float(i.quantity or 0) * float(i.unit_price or 0) for i in self.items)
        for item in self.items:
            item.line_total = float(item.quantity or 0) * float(item.unit_price or 0)
        self.tax_amount = float(self.subtotal) * float(self.tax_rate or 0) / 100.0
        self.total = float(self.subtotal) + float(self.tax_amount)


class VendorBillItem(db.Model):
    __tablename__ = "vendor_bill_items"
    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey("vendor_bills.id"), nullable=False)
    description = db.Column(db.String(255), nullable=False)
    line_type = db.Column(db.Enum(BillLineType), nullable=False, default=BillLineType.EXPENSE)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    quantity = db.Column(db.Numeric(10, 3), default=1)
    unit_price = db.Column(db.Numeric(15, 4), default=0)
    line_total = db.Column(db.Numeric(15, 4), default=0)
    # Fixed-asset specific (only used when line_type == FIXED_ASSET)
    useful_life_years = db.Column(db.Integer)
    salvage_value = db.Column(db.Numeric(15, 4), default=0)
    created_asset_id = db.Column(db.Integer, db.ForeignKey("fixed_assets.id"))   # set after posting

    # ERP-01 — inventory line targets. Required when line_type == INVENTORY.
    variant_id = db.Column(db.Integer, db.ForeignKey("product_variants.id"))
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"))

    # MARSOUD-UNIT-CONVERSION-01 — see InvoiceItem.unit_id note.
    unit_id = db.Column(db.Integer, db.ForeignKey("product_units.id"))
    base_quantity = db.Column(db.Numeric(15, 4))

    # MARSOUD-VENDOR-SUBCAT (Abdelhamid 2026-07-14) — per-vendor
    # sub-category. Nullable so legacy bill lines keep working; also
    # nullable when the vendor has no sub-categories defined yet.
    sub_category_id = db.Column(db.Integer,
                                 db.ForeignKey("vendor_sub_categories.id"),
                                 nullable=True, index=True)

    account = db.relationship("Account")
    created_asset = db.relationship("FixedAsset")
    variant = db.relationship("ProductVariant", foreign_keys=[variant_id])
    warehouse = db.relationship("Warehouse", foreign_keys=[warehouse_id])
    unit = db.relationship("ProductUnit", foreign_keys=[unit_id])
    sub_category = db.relationship("VendorSubCategory",
                                    foreign_keys=[sub_category_id])


class VendorBillPayment(db.Model):
    __tablename__ = "vendor_bill_payments"
    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey("vendor_bills.id"), nullable=False)
    amount = db.Column(db.Numeric(15, 4), nullable=False)
    payment_date = db.Column(db.Date, default=date.today, nullable=False)
    payment_method_id = db.Column(db.Integer, db.ForeignKey("payment_methods.id"))
    method = db.Column(db.String(30), default="cash")
    notes = db.Column(db.Text)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    payment_method = db.relationship("PaymentMethod")
