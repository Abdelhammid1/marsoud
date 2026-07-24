"""MARSOUD-INSTALLMENT-PLAN-01 (Abdelhamid 2026-07-24).

Split one invoice into scheduled payment installments — each row
is a mini "due date + amount" that flows through overdue tracking
and reminders independently.
"""
from datetime import datetime
from app import db


INSTALLMENT_PENDING = "PENDING"
INSTALLMENT_PAID = "PAID"
INSTALLMENT_OVERDUE = "OVERDUE"
ALL_INSTALLMENT_STATUSES = (
    INSTALLMENT_PENDING, INSTALLMENT_PAID, INSTALLMENT_OVERDUE,
)
INSTALLMENT_STATUS_LABELS_AR = {
    INSTALLMENT_PENDING: "معلّق",
    INSTALLMENT_PAID:    "مدفوع",
    INSTALLMENT_OVERDUE: "متأخر",
}


class InvoiceInstallment(db.Model):
    __tablename__ = "invoice_installments"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer,
                            db.ForeignKey("invoices.id",
                                          ondelete="CASCADE"),
                            nullable=False, index=True)
    sequence_no = db.Column(db.Integer, nullable=False)
    amount = db.Column(db.Numeric(15, 2), nullable=False)
    due_date = db.Column(db.Date, nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False,
                       default=INSTALLMENT_PENDING, index=True)
    paid_payment_id = db.Column(db.Integer,
                                 db.ForeignKey("payments.id"),
                                 nullable=True)
    paid_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)

    __table_args__ = (
        db.UniqueConstraint("invoice_id", "sequence_no",
                             name="ux_installment_seq"),
    )

    invoice = db.relationship(
        "Invoice", backref=db.backref(
            "installments",
            cascade="all, delete-orphan",
            order_by="InvoiceInstallment.sequence_no"))
    payment = db.relationship(
        "Payment", foreign_keys=[paid_payment_id])


class InstallmentReminderSent(db.Model):
    __tablename__ = "installment_reminder_sent"

    id = db.Column(db.Integer, primary_key=True)
    installment_id = db.Column(db.Integer,
                                db.ForeignKey("invoice_installments.id",
                                              ondelete="CASCADE"),
                                nullable=False, index=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    threshold_kind = db.Column(db.String(20), nullable=False)
    threshold_days = db.Column(db.Integer, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow,
                        nullable=False)

    __table_args__ = (
        db.UniqueConstraint("installment_id", "threshold_kind",
                             "threshold_days",
                             name="ux_installment_reminder"),
    )

    installment = db.relationship("InvoiceInstallment")
