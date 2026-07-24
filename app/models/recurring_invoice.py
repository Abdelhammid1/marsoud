"""MARSOUD-RECURRING-INVOICE-01 (Abdelhamid 2026-07-24).

Auto-generate invoices from a template on a schedule.
"""
import json
from datetime import datetime
from app import db


REC_INV_ACTION_EXECUTE = "EXECUTE"
REC_INV_ACTION_FAIL = "FAIL"
REC_INV_ACTION_STOP = "STOP"
REC_INV_ACTION_RESUME = "RESUME"
REC_INV_ACTION_DELETE = "DELETE"

REC_INV_FREQ_DAILY = "DAILY"
REC_INV_FREQ_WEEKLY = "WEEKLY"
REC_INV_FREQ_MONTHLY = "MONTHLY"
REC_INV_FREQ_YEARLY = "YEARLY"
ALL_REC_INV_FREQS = (
    REC_INV_FREQ_DAILY, REC_INV_FREQ_WEEKLY,
    REC_INV_FREQ_MONTHLY, REC_INV_FREQ_YEARLY,
)
REC_INV_FREQ_LABELS_AR = {
    REC_INV_FREQ_DAILY: "يومياً",
    REC_INV_FREQ_WEEKLY: "أسبوعياً",
    REC_INV_FREQ_MONTHLY: "شهرياً",
    REC_INV_FREQ_YEARLY: "سنوياً",
}


class RecurringInvoice(db.Model):
    __tablename__ = "recurring_invoices"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"),
                            nullable=False)
    name = db.Column(db.String(200), nullable=False)
    # JSON list of {description, quantity, unit_price} items.
    items_json = db.Column(db.Text, nullable=False)
    tax_rate = db.Column(db.Numeric(5, 2), default=15.00,
                         nullable=False)
    frequency = db.Column(db.String(20), nullable=False,
                          default=REC_INV_FREQ_MONTHLY)
    next_run_date = db.Column(db.Date, nullable=False, index=True)
    end_date = db.Column(db.Date, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    is_deleted = db.Column(db.Boolean, nullable=False, default=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                              nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)

    company = db.relationship("Company")
    customer = db.relationship("Customer")
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    @property
    def items(self):
        try:
            v = json.loads(self.items_json or "[]")
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []

    def set_items(self, items):
        self.items_json = json.dumps(list(items or []))


class RecurringInvoiceLog(db.Model):
    __tablename__ = "recurring_invoice_logs"

    id = db.Column(db.Integer, primary_key=True)
    recurring_id = db.Column(db.Integer,
                              db.ForeignKey("recurring_invoices.id",
                                            ondelete="CASCADE"),
                              nullable=False, index=True)
    action = db.Column(db.String(20), nullable=False)
    period_posted = db.Column(db.Date, nullable=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"),
                           nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    reason = db.Column(db.Text, nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                              nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)

    schedule = db.relationship("RecurringInvoice",
                                backref=db.backref(
                                    "logs",
                                    cascade="all, delete-orphan",
                                    order_by="RecurringInvoiceLog.created_at"))
    invoice = db.relationship("Invoice")
