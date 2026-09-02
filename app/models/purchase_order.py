"""MARSOUD-PURCHASE-ORDERS-01 (2026-09-02) — أوامر الشراء.

Three stages upstream of the vendor bill: طلب شراء → اعتماد
(أمر الشراء) → إذن استلام (GRN). The GRN itself posts no JE and
moves no stock — VendorBill remains the single source of both.
See ticket §2 for the critical design decision. Do not add
inventory or ledger side-effects here.
"""
import enum
from datetime import datetime, date
from app import db
from app.models.vendor_bill import BillLineType  # reuse — no duplicate enum


class PurchaseOrderStatus(enum.Enum):
    REQUESTED = "REQUESTED"            # طلب شراء داخلي
    APPROVED = "APPROVED"              # أمر شراء رسمي (مُعتمَد)
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"              # كل الكميات وصلت
    CLOSED = "CLOSED"                  # كل الكميات المستلمة اتفوترت
    REJECTED = "REJECTED"              # اترفض قبل الاعتماد
    CANCELLED = "CANCELLED"            # اتلغى بعد الاعتماد قبل أي GRN


class PurchaseOrder(db.Model):
    __tablename__ = "purchase_orders"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                            db.ForeignKey("companies.id"),
                            nullable=False, index=True)
    number = db.Column(db.String(20), index=True, nullable=False)
    vendor_id = db.Column(db.Integer,
                           db.ForeignKey("vendors.id"),
                           nullable=False)
    status = db.Column(db.Enum(PurchaseOrderStatus),
                        default=PurchaseOrderStatus.REQUESTED,
                        nullable=False, index=True)
    currency = db.Column(db.String(3), default="SAR")

    issue_date = db.Column(db.Date, default=date.today, nullable=False)
    expected_date = db.Column(db.Date, nullable=True)

    subtotal = db.Column(db.Numeric(15, 4), default=0)
    tax_rate = db.Column(db.Numeric(5, 2), default=0)
    tax_amount = db.Column(db.Numeric(15, 4), default=0)
    total = db.Column(db.Numeric(15, 4), default=0)

    notes = db.Column(db.Text)

    requested_by_id = db.Column(db.Integer,
                                 db.ForeignKey("users.id"),
                                 nullable=False)
    approved_by_id = db.Column(db.Integer,
                                db.ForeignKey("users.id"),
                                nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)

    rejected_reason = db.Column(db.Text, nullable=True)
    cancelled_reason = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)

    # Soft delete — REQUESTED only (mirrors MARSOUD-52 DRAFT-only)
    deleted_at = db.Column(db.DateTime, nullable=True)
    deleted_by_id = db.Column(db.Integer,
                               db.ForeignKey("users.id"),
                               nullable=True)

    company = db.relationship(
        "Company",
        backref=db.backref("purchase_orders", lazy="dynamic"))
    vendor = db.relationship(
        "Vendor",
        backref=db.backref("purchase_orders", lazy="dynamic"))
    requested_by = db.relationship(
        "User", foreign_keys=[requested_by_id])
    approved_by = db.relationship(
        "User", foreign_keys=[approved_by_id])
    deleted_by = db.relationship(
        "User", foreign_keys=[deleted_by_id])
    items = db.relationship(
        "PurchaseOrderItem",
        backref="purchase_order",
        cascade="all, delete-orphan")

    __table_args__ = (
        db.UniqueConstraint("company_id", "number",
                             name="uq_purchase_order_number"),
    )

    def recalc(self):
        """Refresh subtotal / tax_amount / total + each item's
        line_total. Mirrors VendorBill.recalc()."""
        self.subtotal = sum(
            float(i.quantity or 0) * float(i.unit_price or 0)
            for i in self.items)
        for item in self.items:
            item.line_total = (float(item.quantity or 0)
                                * float(item.unit_price or 0))
        self.tax_amount = (float(self.subtotal)
                            * float(self.tax_rate or 0) / 100.0)
        self.total = float(self.subtotal) + float(self.tax_amount)

    @property
    def is_fully_received(self):
        return all(float(i.qty_received or 0) >= float(i.quantity or 0)
                    for i in self.items)

    @property
    def is_fully_invoiced(self):
        return all(float(i.qty_invoiced or 0) >= float(i.quantity or 0)
                    for i in self.items)

    @property
    def status_ar(self):
        return {
            "REQUESTED":            "طلب",
            "APPROVED":             "مُعتمَد",
            "PARTIALLY_RECEIVED":   "مُستلَم جزئيًا",
            "RECEIVED":             "مُستلَم بالكامل",
            "CLOSED":               "مغلق",
            "REJECTED":             "مرفوض",
            "CANCELLED":            "ملغى",
        }.get(getattr(self.status, "value", str(self.status)),
              str(self.status))


class PurchaseOrderItem(db.Model):
    __tablename__ = "purchase_order_items"

    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(
        db.Integer,
        db.ForeignKey("purchase_orders.id"),
        nullable=False)
    description = db.Column(db.String(255), nullable=False)
    line_type = db.Column(db.Enum(BillLineType),
                           nullable=False,
                           default=BillLineType.INVENTORY)
    variant_id = db.Column(db.Integer,
                            db.ForeignKey("product_variants.id"),
                            nullable=True)
    warehouse_id = db.Column(db.Integer,
                              db.ForeignKey("warehouses.id"),
                              nullable=True)
    unit_id = db.Column(db.Integer,
                         db.ForeignKey("product_units.id"),
                         nullable=True)

    quantity = db.Column(db.Numeric(15, 3), nullable=False)
    unit_price = db.Column(db.Numeric(15, 4), default=0)
    line_total = db.Column(db.Numeric(15, 4), default=0)

    # Cumulative counters — ONLY the service touches these, never a form.
    qty_received = db.Column(db.Numeric(15, 3),
                              default=0, nullable=False)
    qty_invoiced = db.Column(db.Numeric(15, 3),
                              default=0, nullable=False)

    variant = db.relationship("ProductVariant",
                                foreign_keys=[variant_id])
    warehouse = db.relationship("Warehouse",
                                 foreign_keys=[warehouse_id])
    unit = db.relationship("ProductUnit",
                            foreign_keys=[unit_id])

    @property
    def qty_remaining_to_receive(self):
        return max(float(self.quantity or 0)
                    - float(self.qty_received or 0), 0)

    @property
    def qty_remaining_to_invoice(self):
        return max(float(self.qty_received or 0)
                    - float(self.qty_invoiced or 0), 0)
