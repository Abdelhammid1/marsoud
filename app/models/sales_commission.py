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
    # MARSOUD-COMM-SETTLE (2026-08-25) — cumulative amount settled so far,
    # mirroring EmployeeAccrual.paid_amount (models/payroll.py). `status`
    # alone could only say UNPAID/PAID, which made partial settlement
    # impossible: a commission paid half in payroll and half in cash had
    # nowhere to record the half. Individual settlements are audited via
    # the journals themselves (source_type='payroll' for a run,
    # 'commission_settle' for a manual payment).
    settled_amount = db.Column(db.Numeric(15, 4), nullable=False,
                                default=0, server_default="0")
    settled_at = db.Column(db.DateTime)
    payroll_run_id = db.Column(db.Integer,
                                db.ForeignKey("payroll_runs.id"))
    # Phase B: True for negative refund-after-payroll rows that
    # consume the rep's next month's earnings.
    is_carry_forward = db.Column(db.Boolean, nullable=False, default=False)
    journal_entry_id = db.Column(db.Integer,
                                  db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # MARSOUD-COMM-DASHBOARD (2026-08-31) — cancellation trail.
    # A commission may be voided BEFORE payment (UNPAID → reversal JE +
    # marked voided). void_reason is mandatory at the service layer so
    # nobody can wipe an accrual without saying why. Payment side is
    # untouched: once mark_settled has run (status='PAID'), the row is
    # frozen — the reversal path is a NEW compensating JE, not an
    # in-place edit.
    voided_at = db.Column(db.DateTime, nullable=True)
    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                              nullable=True)
    void_reason = db.Column(db.Text, nullable=True)

    sales_rep = db.relationship("User", foreign_keys=[sales_rep_id])
    customer = db.relationship("Customer", foreign_keys=[customer_id])
    invoice = db.relationship("Invoice", foreign_keys=[invoice_id])
    payment = db.relationship("Payment", foreign_keys=[payment_id])
    payroll_run = db.relationship("PayrollRun", foreign_keys=[payroll_run_id])

    # ── MARSOUD-COMM-SETTLE (2026-08-25) ──────────────────────────────
    # Same three accessors EmployeeAccrual exposes, so callers that
    # already know that shape (settle_accrual, the accruals screens)
    # read identically here.

    @property
    def is_settled(self):
        return self.settled_at is not None

    @property
    def remaining(self):
        """Amount still owed to the rep on this row.

        Carry-forward rows carry a NEGATIVE amount (a clawback that eats
        into next month's earnings). `remaining` is signed the same way,
        so summing it across a rep's rows gives the true net owed — which
        is exactly what has to tie back to the 2150 balance.
        """
        return round(float(self.amount or 0)
                     - float(self.settled_amount or 0), 2)

    def mark_settled(self, amount, *, when=None):
        """Add `amount` to what has been settled, stamping settled_at and
        flipping `status` once nothing is left.

        `status` is kept in sync rather than replaced — reports, the
        Phase C query and existing audits all still filter on it.
        """
        from decimal import Decimal
        self.settled_amount = Decimal(str(
            round(float(self.settled_amount or 0) + float(amount), 4)))
        if abs(self.remaining) <= 0.005:
            self.settled_at = when or datetime.utcnow()
            self.status = "PAID"
        else:
            self.settled_at = None
            self.status = "UNPAID"
