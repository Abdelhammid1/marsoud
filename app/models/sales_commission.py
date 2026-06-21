"""MARSOUD-COMM-01 — sales commission ledger row.

One row per (payment × sales_rep) commission. Status flips from UNPAID
to PAID when settled via a PayrollRun (Phase C). Phase B uses
is_carry_forward + negative amount for refund-after-payroll cases.

Each row carries its own snapshot of commission_rate so a later rate
change on the Customer doesn't mutate historical commissions.
"""
from datetime import datetime
from app import db


COMMISSION_STATUSES = ("UNPAID", "PAID")


class SalesCommission(db.Model):
    __tablename__ = "sales_commissions"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    sales_rep_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                             nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"),
                           nullable=False, index=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id"),
                           index=True)

    # Pre-tax portion of the payment that earns commission. Stored
    # alongside the computed amount so the math is auditable.
    taxable_base = db.Column(db.Numeric(15, 4), nullable=False, default=0)
    amount = db.Column(db.Numeric(15, 4), nullable=False, default=0)
    # Frozen at row insert. A later Customer.commission_rate change won't
    # rewrite history — Phase B/C reports use this value.
    commission_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)

    period_year = db.Column(db.Integer, nullable=False)
    period_month = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(15), nullable=False, default="UNPAID",
                        index=True)
    payroll_run_id = db.Column(db.Integer,
                                db.ForeignKey("payroll_runs.id"))
    # Phase B: True for negative refund-after-payroll rows that
    # consume the rep's next month's earnings.
    is_carry_forward = db.Column(db.Boolean, nullable=False, default=False)
    journal_entry_id = db.Column(db.Integer,
                                  db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    sales_rep = db.relationship("User", foreign_keys=[sales_rep_id])
    customer = db.relationship("Customer", foreign_keys=[customer_id])
    invoice = db.relationship("Invoice", foreign_keys=[invoice_id])
    payment = db.relationship("Payment", foreign_keys=[payment_id])
    payroll_run = db.relationship("PayrollRun", foreign_keys=[payroll_run_id])
