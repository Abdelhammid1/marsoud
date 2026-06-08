import enum
from datetime import datetime, date
from app import db


class ContractType(enum.Enum):
    FULL_TIME = "FULL_TIME"
    PART_TIME = "PART_TIME"
    TEMPORARY = "TEMPORARY"


class EmployeeStatus(enum.Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"


class TerminationReason(enum.Enum):
    RESIGNATION = "RESIGNATION"
    DISMISSAL = "DISMISSAL"
    CONTRACT_END = "CONTRACT_END"
    OTHER = "OTHER"


class Gender(enum.Enum):
    MALE = "MALE"
    FEMALE = "FEMALE"


class Employee(db.Model):
    __tablename__ = "employees"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    employee_number = db.Column(db.String(20), index=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150))
    phone = db.Column(db.String(30))
    job_title = db.Column(db.String(100))
    start_date = db.Column(db.Date)
    contract_type = db.Column(db.Enum(ContractType), default=ContractType.FULL_TIME)
    status = db.Column(db.Enum(EmployeeStatus), default=EmployeeStatus.ACTIVE, index=True)

    basic_salary = db.Column(db.Numeric(15, 2), default=0)
    allowances = db.Column(db.Numeric(15, 2), default=0)
    deductions = db.Column(db.Numeric(15, 2), default=0)

    termination_date = db.Column(db.Date)
    termination_reason = db.Column(db.Enum(TerminationReason))
    termination_notes = db.Column(db.Text)

    # HR-01
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True, index=True)
    # HR-02 — personal + contract fields (all nullable, additive)
    national_id = db.Column(db.String(50))
    nationality = db.Column(db.String(60))
    date_of_birth = db.Column(db.Date)
    gender = db.Column(db.Enum(Gender))
    manager_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True)
    contract_end_date = db.Column(db.Date, index=True)
    contract_alert_last_sent = db.Column(db.Date)  # last day we emailed a contract-expiry alert
    notes = db.Column(db.Text)

    is_active = db.Column(db.Boolean, default=True)  # legacy mirror — kept in sync with status
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company", backref=db.backref("employees", lazy="dynamic"))
    department = db.relationship("Department", foreign_keys=[department_id],
                                 backref=db.backref("members", lazy="dynamic"))
    manager = db.relationship("Employee", remote_side=[id], foreign_keys=[manager_id])

    @property
    def total_received(self):
        """Sum of all net payments across past payroll runs."""
        return sum(float(l.net or 0) for l in self.payroll_lines)

    @property
    def last_payslip(self):
        return PayrollLine.query.filter_by(employee_id=self.id).join(PayrollRun).order_by(
            PayrollRun.period_year.desc(), PayrollRun.period_month.desc()
        ).first()


class PayrollRun(db.Model):
    __tablename__ = "payroll_runs"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    number = db.Column(db.String(20), index=True)
    period_year = db.Column(db.Integer, nullable=False)
    period_month = db.Column(db.Integer, nullable=False)
    total_gross = db.Column(db.Numeric(15, 2), default=0)
    total_net = db.Column(db.Numeric(15, 2), default=0)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    lines = db.relationship("PayrollLine", backref="run", cascade="all, delete-orphan")
    company = db.relationship("Company")

    __table_args__ = (
        db.UniqueConstraint("company_id", "period_year", "period_month", name="uq_payroll_period"),
    )


class PayrollLine(db.Model):
    """One employee's line in a monthly payroll run.

    All variable fields (overtime, bonus, absence/late/advance deductions) live
    here, not on Employee — they reset each month because each run gets a fresh
    set of lines.
    """
    __tablename__ = "payroll_lines"
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey("payroll_runs.id"), nullable=False)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False)

    working_days = db.Column(db.Integer, default=30)
    basic = db.Column(db.Numeric(15, 2), default=0)        # prorated (basic / 30 × working_days)
    allowances = db.Column(db.Numeric(15, 2), default=0)
    overtime = db.Column(db.Numeric(15, 2), default=0)
    bonus = db.Column(db.Numeric(15, 2), default=0)
    deductions = db.Column(db.Numeric(15, 2), default=0)   # fixed deductions from employee
    absence_deduction = db.Column(db.Numeric(15, 2), default=0)
    late_deduction = db.Column(db.Numeric(15, 2), default=0)
    advance_deduction = db.Column(db.Numeric(15, 2), default=0)

    net = db.Column(db.Numeric(15, 2), default=0)
    amount_paid = db.Column(db.Numeric(15, 2), default=0)  # what was actually paid; (net - amount_paid) → accrual

    # HR-07 — true when absence/late were auto-filled from AttendanceExceptions
    # rather than manually entered. Flipped back to False the moment a user
    # overrides the value in the payroll form.
    attendance_auto_calculated = db.Column(db.Boolean, default=False, nullable=False)

    employee = db.relationship("Employee", backref="payroll_lines")


class EmployeeAccrual(db.Model):
    """Per-employee unpaid salary balance.

    Created when a payroll line's amount_paid < net. Tracks who is owed what so
    the company can settle later. The aggregate balance ties to a portion of
    2130 (Salaries Payable) in the GL.
    """
    __tablename__ = "employee_accruals"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=False, index=True)
    source_run_id = db.Column(db.Integer, db.ForeignKey("payroll_runs.id"))
    source_line_id = db.Column(db.Integer, db.ForeignKey("payroll_lines.id"))
    amount = db.Column(db.Numeric(15, 2), nullable=False)
    settled_at = db.Column(db.DateTime)
    settlement_journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    employee = db.relationship("Employee", backref=db.backref("accruals", lazy="dynamic"))
    company = db.relationship("Company")
    source_run = db.relationship("PayrollRun")
    source_line = db.relationship("PayrollLine", backref=db.backref("accrual", uselist=False))
    settlement_entry = db.relationship("JournalEntry")

    @property
    def is_settled(self):
        return self.settled_at is not None
