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
    created_at = db.Column(db.DateTime, default=datetime.now)
    # MARSOUD-52 — soft delete, DRAFT only
    deleted_at = db.Column(db.DateTime, nullable=True)
    deleted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    company = db.relationship("Company", backref=db.backref("vendor_bills", lazy="dynamic"))
    vendor = db.relationship("Vendor", backref=db.backref("bills", lazy="dynamic"))
    items = db.relationship("VendorBillItem", backref="bill", cascade="all, delete-orphan")
    payments = db.relationship("VendorBillPayment", backref="bill", cascade="all, delete-orphan")
    deleted_by = db.relationship("User", foreign_keys=[deleted_by_id])

    __table_args__ = (
        db.UniqueConstraint("company_id", "number", name="uq_vendor_bill_number"),
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
    quantity = db.Column(db.Numeric(10, 2), default=1)
    unit_price = db.Column(db.Numeric(15, 4), default=0)
    line_total = db.Column(db.Numeric(15, 4), default=0)
    # Fixed-asset specific (only used when line_type == FIXED_ASSET)
    useful_life_years = db.Column(db.Integer)
    salvage_value = db.Column(db.Numeric(15, 4), default=0)
    created_asset_id = db.Column(db.Integer, db.ForeignKey("fixed_assets.id"))   # set after posting

    # ERP-01 — inventory line targets. Required when line_type == INVENTORY.
    variant_id = db.Column(db.Integer, db.ForeignKey("product_variants.id"))
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"))

    account = db.relationship("Account")
    created_asset = db.relationship("FixedAsset")
    variant = db.relationship("ProductVariant", foreign_keys=[variant_id])
    warehouse = db.relationship("Warehouse", foreign_keys=[warehouse_id])


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
    created_at = db.Column(db.DateTime, default=datetime.now)

    payment_method = db.relationship("PaymentMethod")
