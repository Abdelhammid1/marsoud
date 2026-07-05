"""HR Phase 2 models — leave types, balances, requests, and attendance exceptions.

Design principle (from MARSOUD-25 spec):
    "Every day of the month = attendance, unless explicitly logged as an exception."

No daily attendance roster. The only data that flows into payroll is the set
of AttendanceException rows for the period.
"""
import enum
from datetime import datetime
from app import db


# ─── HR-05: leave types + per-employee balances ─────────────────────────
class LeaveType(db.Model):
    """A company-defined leave type (annual / sick / unpaid / etc).

    accrual_per_month: how many days are added to each employee's balance
        each month (e.g. 1.75 for KSA annual = 21/12).
    max_balance: cap on the running balance — accrual stops adding past this.
    is_paid: paid leave produces APPROVED_LEAVE exceptions; unpaid produces
        UNPAID_LEAVE exceptions in HR-06 approval flow.
    """
    __tablename__ = "leave_types"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    accrual_per_month = db.Column(db.Numeric(6, 3), default=0, nullable=False)
    max_balance = db.Column(db.Numeric(6, 2), default=0, nullable=False)
    is_paid = db.Column(db.Boolean, default=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company", backref=db.backref("leave_types", lazy="dynamic"))

    __table_args__ = (
        db.UniqueConstraint("company_id", "name", name="uq_leave_type_company_name"),
    )

    def __repr__(self):
        return f"<LeaveType {self.name} (co={self.company_id})>"


class LeaveBalance(db.Model):
    __tablename__ = "leave_balances"
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"),
                            nullable=False, index=True)
    leave_type_id = db.Column(db.Integer, db.ForeignKey("leave_types.id"),
                              nullable=False, index=True)
    year = db.Column(db.Integer, nullable=False)
    balance_days = db.Column(db.Numeric(7, 2), default=0, nullable=False)
    used_days = db.Column(db.Numeric(7, 2), default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    employee = db.relationship("Employee", backref=db.backref("leave_balances", lazy="dynamic"))
    leave_type = db.relationship("LeaveType", backref=db.backref("balances", lazy="dynamic"))

    __table_args__ = (
        db.UniqueConstraint("employee_id", "leave_type_id", "year",
                            name="uq_leave_balance_employee_type_year"),
    )

    @property
    def remaining_days(self):
        return float(self.balance_days or 0) - float(self.used_days or 0)


# ─── HR-05b: attendance exceptions ──────────────────────────────────────
class AttendanceExceptionType(enum.Enum):
    ABSENT = "ABSENT"                   # full-day deduction (unpaid)
    LATE = "LATE"                       # partial deduction by duration_hours / 8
    UNPAID_LEAVE = "UNPAID_LEAVE"       # full-day deduction (unpaid leave from HR-06)
    APPROVED_LEAVE = "APPROVED_LEAVE"   # paid leave from HR-06 — no deduction


class AttendanceException(db.Model):
    __tablename__ = "attendance_exceptions"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"),
                            nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    type = db.Column(db.Enum(AttendanceExceptionType), nullable=False)
    duration_hours = db.Column(db.Numeric(4, 2))      # only meaningful for LATE
    note = db.Column(db.Text)
    leave_request_id = db.Column(db.Integer, db.ForeignKey("leave_requests.id"), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company")
    employee = db.relationship("Employee", backref=db.backref("attendance_exceptions", lazy="dynamic"))
    leave_request = db.relationship("LeaveRequest", backref=db.backref("exceptions", lazy="dynamic"))
    actor = db.relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        db.UniqueConstraint("employee_id", "date", name="uq_exception_employee_date"),
    )

    def deduction_days(self):
        """How many days this single exception deducts from billable_days.

        ABSENT / UNPAID_LEAVE → 1.0
        LATE → duration_hours / 8 (clamped to [0, 1])
        APPROVED_LEAVE → 0 (paid)
        """
        t = self.type
        if t == AttendanceExceptionType.APPROVED_LEAVE:
            return 0.0
        if t == AttendanceExceptionType.LATE:
            hours = float(self.duration_hours or 0)
            return max(0.0, min(1.0, hours / 8.0))
        # ABSENT / UNPAID_LEAVE
        return 1.0


# ─── HR-06: leave requests ──────────────────────────────────────────────
class LeaveRequestStatus(enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class LeaveRequest(db.Model):
    __tablename__ = "leave_requests"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"),
                            nullable=False, index=True)
    leave_type_id = db.Column(db.Integer, db.ForeignKey("leave_types.id"),
                              nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    days_count = db.Column(db.Numeric(6, 2), default=0, nullable=False)
    reason = db.Column(db.Text)
    status = db.Column(db.Enum(LeaveRequestStatus),
                       default=LeaveRequestStatus.PENDING, nullable=False, index=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    review_note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    company = db.relationship("Company")
    employee = db.relationship("Employee", backref=db.backref("leave_requests", lazy="dynamic"))
    leave_type = db.relationship("LeaveType")
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    creator = db.relationship("User", foreign_keys=[created_by])
