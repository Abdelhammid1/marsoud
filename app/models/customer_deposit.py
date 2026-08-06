"""MARSOUD-CUSTOMER-DEPOSIT-01 (Abdelhamid 2026-07-24).

Advance payments (deposits) from customers before the final
invoice exists. Balanced JE posted at record time; consumed when
an invoice is issued or reversed on refund.
"""
from datetime import datetime
from app import db


DEPOSIT_ACTIVE = "ACTIVE"
DEPOSIT_APPLIED = "APPLIED"
DEPOSIT_REFUNDED = "REFUNDED"
ALL_DEPOSIT_STATUSES = (
    DEPOSIT_ACTIVE, DEPOSIT_APPLIED, DEPOSIT_REFUNDED,
)
DEPOSIT_STATUS_LABELS_AR = {
    DEPOSIT_ACTIVE: "متاح",
    DEPOSIT_APPLIED: "تم تطبيقه",
    DEPOSIT_REFUNDED: "مسترجع",
}


class CustomerDeposit(db.Model):
    __tablename__ = "customer_deposits"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    customer_id = db.Column(db.Integer,
                             db.ForeignKey("customers.id"),
                             nullable=False, index=True)
    doc_number = db.Column(db.String(30), nullable=False)
    amount = db.Column(db.Numeric(15, 2), nullable=False)
    payment_method_id = db.Column(db.Integer,
                                    db.ForeignKey("payment_methods.id"),
                                    nullable=False)
    date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), nullable=False,
                       default=DEPOSIT_ACTIVE, index=True)
    applied_invoice_id = db.Column(db.Integer,
                                     db.ForeignKey("invoices.id"),
                                     nullable=True)
    journal_entry_id = db.Column(db.Integer,
                                   db.ForeignKey("journal_entries.id"),
                                   nullable=True)
    refund_journal_entry_id = db.Column(
        db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                              nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)
    # MARSOUD-DEPOSIT-AUDIT-01 (2026-08-06) — the reception side
    # already has a trail (created_by_id above); the refund side used
    # to have none. Both nullable so pre-fix deposits stay NULL; every
    # future refund_deposit() stamps both fields.
    refunded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                               nullable=True)
    refunded_at = db.Column(db.DateTime, nullable=True)

    company = db.relationship("Company")
    customer = db.relationship("Customer",
                                backref=db.backref(
                                    "deposits", lazy="dynamic"))
    payment_method = db.relationship("PaymentMethod")
    applied_invoice = db.relationship(
        "Invoice", foreign_keys=[applied_invoice_id])
    journal_entry = db.relationship(
        "JournalEntry", foreign_keys=[journal_entry_id])
    refund_journal_entry = db.relationship(
        "JournalEntry", foreign_keys=[refund_journal_entry_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    refunded_by = db.relationship("User", foreign_keys=[refunded_by_id])

    @property
    def is_active(self):
        return self.status == DEPOSIT_ACTIVE
