"""MARSOUD-ATTENDANCE-POLICY (2026-08-05) — which policy applies to whom.

One function matters here: `resolve_policy_for_employee`. Everything the
attendance tickets do later — deciding whether a check-in is late,
deciding whether a missing day is an absence — asks it first, so the
precedence rule is expressed once and never re-derived.

    employee override  →  department policy  →  company policy  →  None

None is a real answer, not a failure. A company that has never defined a
policy keeps behaving exactly as it does today: nothing is automatically
late, nothing is automatically absent, and HR carries on entering
exceptions by hand. That is what makes this whole batch safe to deploy
to existing tenants.
"""
from datetime import date, datetime, timedelta

from app import db
from app.models.attendance import AttendancePolicy, PolicyScope


class AttendanceError(Exception):
    """User-facing problem defining or resolving a policy."""


def resolve_policy_for_employee(employee_id, on_date=None):
    """The policy that governs this employee, most specific first.

    `on_date` is accepted because the callers in later tickets have it in
    hand and a future dated-policy feature would need it. It is not used
    yet — policies have no validity window — and saying so is better than
    a caller assuming a date filter exists.
    """
    from app.models import Employee

    emp = db.session.get(Employee, employee_id)
    if emp is None:
        return None

    # order_by is not decoration. create_policy refuses a duplicate at the
    # same scope, but a direct insert — a data fix, an import — can still
    # make two, and .first() with no ordering would then resolve to
    # whichever row the database felt like returning. Newest wins, stated
    # rather than left to chance.
    base = (AttendancePolicy.query
            .filter_by(company_id=emp.company_id, is_active=True)
            .order_by(AttendancePolicy.id.desc()))

    own = base.filter_by(scope=PolicyScope.EMPLOYEE,
                         employee_id=emp.id).first()
    if own:
        return own

    if emp.department_id:
        dept = base.filter_by(scope=PolicyScope.DEPARTMENT,
                              department_id=emp.department_id).first()
        if dept:
            return dept

    return base.filter_by(scope=PolicyScope.COMPANY).first()


# ─── CRUD, mirroring services/leave.py's leave-type helpers ─────────────
def policies_for_company(company_id):
    return (AttendancePolicy.query
            .filter_by(company_id=company_id)
            .order_by(AttendancePolicy.scope.asc(),
                      AttendancePolicy.id.asc()).all())


def _validate_target(company_id, scope, department_id, employee_id):
    """The department/employee must belong to THIS company.

    Found by auditing: the form only offers own-company targets, but
    nothing checked, so a hand-crafted POST created a policy in company A
    pointing at company B's employee — and the listing renders
    `p.employee.name`, which put B's employee name on A's screen. A
    cross-tenant leak from a screen that never shows a text field.

    Same rule as the account pickers in the operations centre: validate
    against what the form WOULD have offered, not merely against the id
    being a number.
    """
    from app.models import Department, Employee

    if scope == PolicyScope.DEPARTMENT:
        if not department_id:
            raise AttendanceError("اختر القسم الذي تنطبق عليه السياسة")
        dept = db.session.get(Department, department_id)
        if dept is None or dept.company_id != company_id:
            raise AttendanceError("القسم المختار غير صالح")

    if scope == PolicyScope.EMPLOYEE:
        if not employee_id:
            raise AttendanceError("اختر الموظف الذي تنطبق عليه السياسة")
        emp = db.session.get(Employee, employee_id)
        if emp is None or emp.company_id != company_id:
            raise AttendanceError("الموظف المختار غير صالح")


def _validate(scope, department_id, employee_id, policy_type,
              start_time, end_time, latest_checkin):
    from app.models.attendance import PolicyType

    if policy_type == PolicyType.FIXED:
        if not start_time or not end_time:
            raise AttendanceError("وقت البداية والنهاية مطلوبان")
        if start_time >= end_time:
            raise AttendanceError("وقت البداية يجب أن يسبق وقت النهاية")
    else:
        if not latest_checkin:
            raise AttendanceError(
                "أقصى وقت للحضور مطلوب في الدوام المرن — "
                "هو ما يحدد متى يُحتسب الحضور متأخرًا")


