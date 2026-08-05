"""MARSOUD-VIOLATION-POLICY (2026-08-05) — which violation policy
applies to whom, and the permission-request workflow that lives with it.

Ticket 6 layers monetary rules on top of ticket 1's schedule rules.
Nothing here decides WHETHER someone was late; that is ticket 4. What
this file decides is HOW MUCH LATENESS COSTS — and the answer routes
through resolve_violation_policy_for_employee, which follows ticket 1's
precedence exactly:

    employee override  →  department policy  →  company policy  →  None

None means "no violation policy has been set for this company", and the
payroll layer must fall back to pre-batch behaviour byte-for-byte.
"""
from datetime import date, datetime
from decimal import Decimal

from app import db
from app.models import (
    AttendanceViolationPolicy, PolicyScope,
    LatePermissionRequest, PermissionStatus,
    Employee,
)


class ViolationError(Exception):
    """User-facing problem defining a policy or submitting a permission
    request. Same shape as AttendanceError / LeaveError."""


# ─── Resolver ───────────────────────────────────────────────────────────
def resolve_violation_policy_for_employee(employee_id, on_date=None):
    """The violation policy that governs this employee, most specific
    first. Identical shape to resolve_policy_for_employee — kept as a
    separate function only so the two policy tables never share a query.

    `on_date` is accepted for symmetry with ticket 1 and for a future
    dated-policy feature. It is not consulted yet — policies have no
    validity window — and it is better to say so than to have a caller
    assume a date filter exists.
    """
    emp = db.session.get(Employee, employee_id)
    if emp is None:
        return None

    # Newest wins if two rows exist at the same scope; create_policy
    # refuses a duplicate, but a data fix or import can still make two,
    # and .first() with no ordering would return whichever row the
    # database felt like handing back.
    base = (AttendanceViolationPolicy.query
            .filter_by(company_id=emp.company_id, is_active=True)
            .order_by(AttendanceViolationPolicy.id.desc()))

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


# ─── CRUD, mirroring services/attendance.py's policy helpers ────────────
def violation_policies_for_company(company_id):
    return (AttendanceViolationPolicy.query
            .filter_by(company_id=company_id)
            .order_by(AttendanceViolationPolicy.scope.asc(),
                      AttendanceViolationPolicy.id.asc()).all())


def _as_scope(value):
    if isinstance(value, PolicyScope):
        return value
    try:
        return PolicyScope(str(value))
    except ValueError:
        raise ViolationError("نطاق السياسة غير صالح")


def _validate_target(company_id, scope, department_id, employee_id):
    """The department/employee must belong to THIS company.

    Same reason as _validate_target in services/attendance.py: the form
    only offers own-company targets, but a hand-crafted POST could pick
    a foreign id. The listing page renders `p.employee.name`, so a
    missing check would put company B's name on company A's screen — a
    cross-tenant leak from a screen that never shows a text field.
    """
    from app.models import Department

    if scope == PolicyScope.DEPARTMENT:
        if not department_id:
            raise ViolationError("اختر القسم الذي تنطبق عليه السياسة")
        dept = db.session.get(Department, department_id)
        if dept is None or dept.company_id != company_id:
            raise ViolationError("القسم المختار غير صالح")

    if scope == PolicyScope.EMPLOYEE:
        if not employee_id:
            raise ViolationError("اختر الموظف الذي تنطبق عليه السياسة")
        emp = db.session.get(Employee, employee_id)
        if emp is None or emp.company_id != company_id:
            raise ViolationError("الموظف المختار غير صالح")


def _dec(v, default="0"):
    """Coerce a form value to Decimal, refusing negatives up front."""
    if v is None or v == "":
        v = default
    try:
        d = Decimal(str(v))
    except Exception:
        raise ViolationError("قيمة رقمية غير صالحة")
    if d < 0:
        raise ViolationError("القيم لا يمكن أن تكون سالبة")
    return d


def _int(v, default=0):
    if v is None or v == "":
        v = default
    try:
        i = int(v)
    except (TypeError, ValueError):
        raise ViolationError("قيمة رقمية غير صالحة")
    if i < 0:
        raise ViolationError("القيم لا يمكن أن تكون سالبة")
    return i


