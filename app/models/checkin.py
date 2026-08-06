"""MARSOUD-ATTENDANCE-CHECKIN (2026-08-05) — the employee's own record.

Until now the only attendance data in the system was the exception: HR
typing in, after the fact, that someone was absent or late. Nobody ever
recorded the ordinary case of turning up on time, so there was nothing to
measure an exception against.

This is the raw log and nothing more. Ticket 4 is what compares a row
here against the policy and decides whether it was late — deliberately
kept out of the model, so a check-in stays a fact rather than a verdict.

ONE ROW PER EMPLOYEE PER DAY, enforced by a unique constraint the same
way AttendanceException does it. Check-out updates the row rather than
creating a second one, so "arrived twice" is impossible by construction
rather than by a service remembering to look.

Coordinates are nullable on purpose: the ticket says a refused browser
permission must not block the check-in. Location is evidence when
offered, never a gate.

KNOWN LIMITATION — SHIFTS THAT CROSS MIDNIGHT. The row is keyed by
calendar date, and check-out looks for TODAY's row. An employee who
starts at 22:00 therefore cannot check out at 06:00 the next morning:
their check-in belongs to yesterday and the service answers "you have
not checked in today". Found by auditing rather than in use.

Not fixed here, and not hidden either: no ticket in this batch mentions
night shifts, and doing it properly means the POLICY has to say a shift
crosses midnight — otherwise a 06:00 check-out is indistinguishable from
someone arriving very early. That belongs with shift-aware policies, not
with a widened lookup window that would guess.
"""
from datetime import datetime

from app import db


class AttendanceCheckin(db.Model):
    __tablename__ = "attendance_checkins"
    __table_args__ = (
        db.UniqueConstraint("employee_id", "date",
                            name="uq_attendance_checkin_employee_date"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                           db.ForeignKey("companies.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    # Keyed to the Employee, not the User — the same choice
    # AttendanceException makes, and the reason is the same: a user may
    # hold an Employee row in several companies.
    employee_id = db.Column(db.Integer,
                            db.ForeignKey("employees.id", ondelete="CASCADE"),
                            nullable=False, index=True)

    date = db.Column(db.Date, nullable=False, index=True)
    check_in_time = db.Column(db.DateTime)
    check_out_time = db.Column(db.DateTime)

    check_in_lat = db.Column(db.Float)
    check_in_lng = db.Column(db.Float)
    check_out_lat = db.Column(db.Float)
    check_out_lng = db.Column(db.Float)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    employee = db.relationship("Employee")

    @property
    def is_open(self):
        """Checked in and not yet out."""
        return self.check_in_time is not None and self.check_out_time is None

    @property
    def worked_hours(self):
        """Hours between in and out, or None while the day is still open."""
        if not (self.check_in_time and self.check_out_time):
            return None
        delta = self.check_out_time - self.check_in_time
        return round(delta.total_seconds() / 3600.0, 2)

    def __repr__(self):                                  # pragma: no cover
        return (f"<AttendanceCheckin emp={self.employee_id} {self.date} "
                f"in={self.check_in_time} out={self.check_out_time}>")