def create_policy(*, company_id, scope, policy_type, department_id=None,
                  employee_id=None, start_time=None, end_time=None,
                  work_days=None, earliest_checkin=None, latest_checkin=None,
                  required_hours_per_day=None, auto_absent_enabled=False,
                  created_by=None):
    """One policy per (scope, target). A second one for the same target
    would make resolution depend on insertion order."""
    scope = _as_scope(scope)
    policy_type = _as_type(policy_type)
    department_id = department_id if scope == PolicyScope.DEPARTMENT else None
    employee_id = employee_id if scope == PolicyScope.EMPLOYEE else None
    _validate_target(company_id, scope, department_id, employee_id)
    _validate(scope, department_id, employee_id, policy_type,
              start_time, end_time, latest_checkin)

    clash = AttendancePolicy.query.filter_by(
        company_id=company_id, scope=scope, department_id=department_id,
        employee_id=employee_id).first()
    if clash:
        raise AttendanceError("توجد سياسة بنفس النطاق بالفعل — عدّلها بدل إنشاء واحدة جديدة")

    p = AttendancePolicy(
        company_id=company_id, scope=scope, policy_type=policy_type,
        department_id=department_id, employee_id=employee_id,
        start_time=start_time, end_time=end_time,
        work_days=work_days, earliest_checkin=earliest_checkin,
        latest_checkin=latest_checkin,
        required_hours_per_day=required_hours_per_day,
        auto_absent_enabled=bool(auto_absent_enabled),
        created_by=created_by,
    )
    db.session.add(p)
    db.session.commit()
    return p


def update_policy(policy, **fields):
    scope = _as_scope(fields.get("scope", policy.scope))
    policy_type = _as_type(fields.get("policy_type", policy.policy_type))
    department_id = fields.get("department_id", policy.department_id)
    employee_id = fields.get("employee_id", policy.employee_id)
    department_id = department_id if scope == PolicyScope.DEPARTMENT else None
    employee_id = employee_id if scope == PolicyScope.EMPLOYEE else None

    start_time = fields.get("start_time", policy.start_time)
    end_time = fields.get("end_time", policy.end_time)
    latest_checkin = fields.get("latest_checkin", policy.latest_checkin)
    _validate_target(policy.company_id, scope, department_id, employee_id)
    _validate(scope, department_id, employee_id, policy_type,
              start_time, end_time, latest_checkin)

    clash = AttendancePolicy.query.filter(
        AttendancePolicy.id != policy.id,
        AttendancePolicy.company_id == policy.company_id,
        AttendancePolicy.scope == scope,
        AttendancePolicy.department_id == department_id,
        AttendancePolicy.employee_id == employee_id).first()
    if clash:
        raise AttendanceError("توجد سياسة بنفس النطاق بالفعل")

    policy.scope = scope
    policy.policy_type = policy_type
    policy.department_id = department_id
    policy.employee_id = employee_id
    policy.start_time = start_time
    policy.end_time = end_time
    policy.work_days = fields.get("work_days", policy.work_days)
    policy.earliest_checkin = fields.get("earliest_checkin",
                                         policy.earliest_checkin)
    policy.latest_checkin = latest_checkin
    policy.required_hours_per_day = fields.get(
        "required_hours_per_day", policy.required_hours_per_day)
    if "is_active" in fields:
        policy.is_active = bool(fields["is_active"])
    if "auto_absent_enabled" in fields:
        policy.auto_absent_enabled = bool(fields["auto_absent_enabled"])
    db.session.commit()
    return policy


def delete_policy(policy):
    db.session.delete(policy)
    db.session.commit()