def create_violation_policy(*, company_id, scope,
                            department_id=None, employee_id=None,
                            absence_unexcused_deduction_days=None,
                            absence_excused_deduction_days=None,
                            monthly_free_late_minutes=None,
                            daily_free_late_minutes_cap=None,
                            permission_count_per_month=None,
                            permission_max_hours=None,
                            created_by=None):
    """One policy per (scope, target). A second one for the same target
    would make resolution depend on insertion order."""
    scope = _as_scope(scope)
    department_id = department_id if scope == PolicyScope.DEPARTMENT else None
    employee_id = employee_id if scope == PolicyScope.EMPLOYEE else None
    _validate_target(company_id, scope, department_id, employee_id)

    clash = AttendanceViolationPolicy.query.filter_by(
        company_id=company_id, scope=scope, department_id=department_id,
        employee_id=employee_id).first()
    if clash:
        raise ViolationError(
            "توجد سياسة انتهاكات بنفس النطاق بالفعل — عدّلها بدل إنشاء واحدة جديدة")

    p = AttendanceViolationPolicy(
        company_id=company_id, scope=scope,
        department_id=department_id, employee_id=employee_id,
        absence_unexcused_deduction_days=_dec(
            absence_unexcused_deduction_days, "1.00"),
        absence_excused_deduction_days=_dec(
            absence_excused_deduction_days, "1.00"),
        monthly_free_late_minutes=_int(monthly_free_late_minutes),
        daily_free_late_minutes_cap=_int(daily_free_late_minutes_cap),
        permission_count_per_month=_int(permission_count_per_month),
        permission_max_hours=_dec(permission_max_hours, "0"),
        created_by=created_by,
    )
    db.session.add(p)
    db.session.commit()
    return p


def update_violation_policy(policy, **fields):
    scope = _as_scope(fields.get("scope", policy.scope))
    department_id = fields.get("department_id", policy.department_id)
    employee_id = fields.get("employee_id", policy.employee_id)
    department_id = department_id if scope == PolicyScope.DEPARTMENT else None
    employee_id = employee_id if scope == PolicyScope.EMPLOYEE else None
    _validate_target(policy.company_id, scope, department_id, employee_id)

    clash = AttendanceViolationPolicy.query.filter(
        AttendanceViolationPolicy.id != policy.id,
        AttendanceViolationPolicy.company_id == policy.company_id,
        AttendanceViolationPolicy.scope == scope,
        AttendanceViolationPolicy.department_id == department_id,
        AttendanceViolationPolicy.employee_id == employee_id).first()
    if clash:
        raise ViolationError("توجد سياسة انتهاكات بنفس النطاق بالفعل")

    policy.scope = scope
    policy.department_id = department_id
    policy.employee_id = employee_id
    for f, default in (
        ("absence_unexcused_deduction_days", "1.00"),
        ("absence_excused_deduction_days", "1.00"),
        ("permission_max_hours", "0"),
    ):
        if f in fields:
            setattr(policy, f, _dec(fields[f], default))
    for f in ("monthly_free_late_minutes",
              "daily_free_late_minutes_cap",
              "permission_count_per_month"):
        if f in fields:
            setattr(policy, f, _int(fields[f]))
    if "is_active" in fields:
        policy.is_active = bool(fields["is_active"])
    db.session.commit()
    return policy


def delete_violation_policy(policy):
    db.session.delete(policy)
    db.session.commit()


# ─── Permission-request workflow ────────────────────────────────────────
# Mirrors the leave-request workflow field for field. Kept in this file
# (not services/leave.py) because the caps that gate it come from the
# violation policy, and separating the two would mean the CRUD called
# resolve_violation_policy_for_employee across a module boundary for a
# rule that is 20 lines long.

def _month_bounds(d):
    from calendar import monthrange
    start = date(d.year, d.month, 1)
    end = date(d.year, d.month, monthrange(d.year, d.month)[1])
    return start, end


