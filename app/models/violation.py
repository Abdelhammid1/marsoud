"""MARSOUD-VIOLATION-POLICY (2026-08-05) — the rules that turn attendance
exceptions into money.

Ticket 1 wrote down what the working day IS. This ticket writes down what
happens when someone breaks it: how much of a day an absence costs, how
many minutes of lateness the company forgives, how often an employee may
ask permission for a late arrival, and the top of a permission window.

SCOPE follows ticket 1's shape verbatim — the same PolicyScope enum, the
same COMPANY / DEPARTMENT / EMPLOYEE precedence, the same "None is a real
answer" story. A company with no violation policy resolves to None and
the whole system keeps its pre-batch behaviour: absence is a flat 1.0
day, lateness is charged in full, permissions do not exist.

DEFAULTS ARE THE "NO ALLOWANCE" VALUES. Absence deductions default to 1.0
for both branches; free-late minutes and permission counts default to 0.
So a fresh policy row inserted by HR before they have thought about the
numbers behaves identically to no policy — the byte-for-byte regression
guarantee holds not only when nothing is defined, but also when someone
has clicked "create" and left every field on its default.
"""
import enum
from datetime import datetime
from decimal import Decimal

from app import db
from app.models.attendance import PolicyScope   # reuse; do NOT clone


class AttendanceViolationPolicy(db.Model):
    __tablename__ = "attendance_violation_policies"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                           db.ForeignKey("companies.id", ondelete="CASCADE"),
                           nullable=False, index=True)

    scope = db.Column(db.Enum(PolicyScope, name="policyscope"),
                      nullable=False, default=PolicyScope.COMPANY, index=True)
    department_id = db.Column(db.Integer,
                              db.ForeignKey("departments.id",
                                            ondelete="CASCADE"),
                              nullable=True, index=True)
    employee_id = db.Column(db.Integer,
                            db.ForeignKey("employees.id",
                                          ondelete="CASCADE"),
                            nullable=True, index=True)

    # Absence — split on the exception's is_excused flag. Defaults are the
    # pre-batch rate (1.0 day for either kind), so a fresh policy with
    # untouched fields costs exactly what today costs.
    absence_unexcused_deduction_days = db.Column(
        db.Numeric(4, 2), nullable=False, default=Decimal("1.00"))
    absence_excused_deduction_days = db.Column(
        db.Numeric(4, 2), nullable=False, default=Decimal("1.00"))

    # Lateness — a monthly pool of forgiven minutes plus an optional
    # per-day cap on how many count toward the pool. Both default to 0 so
    # no allowance exists unless HR sets one.
    monthly_free_late_minutes = db.Column(
        db.Integer, nullable=False, default=0)
    daily_free_late_minutes_cap = db.Column(
        db.Integer, nullable=False, default=0)

    # Permission requests — how many an employee may file per calendar
    # month, and the longest single one they may ask for. Zero means the
    # feature is off for whoever this policy resolves to.
    permission_count_per_month = db.Column(
        db.Integer, nullable=False, default=0)
    permission_max_hours = db.Column(
        db.Numeric(4, 2), nullable=False, default=Decimal("0.00"))

    is_active = db.Column(db.Boolean, nullable=False, default=True,
                          index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    department = db.relationship("Department")
    employee = db.relationship("Employee", foreign_keys=[employee_id])

    def __repr__(self):                                  # pragma: no cover
        target = (f"dept={self.department_id}" if self.department_id
                  else f"emp={self.employee_id}" if self.employee_id
                  else f"co={self.company_id}")
        return f"<AttendanceViolationPolicy {self.scope.value} {target}>"


class PermissionStatus(enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class LatePermissionRequest(db.Model):
    """A permission to arrive late (or leave early) on a single day.

    Mirrors LeaveRequest field-for-field, then narrows to one day + a
    duration in hours. When approved, the day's LATE exception is
    forgiven up to `hours_count` in compute_late_deduction — BEFORE the
    daily cap and the monthly pool apply, per the spec.

    The workflow is deliberately identical to LeaveRequest (PENDING →
    APPROVED / REJECTED / CANCELLED, reviewer + timestamp + note) so HR
    do not learn a second vocabulary and can build muscle memory for
    both from the same screen shape.
    """
    __tablename__ = "late_permission_requests"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"),
                            nullable=False, index=True)

    # Single-day; date only. start_time / end_time are the window within
    # the day (informational — the deduction math uses hours_count).
    request_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time)
    end_time = db.Column(db.Time)
    hours_count = db.Column(db.Numeric(4, 2), default=0, nullable=False)

    reason = db.Column(db.Text)
    status = db.Column(db.Enum(PermissionStatus, name="permissionstatus"),
                       default=PermissionStatus.PENDING,
                       nullable=False, index=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    review_note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    company = db.relationship("Company")
    employee = db.relationship(
        "Employee",
        backref=db.backref("permission_requests", lazy="dynamic"))
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    creator = db.relationship("User", foreign_keys=[created_by])

    def __repr__(self):                                  # pragma: no cover
        return (f"<LatePermissionRequest emp={self.employee_id} "
                f"{self.request_date} {float(self.hours_count)}h "
                f"{self.status.value}>")