def _as_scope(value):
    if isinstance(value, PolicyScope):
        return value
    try:
        return PolicyScope(str(value))
    except ValueError:
        raise AttendanceError("نطاق السياسة غير صالح")


def _as_type(value):
    from app.models.attendance import PolicyType
    if isinstance(value, PolicyType):
        return value
    try:
        return PolicyType(str(value))
    except ValueError:
        raise AttendanceError("نوع الدوام غير صالح")


# ═══ MARSOUD-ATTENDANCE-CHECKIN (ticket 2) ══════════════════════════════
def _today_row(employee, on_date):
    from app.models import AttendanceCheckin
    return AttendanceCheckin.query.filter_by(
        employee_id=employee.id, date=on_date).first()


def check_in(employee, *, lat=None, lng=None, now=None):
    """Record an arrival. One per employee per day.

    Returns (row, exception_or_None) — the second element is whatever
    ticket 4's evaluation produced, so the caller can tell the employee
    they were marked late instead of leaving them to find out on the
    payslip.
    """
    from app.models import AttendanceCheckin

    now = now or datetime.now()
    on_date = now.date()
    existing = _today_row(employee, on_date)
    if existing is not None and existing.check_in_time is not None:
        raise AttendanceError(
            "سجّلت حضورك اليوم بالفعل الساعة "
            + existing.check_in_time.strftime("%H:%M"))

    row = existing or AttendanceCheckin(
        company_id=employee.company_id, employee_id=employee.id,
        date=on_date)
    row.check_in_time = now
    row.check_in_lat = lat
    row.check_in_lng = lng
    db.session.add(row)
    db.session.commit()

    return row, evaluate_checkin(row)


def check_out(employee, *, lat=None, lng=None, now=None):
    """Record a departure against today's check-in."""
    now = now or datetime.now()
    row = _today_row(employee, now.date())
    if row is None or row.check_in_time is None:
        raise AttendanceError("لم تسجّل حضورك اليوم بعد")
    if row.check_out_time is not None:
        raise AttendanceError(
            "سجّلت انصرافك اليوم بالفعل الساعة "
            + row.check_out_time.strftime("%H:%M"))
    if now < row.check_in_time:
        raise AttendanceError("وقت الانصراف قبل وقت الحضور")

    row.check_out_time = now
    row.check_out_lat = lat
    row.check_out_lng = lng
    db.session.commit()
    return row


def checkin_for(employee_id, on_date):
    from app.models import AttendanceCheckin
    return AttendanceCheckin.query.filter_by(
        employee_id=employee_id, date=on_date).first()


# ═══ MARSOUD-ATTENDANCE-AUTO (ticket 4) ═════════════════════════════════
def evaluate_checkin(checkin):
    """Turn a late arrival into an AttendanceException. Returns it, or None.

    THE EXCEPTION RECORDS THE RAW FACT. An employee 30 minutes late gets
    an exception of 30 minutes, even where a policy allows 20 minutes
    free — every allowance is applied later, when the deduction is
    computed (ticket 6). Netting the grace off here would make the
    attendance record disagree with what actually happened, and would
    then be subtracted a second time at payroll.

    Routed through the EXISTING create_exception() rather than inserting
    directly, so its duplicate-per-day validation keeps working — the
    ticket is explicit about this, and it is what stops a manual entry
    and an automatic one colliding.

    No policy → None. That is the whole backward-compatibility story: a
    company that has defined nothing keeps recording nothing
    automatically.
    """
    from app.models import AttendanceExceptionType
    from app.services.leave import create_exception, LeaveError

    policy = resolve_policy_for_employee(checkin.employee_id, checkin.date)
    if policy is None:
        return None
    if not policy.is_working_day(checkin.date):
        return None

    expected = policy.expected_arrival
    if expected is None or checkin.check_in_time is None:
        return None

    arrived = checkin.check_in_time
    threshold = datetime.combine(checkin.date, expected)
    if arrived <= threshold:
        return None

    minutes_late = (arrived - threshold).total_seconds() / 60.0
    if minutes_late < 1:
        return None

    try:
        return create_exception(
            company_id=checkin.company_id,
            employee_id=checkin.employee_id,
            date_=checkin.date,
            type_=AttendanceExceptionType.LATE,
            duration_hours=round(minutes_late / 60.0, 2),
            note=("تلقائي: حضور " + arrived.strftime("%H:%M")
                  + " بدل " + expected.strftime("%H:%M")),
        )
    except LeaveError:
        # A day already carrying an exception — a manual entry, or an
        # approved leave — wins. The employee is not marked late for a
        # day HR has already ruled on.
        return None