def submit_permission_request(*, company_id, employee_id, request_date,
                              hours_count, start_time=None, end_time=None,
                              reason=None, created_by=None):
    """Employee (or HR on their behalf) files a permission request for a
    single day. Refuses when the policy caps are exceeded, or when the
    employee has no violation policy configured (permissions are the
    policy's feature — no policy means the button should not exist).

    PENDING at rest; approve/reject/cancel move it. On approval,
    compute_late_deduction() subtracts hours_count from that day's LATE
    minutes BEFORE the daily cap and monthly pool apply.
    """
    if request_date is None:
        raise ViolationError("تاريخ الاستئذان مطلوب")
    hours = _dec(hours_count, "0")
    if hours <= 0:
        raise ViolationError("عدد الساعات يجب أن يكون أكبر من صفر")
    if start_time is not None and end_time is not None and end_time <= start_time:
        raise ViolationError("وقت النهاية يجب أن يكون بعد وقت البداية")

    emp = db.session.get(Employee, employee_id)
    if not emp or emp.company_id != company_id:
        raise ViolationError("الموظف غير موجود")

    policy = resolve_violation_policy_for_employee(employee_id, request_date)
    if policy is None:
        raise ViolationError(
            "لا توجد سياسة انتهاكات مفعّلة — أضف واحدة قبل قبول طلبات الاستئذان")
    if int(policy.permission_count_per_month or 0) <= 0:
        raise ViolationError(
            "السياسة الحالية لا تسمح بطلبات استئذان — عدّل الحد الشهري في السياسة")
    if policy.permission_max_hours and hours > Decimal(policy.permission_max_hours):
        raise ViolationError(
            f"المدة تتجاوز الحد الأقصى المسموح ({float(policy.permission_max_hours):g} ساعة)")

    # Monthly count refuses the Nth+1 — cancelled requests do not count.
    start, end = _month_bounds(request_date)
    used = LatePermissionRequest.query.filter(
        LatePermissionRequest.employee_id == employee_id,
        LatePermissionRequest.status.in_([
            PermissionStatus.PENDING, PermissionStatus.APPROVED]),
        LatePermissionRequest.request_date >= start,
        LatePermissionRequest.request_date <= end,
    ).count()
    if used >= int(policy.permission_count_per_month or 0):
        raise ViolationError(
            f"تجاوزت الحد الشهري لطلبات الاستئذان ({policy.permission_count_per_month})")

    req = LatePermissionRequest(
        company_id=company_id, employee_id=employee_id,
        request_date=request_date,
        start_time=start_time, end_time=end_time,
        hours_count=hours,
        reason=(reason or "").strip() or None,
        status=PermissionStatus.PENDING,
        created_by=created_by,
    )
    db.session.add(req)
    db.session.commit()
    return req


def approve_permission_request(req, *, reviewer_id, review_note=None):
    if req.status != PermissionStatus.PENDING:
        raise ViolationError("يمكن اعتماد الطلبات في حالة الانتظار فقط")
    req.status = PermissionStatus.APPROVED
    req.reviewed_by = reviewer_id
    req.reviewed_at = datetime.utcnow()
    if review_note:
        req.review_note = review_note
    db.session.commit()
    return req


def reject_permission_request(req, *, reviewer_id, review_note=None):
    if req.status != PermissionStatus.PENDING:
        raise ViolationError("يمكن رفض الطلبات في حالة الانتظار فقط")
    req.status = PermissionStatus.REJECTED
    req.reviewed_by = reviewer_id
    req.reviewed_at = datetime.utcnow()
    req.review_note = (review_note or None)
    db.session.commit()
    return req


def cancel_permission_request(req, *, reviewer_id, review_note=None):
    if req.status == PermissionStatus.CANCELLED:
        return req
    req.status = PermissionStatus.CANCELLED
    req.reviewed_by = reviewer_id
    req.reviewed_at = datetime.utcnow()
    if review_note:
        req.review_note = review_note
    db.session.commit()
    return req


def approved_permissions_for(employee_id, year, month):
    """The month's approved permissions for one employee — the query
    compute_late_deduction runs before applying cap+pool."""
    from calendar import monthrange
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    return LatePermissionRequest.query.filter(
        LatePermissionRequest.employee_id == employee_id,
        LatePermissionRequest.status == PermissionStatus.APPROVED,
        LatePermissionRequest.request_date >= start,
        LatePermissionRequest.request_date <= end,
    ).all()
