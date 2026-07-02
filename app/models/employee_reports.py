"""MARSOUD-EMPLOYEE-DAILY-REPORTS — end-of-day activity digest that the
employee reviews and submits, then owner/admins consume.

Two tables:
  employee_daily_reports   — one row per employee per day. Starts life
                              as DRAFT (created by the cron), turns
                              SUBMITTED when the employee submits.
  employee_report_access   — per-viewer allow-list. Owners see every
                              report by default (bypass this table).
                              Any other admin/manager sees only the
                              employees explicitly listed here.
"""
import enum
from datetime import datetime, date
from app import db


class DailyReportStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"


class EmployeeDailyReport(db.Model):
    __tablename__ = "employee_daily_reports"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                             nullable=False, index=True)
    employee_id = db.Column(db.Integer,
                              db.ForeignKey("employees.id"),
                              nullable=False, index=True)
    report_date = db.Column(db.Date, nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    # Auto-generated summary from the 4 source tables.
    body = db.Column(db.Text, nullable=False, default="")
    # Employee's optional freeform additions.
    employee_notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.Enum(DailyReportStatus), nullable=False,
                          default=DailyReportStatus.DRAFT, index=True)
    submitted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                             nullable=False)

    employee = db.relationship("Employee",
                                  backref=db.backref("daily_reports",
                                                     lazy="dynamic"))
    company = db.relationship("Company")

    __table_args__ = (
        db.UniqueConstraint(
            "company_id", "employee_id", "report_date",
            name="uq_employee_daily_report_day",
        ),
    )


class EmployeeReportAccess(db.Model):
    """One row = "viewer_user can see reports for employee". Owners
    aren't in this table — they see everyone by default."""
    __tablename__ = "employee_report_access"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                             nullable=False, index=True)
    viewer_user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                                  nullable=False, index=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"),
                                  nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    viewer = db.relationship("User", foreign_keys=[viewer_user_id])
    employee = db.relationship("Employee", foreign_keys=[employee_id])
    company = db.relationship("Company")

    __table_args__ = (
        db.UniqueConstraint(
            "company_id", "viewer_user_id", "employee_id",
            name="uq_employee_report_access",
        ),
    )
