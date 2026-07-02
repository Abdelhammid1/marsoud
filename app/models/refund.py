import enum
from datetime import datetime, date
from app import db


class RefundType(enum.Enum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    CREDIT_NOTE = "CREDIT_NOTE"


# MARSOUD-REFUNDS-01 — mirror on the purchase side. Same three shapes,
# different terminology (debit note = the money the vendor now owes us).
class VendorRefundType(enum.Enum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    DEBIT_NOTE = "DEBIT_NOTE"

    @property
    def label_ar(self):
        return {
            "FULL": "مرتجع كامل",
            "PARTIAL": "مرتجع جزئي",
            "DEBIT_NOTE": "إشعار مدين",
        }[self.value]


class Refund(db.Model):
    __tablename__ = "refunds"
    id = db.Column(db.Integer, primary_key=True)
    # MARSOUD-REFUNDS-01 — company + number gained for the new /refunds
    # page + RET-nnnn ordering. Nullable so pre-ticket rows read cleanly.
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                            nullable=True, index=True)
    number = db.Column(db.String(30), nullable=True, index=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    type = db.Column(db.Enum(RefundType), nullable=False)
    amount = db.Column(db.Numeric(15, 4), nullable=False)
    reason = db.Column(db.Text)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.now)

    invoice = db.relationship("Invoice", backref="refunds")
    company = db.relationship("Company")


class CreditNote(db.Model):
    __tablename__ = "credit_notes"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    amount = db.Column(db.Numeric(15, 4), nullable=False)
    used_amount = db.Column(db.Numeric(15, 4), default=0)
    reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)

    company = db.relationship("Company")
    customer = db.relationship("Customer", backref="credit_notes")
    invoice = db.relationship("Invoice")

    @property
    def balance(self):
        return float(self.amount) - float(self.used_amount or 0)


class VendorBillRefund(db.Model):
    """MARSOUD-REFUNDS-01 — purchase-side refund.

    Mirrors Refund but keyed to a VendorBill instead of an Invoice.
    Same three shapes (FULL / PARTIAL / DEBIT_NOTE) — DEBIT_NOTE
    accumulates a balance we can apply against a future bill from the
    same vendor (analogous to CreditNote on the sales side)."""
    __tablename__ = "vendor_bill_refunds"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                            nullable=False, index=True)
    number = db.Column(db.String(30), nullable=True, index=True)
    bill_id = db.Column(db.Integer, db.ForeignKey("vendor_bills.id"),
                          nullable=False, index=True)
    type = db.Column(db.Enum(VendorRefundType), nullable=False)
    amount = db.Column(db.Numeric(15, 4), nullable=False)
    reason = db.Column(db.Text)
    journal_entry_id = db.Column(db.Integer,
                                    db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.now)

    bill = db.relationship("VendorBill", backref="refunds")
    company = db.relationship("Company")


class DebitNote(db.Model):
    """MARSOUD-REFUNDS-01 — analog of CreditNote on the purchase side.

    Balance the vendor owes us; can be netted against a future bill by
    the same vendor. Auto-created when a VendorBillRefund of type
    DEBIT_NOTE is issued."""
    __tablename__ = "debit_notes"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                            nullable=False, index=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"),
                            nullable=False)
    bill_id = db.Column(db.Integer, db.ForeignKey("vendor_bills.id"))
    amount = db.Column(db.Numeric(15, 4), nullable=False)
    used_amount = db.Column(db.Numeric(15, 4), default=0)
    reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)

    company = db.relationship("Company")
    vendor = db.relationship("Vendor", backref="debit_notes")
    bill = db.relationship("VendorBill")

    @property
    def balance(self):
        return float(self.amount) - float(self.used_amount or 0)
