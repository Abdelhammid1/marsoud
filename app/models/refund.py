import enum
from datetime import datetime, date
from app import db


class RefundType(enum.Enum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    CREDIT_NOTE = "CREDIT_NOTE"


class Refund(db.Model):
    __tablename__ = "refunds"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    type = db.Column(db.Enum(RefundType), nullable=False)
    amount = db.Column(db.Numeric(15, 4), nullable=False)
    reason = db.Column(db.Text)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.now)

    invoice = db.relationship("Invoice", backref="refunds")


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
