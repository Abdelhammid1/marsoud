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

    base = AttendancePolicy.query.filter_by(
        company_id=emp.company_id, is_active=True)

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


def _validate(scope, department_id, employee_id, policy_type,
              start_time, end_time, latest_checkin):
    from app.models.attendance import PolicyType

    if scope == PolicyScope.DEPARTMENT and not department_id:
        raise AttendanceError("اختر القسم الذي تنطبق عليه السياسة")
    if scope == PolicyScope.EMPLOYEE and not employee_id:
        raise AttendanceError("اختر الموظف الذي تنطبق عليه السياسة")

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
                  required_hours_per_day=None, created_by=None):
    """One policy per (scope, target). A second one for the same target
    would make resolution depend on insertion order."""
    scope = _as_scope(scope)
    policy_type = _as_type(policy_type)
    department_id = department_id if scope == PolicyScope.DEPARTMENT else None
    employee_id = employee_id if scope == PolicyScope.EMPLOYEE else None
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
