"""MARSOUD-INSIGHTS-AGENT-PROFESSIONAL (2026-08-06) — HR reads.

The people-scope tools cover employees, departments, leave, and
attendance. Every tool honours the same two-layer permission model
(insights.use at the route + payroll.view / employees.view per
tool for sensitive slices).

Naming convention: `hr_*` prefix so the model can shortlist when
choosing between 100 tools.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from app import db
from app.agent.insights_catalog import (
    register, has_perm, perm_denied, parse_date,
)


# ─── hr_list_employees ─────────────────────────────────────────
@register(
    name="hr_list_employees",
    description=(
        "قائمة الموظفين النشطين في الشركة مع الأساسيات (الاسم، "
        "الايميل، القسم، تاريخ التعيين، الحالة). البحث بالاسم "
        "أو الايميل."),
    input_schema={
        "type": "object",
        "properties": {
            "search": {"type": "string"},
            "department_id": {"type": "integer"},
            "limit": {"type": "integer"},
        },
    },
    permission="employees.view",
)
def hr_list_employees(args, company_id, user_id):
    if not has_perm(user_id, company_id, "employees.view"):
        return perm_denied("employees.view")
    from app.models import Employee
    q = Employee.query.filter_by(company_id=company_id)
    if args.get("department_id"):
        q = q.filter(Employee.department_id == int(args["department_id"]))
    search = (args.get("search") or "").strip()
    if search:
        from sqlalchemy import or_
        q = q.filter(or_(Employee.name.ilike(f"%{search}%"),
                         Employee.email.ilike(f"%{search}%")))
    limit = min(int(args.get("limit") or 100), 500)
    rows = q.order_by(Employee.name.asc()).limit(limit).all()
    return {
        "count": q.count(),
        "employees": [
            {"id": e.id, "name": e.name,
             "email": getattr(e, "email", None),
             "job_title": getattr(e, "job_title", None),
             "department_id": getattr(e, "department_id", None),
             "start_date": (e.start_date.isoformat()
                           if getattr(e, "start_date", None) else None),
             "user_id": e.user_id,
             "status": (e.status.value
                        if getattr(e, "status", None) else None)}
            for e in rows
        ],
    }


# ─── hr_get_employee ───────────────────────────────────────────
@register(
    name="hr_get_employee",
    description=(
        "تفاصيل موظف واحد بالـ id: بياناته الأساسية + رقم "
        "المستخدم المربوط + تاريخ التعيين + الراتب لو عندك "
        "payroll.view."),
    input_schema={
        "type": "object",
        "properties": {
            "employee_id": {"type": "integer"},
        },
        "required": ["employee_id"],
    },
    permission="employees.view",
)
def hr_get_employee(args, company_id, user_id):
    if not has_perm(user_id, company_id, "employees.view"):
        return perm_denied("employees.view")
    from app.models import Employee
    emp = Employee.query.filter_by(
        id=int(args["employee_id"]),
        company_id=company_id).first()
    if not emp:
        return {"error": "الموظف غير موجود"}
    out = {
        "id": emp.id, "name": emp.name,
        "email": getattr(emp, "email", None),
        "phone": getattr(emp, "phone", None),
        "job_title": getattr(emp, "job_title", None),
        "department_id": getattr(emp, "department_id", None),
        "user_id": emp.user_id,
        "start_date": (emp.start_date.isoformat()
                      if getattr(emp, "start_date", None) else None),
        "status": (emp.status.value
                   if getattr(emp, "status", None) else None),
    }
    if has_perm(user_id, company_id, "payroll.view"):
        out["basic_salary"] = float(getattr(emp, "basic_salary", 0) or 0)
        out["allowances"] = float(getattr(emp, "allowances", 0) or 0)
    else:
        out["basic_salary"] = None
        out["_note_salary"] = "قراءة الراتب محتاجة payroll.view."
    return out


# ─── hr_list_departments ───────────────────────────────────────
@register(
    name="hr_list_departments",
    description="قائمة أقسام الشركة مع عدد الموظفين في كل قسم.",
    input_schema={"type": "object", "properties": {}},
    permission="employees.view",
)
def hr_list_departments(args, company_id, user_id):
    if not has_perm(user_id, company_id, "employees.view"):
        return perm_denied("employees.view")
    from app.models import Department, Employee
    depts = Department.query.filter_by(company_id=company_id).all()
    return {
        "departments": [
            {"id": d.id, "name": d.name,
             "manager_id": getattr(d, "manager_id", None),
             "employee_count": Employee.query.filter_by(
                 company_id=company_id,
                 department_id=d.id).count()}
            for d in depts
        ],
    }


# ─── hr_expiring_contracts ─────────────────────────────────────
@register(
    name="hr_expiring_contracts",
    description=(
        "الموظفين اللي عقودهم بتخلص خلال أفق زمني (default 60 "
        "يوم). كل صف بيرجع بأيام متبقية + severity."),
    input_schema={
        "type": "object",
        "properties": {
            "horizon_days": {"type": "integer"},
        },
    },
    permission="employees.view",
)
def hr_expiring_contracts(args, company_id, user_id):
    if not has_perm(user_id, company_id, "employees.view"):
        return perm_denied("employees.view")
    from app.models import Employee
    today = date.today()
    horizon = int(args.get("horizon_days") or 60)
    limit_date = today + timedelta(days=horizon)
    rows = Employee.query.filter(
        Employee.company_id == company_id,
        Employee.contract_end_date.isnot(None),
        Employee.contract_end_date <= limit_date,
    ).order_by(Employee.contract_end_date.asc()).all()
    out = []
    for e in rows:
        days_left = (e.contract_end_date - today).days
        severity = ("expired" if days_left < 0
                    else "urgent" if days_left <= 14
                    else "soon" if days_left <= 30
                    else "watch")
        out.append({
            "id": e.id, "name": e.name,
            "contract_end_date": e.contract_end_date.isoformat(),
            "days_left": days_left,
            "severity": severity,
        })
    return {"horizon_days": horizon, "rows": out}


# ─── hr_employee_leave_balances ────────────────────────────────
@register(
    name="hr_employee_leave_balances",
    description=(
        "أرصدة الإجازات لموظف واحد لسنة معينة (default = "
        "السنة الحالية)."),
    input_schema={
        "type": "object",
        "properties": {
            "employee_id": {"type": "integer"},
            "year": {"type": "integer"},
        },
        "required": ["employee_id"],
    },
    permission="employees.view",
)
def hr_employee_leave_balances(args, company_id, user_id):
    if not has_perm(user_id, company_id, "employees.view"):
        return perm_denied("employees.view")
    from app.models import LeaveBalance, LeaveType
    year = int(args.get("year") or date.today().year)
    try:
        from app.services.leave import ensure_employee_balances
        ensure_employee_balances(int(args["employee_id"]), year)
    except Exception:  # noqa: BLE001
        pass
    balances = LeaveBalance.query.filter_by(
        employee_id=int(args["employee_id"]), year=year).all()
    return {
        "year": year,
        "balances": [
            {"leave_type_id": b.leave_type_id,
             "leave_type_name": (
                 (t := db.session.get(LeaveType, b.leave_type_id))
                 and t.name),
             "allotted": float(getattr(b, "allotted", 0) or 0),
             "used": float(getattr(b, "used", 0) or 0),
             "remaining": float(getattr(b, "remaining", 0) or 0)}
            for b in balances
        ],
    }


# ─── hr_attendance_summary ─────────────────────────────────────
@register(
    name="hr_attendance_summary",
    description=(
        "ملخص الحضور والغياب لموظف واحد في شهر معين: عدد أيام "
        "الغياب، عدد أيام التأخير، الأذون المعتمدة، والخصم "
        "المستحق (بيحتاج payroll.view)."),
    input_schema={
        "type": "object",
        "properties": {
            "employee_id": {"type": "integer"},
            "year": {"type": "integer"},
            "month": {"type": "integer"},
        },
        "required": ["employee_id", "year", "month"],
    },
    permission="employees.view",
)
def hr_attendance_summary(args, company_id, user_id):
    if not has_perm(user_id, company_id, "employees.view"):
        return perm_denied("employees.view")
    from app.services.leave import (
        exceptions_in_period, attendance_deductions,
    )
    from app.services.violation import approved_permissions_for
    emp_id = int(args["employee_id"])
    y, m = int(args["year"]), int(args["month"])
    excs = exceptions_in_period(company_id, y, m, employee_id=emp_id)
    perms = approved_permissions_for(emp_id, y, m)
    out = {
        "employee_id": emp_id, "year": y, "month": m,
        "exceptions": [
            {"id": e.id,
             "type": (e.type.value
                      if hasattr(e.type, "value") else str(e.type)),
             "date": e.date.isoformat() if e.date else None,
             "hours": float(getattr(e, "hours", 0) or 0)}
            for e in excs
        ],
        "approved_permissions_count": len(perms),
    }
    if has_perm(user_id, company_id, "payroll.view"):
        out["deductions"] = attendance_deductions(emp_id, y, m)
    else:
        out["deductions"] = None
        out["_note_deductions"] = (
            "قراءة الخصم محتاجة payroll.view.")
    return out


# ─── hr_late_breakdown ─────────────────────────────────────────
@register(
    name="hr_late_breakdown",
    description=(
        "تفصيل التأخير لموظف في شهر معين: عدد الأيام المخصومة، "
        "الرصيد المستخدم من pool، الرصيد المتبقي. بيحتاج "
        "payroll.view."),
    input_schema={
        "type": "object",
        "properties": {
            "employee_id": {"type": "integer"},
            "year": {"type": "integer"},
            "month": {"type": "integer"},
        },
        "required": ["employee_id", "year", "month"],
    },
    permission="payroll.view",
)
def hr_late_breakdown(args, company_id, user_id):
    if not has_perm(user_id, company_id, "payroll.view"):
        return perm_denied("payroll.view")
    from app.services.payroll import late_month_breakdown
    from app.services.violation import (
        resolve_violation_policy_for_employee,
    )
    emp_id = int(args["employee_id"])
    y, m = int(args["year"]), int(args["month"])
    policy = resolve_violation_policy_for_employee(emp_id)
    return {
        "employee_id": emp_id, "year": y, "month": m,
        "breakdown": late_month_breakdown(emp_id, y, m, policy=policy),
        "policy_id": policy.id if policy else None,
    }
