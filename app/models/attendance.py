"""MARSOUD-ATTENDANCE-POLICY (2026-08-05) — what the working day is.

The HR module could record that someone was absent or late, but nothing
anywhere said what time they were supposed to arrive. Every exception was
typed in retroactively against a rule that lived in somebody's head.

This is that rule, written down. Nothing consumes it yet — ticket 4 is
what compares a real check-in against it. Until then the table exists and
changes nothing, which is deliberate: a company with no policy resolves
to None and the whole system keeps behaving exactly as it does today.

SCOPE follows the shape already used for Department: a policy can belong
to the company, to one department, or to one employee, and the most
specific one wins. `resolve_policy_for_employee` in
app/services/attendance.py is the only place that ordering is expressed.
"""
import enum
from datetime import datetime

from app import db


class PolicyScope(str, enum.Enum):
    COMPANY = "COMPANY"          # the default for everyone
    DEPARTMENT = "DEPARTMENT"    # overrides the company policy
    EMPLOYEE = "EMPLOYEE"        # overrides both


class PolicyType(str, enum.Enum):
    # Fixed hours: be here between start_time and end_time on work_days.
    FIXED = "FIXED"
    # Flexible: arrive any time in a window, work a required number of
    # hours. Lateness means arriving after latest_checkin, not after a
    # single start time.
    FLEXIBLE = "FLEXIBLE"


# Monday=0 … Sunday=6, matching date.weekday() so no translation is ever
# needed at comparison time. Stored as a comma-separated string because
# it is a handful of small integers read as a whole — a child table would
# be three joins for no benefit.
DEFAULT_WORK_DAYS = "6,0,1,2,3"      # Sun–Thu, the regional working week


class AttendancePolicy(db.Model):
    __tablename__ = "attendance_policies"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                           db.ForeignKey("companies.id", ondelete="CASCADE"),
                           nullable=False, index=True)

    scope = db.Column(db.Enum(PolicyScope), nullable=False,
                      default=PolicyScope.COMPANY, index=True)
    # Exactly one of these is set, and only for the matching scope.
    department_id = db.Column(db.Integer,
                              db.ForeignKey("departments.id",
                                            ondelete="CASCADE"),
                              nullable=True, index=True)
    employee_id = db.Column(db.Integer,
                            db.ForeignKey("employees.id", ondelete="CASCADE"),
                            nullable=True, index=True)

    policy_type = db.Column(db.Enum(PolicyType), nullable=False,
                            default=PolicyType.FIXED)

    # FIXED
    start_time = db.Column(db.Time)
    end_time = db.Column(db.Time)
    work_days = db.Column(db.String(20), default=DEFAULT_WORK_DAYS)

    # FLEXIBLE
    earliest_checkin = db.Column(db.Time)
    latest_checkin = db.Column(db.Time)
    required_hours_per_day = db.Column(db.Numeric(4, 2))

    is_active = db.Column(db.Boolean, nullable=False, default=True,
                          index=True)

    # MARSOUD-ATTENDANCE-AUTO — the absence sweep is OFF until switched on,
    # and this asymmetry is deliberate.
    #
    # Lateness is opt-in by behaviour: it can only fire for an employee
    # who actually checked in, so defining a policy costs nothing.
    # ABSENCE is the opposite — it fires for everyone who did NOT check
    # in, which on day one is the entire company. Measured: 3 employees,
    # a fresh policy, zero check-ins produced 3 absences on the first
    # sweep, a full day's pay each, and again every working day after.
    #
    # The realistic rollout is "write the hours down, then tell staff to
    # start checking in", and the sweep runs between those two steps. So
    # HR turns this on when attendance is actually being recorded.
    auto_absent_enabled = db.Column(db.Boolean, nullable=False,
                                    default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    department = db.relationship("Department")
    employee = db.relationship("Employee", foreign_keys=[employee_id])

    # ── helpers used by ticket 4, kept here so the rule and the data
    #    that expresses it stay together ────────────────────────────────
    @property
    def work_day_numbers(self):
        """The weekdays this policy expects attendance on, as date.weekday()
        numbers. An empty/blank column means every day, which is the safe
        reading: a policy that lists no working days should not silently
        mark everyone absent."""
        raw = (self.work_days or "").strip()
        if not raw:
            return set(range(7))
        out = set()
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit() and 0 <= int(part) <= 6:
                out.add(int(part))
        return out or set(range(7))

    def is_working_day(self, on_date):
        return on_date.weekday() in self.work_day_numbers

    @property
    def expected_arrival(self):
        """The time after which an arrival counts as late, or None when the
        policy cannot say. FLEXIBLE measures against the END of the
        arrival window — arriving at 10:00 inside a 08:00-10:30 window is
        not late."""
        if self.policy_type == PolicyType.FLEXIBLE:
            return self.latest_checkin
        return self.start_time

    def __repr__(self):                                  # pragma: no cover
        target = (f"dept={self.department_id}" if self.department_id
                  else f"emp={self.employee_id}" if self.employee_id
                  else f"co={self.company_id}")
        return f"<AttendancePolicy {self.scope.value} {target}>"
