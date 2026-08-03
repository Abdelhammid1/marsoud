"""MARSOUD-ADVANCES — employee advances (سلف الموظفين).

Before this, `PayrollLine.advance_deduction` was a number the accountant
retyped every month with nothing behind it: no record of the money
actually leaving the till, no running balance, and no way to notice when
a month was skipped.

Two tables:

  AdvanceRequest  — an employee asking for an advance from /my/.
                    Mirrors LeaveRequest exactly (PENDING → APPROVED /
                    REJECTED, reviewed_by / reviewed_at / review_note).

  EmployeeAdvance — the real balance. Created either from an approved
                    request or added directly by an accountant; both
                    paths land in advances.approve_advance(), so there
                    is one place that posts the disbursement journal.

Scope (deliberate): one ACTIVE advance per employee at a time. The
monthly installment is amount / months, and the last installment
absorbs the rounding remainder because the payroll deduction is
min(monthly_installment, remaining).
"""
import enum
from datetime import datetime

from app import db


class AdvanceRequestStatus(enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class AdvanceStatus(enum.Enum):
    ACTIVE = "ACTIVE"        # still being deducted
    SETTLED = "SETTLED"      # fully recovered — remaining hit zero
    CANCELLED = "CANCELLED"  # journal reversed, deductions stopped


class AdvanceSource(enum.Enum):
    REQUEST = "REQUEST"      # came from an approved employee request
    DIRECT = "DIRECT"        # accountant/owner added it straight away


class AdvanceRequest(db.Model):
    __tablename__ = "advance_requests"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"),
                            nullable=False, index=True)
    amount = db.Column(db.Numeric(15, 2), nullable=False)
    reason = db.Column(db.Text)
    status = db.Column(db.Enum(AdvanceRequestStatus),
                       default=AdvanceRequestStatus.PENDING,
                       nullable=False, index=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    review_note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    company = db.relationship("Company")
    employee = db.relationship(
        "Employee", backref=db.backref("advance_requests", lazy="dynamic"))
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    creator = db.relationship("User", foreign_keys=[created_by])


class EmployeeAdvance(db.Model):
    __tablename__ = "employee_advances"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"),
                            nullable=False, index=True)

    amount = db.Column(db.Numeric(15, 2), nullable=False)     # disbursed
    remaining = db.Column(db.Numeric(15, 2), nullable=False)  # still owed
    months = db.Column(db.Integer, nullable=False, default=1)
    monthly_installment = db.Column(db.Numeric(15, 2), nullable=False)
    disbursed_on = db.Column(db.Date, nullable=False)

    status = db.Column(db.Enum(AdvanceStatus), default=AdvanceStatus.ACTIVE,
                       nullable=False, index=True)
    source = db.Column(db.Enum(AdvanceSource), default=AdvanceSource.DIRECT,
                       nullable=False)

    request_id = db.Column(db.Integer, db.ForeignKey("advance_requests.id"))
    journal_entry_id = db.Column(db.Integer,
                                 db.ForeignKey("journal_entries.id"))
    reversal_entry_id = db.Column(db.Integer,
                                  db.ForeignKey("journal_entries.id"))

    note = db.Column(db.Text)
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    cancelled_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    cancelled_at = db.Column(db.DateTime)
    cancel_reason = db.Column(db.Text)
    settled_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company")
    employee = db.relationship(
        "Employee", backref=db.backref("advances", lazy="dynamic"))
    request = db.relationship("AdvanceRequest",
                              backref=db.backref("advance", uselist=False))
    entry = db.relationship("JournalEntry", foreign_keys=[journal_entry_id])
    reversal_entry = db.relationship("JournalEntry",
                                     foreign_keys=[reversal_entry_id])
    approver = db.relationship("User", foreign_keys=[approved_by])
    creator = db.relationship("User", foreign_keys=[created_by])
    canceller = db.relationship("User", foreign_keys=[cancelled_by])

    @property
    def is_active(self):
        return self.status == AdvanceStatus.ACTIVE

    @property
    def paid_amount(self):
        """How much has been recovered through payroll so far."""
        return float(self.amount or 0) - float(self.remaining or 0)

    @property
    def next_installment(self):
        """What the next payroll run will deduct. 0 once closed."""
        if self.status != AdvanceStatus.ACTIVE:
            return 0.0
        return round(min(float(self.monthly_installment or 0),
                         float(self.remaining or 0)), 2)