def mark_absent_for_date(company_id, on_date, actor_id=None):
    """Create ABSENT for anyone who never checked in on a working day.

    Runs from /cron/tick after the day is over. Idempotent twice over:
    employees who already have an exception are skipped before we get
    there, and create_exception refuses a second one anyway.
    """
    from app.models import (Employee, EmployeeStatus, AttendanceCheckin,
                            AttendanceException, AttendanceExceptionType)
    from app.services.leave import create_exception, LeaveError

    created, skipped = 0, {}

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    employees = Employee.query.filter_by(
        company_id=company_id, status=EmployeeStatus.ACTIVE).all()
    if not employees:
        return {"created": 0, "skipped": {"no active employees": 1}}

    checked_in = {
        r.employee_id for r in AttendanceCheckin.query.filter_by(
            company_id=company_id, date=on_date).all()
        if r.check_in_time is not None}
    # MARSOUD-EXCEPTION-AUDIT — a RAW query on purpose: cancelled rows
    # count here. The table's UNIQUE(employee_id, date) counts them too,
    # so a day whose exception was cancelled cannot take a new one, and
    # skipping the employee is the honest answer rather than attempting
    # an insert the database will refuse.
    already = {
        e.employee_id for e in AttendanceException.query.filter_by(
            company_id=company_id, date=on_date).all()}

    for emp in employees:
        policy = resolve_policy_for_employee(emp.id, on_date)
        if policy is None:
            skip("no policy")
            continue
        if not policy.auto_absent_enabled:
            # Defining working hours must not, on its own, start deducting
            # a day's pay from everyone who has not adopted check-in yet.
            skip("auto-absence not enabled on the policy")
            continue
        if not policy.is_working_day(on_date):
            skip("not a working day")
            continue
        if emp.start_date and on_date < emp.start_date:
            skip("before the employee started")
            continue
        if emp.id in checked_in:
            skip("attended")
            continue
        if emp.id in already:
            skip("already has an exception")
            continue
        try:
            create_exception(
                company_id=company_id, employee_id=emp.id, date_=on_date,
                type_=AttendanceExceptionType.ABSENT,
                note="تلقائي: لا يوجد تسجيل حضور",
                created_by=actor_id)
            created += 1
        except LeaveError:
            skip("refused by create_exception")

    return {"created": created, "skipped": skipped}


def sweep_absences(now=None, company_id=None):
    """The /cron/tick job. Looks at YESTERDAY, never today.

    A day is only judged once it is over — running against today would
    mark absent everyone who simply has not arrived yet.
    """
    from app.models import Company

    now = now or date.today()
    target = now - timedelta(days=1)
    summary = {"date": target.isoformat(), "created": 0, "companies": 0,
               "skipped": {}}

    companies = ([db.session.get(Company, company_id)] if company_id
                 else Company.query.filter_by(is_active=True).all())
    for co in [c for c in companies if c is not None]:
        res = mark_absent_for_date(co.id, target)
        summary["created"] += res["created"]
        if res["created"]:
            summary["companies"] += 1
        for reason, n in res["skipped"].items():
            summary["skipped"][reason] = summary["skipped"].get(reason, 0) + n
    return summary
