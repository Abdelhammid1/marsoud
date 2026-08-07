"""MARSOUD-INSIGHTS-AGENT-PROFESSIONAL (2026-08-06) — the three
composite tools that turn the analyst from a data-fetcher into an
analyst.

Each composite answers a question the manager actually asks:
  · analyze_employee — "إزاي أداء أحمد الشهر ده؟" (all four axes
    at once — attendance, tasks, advances, evaluation).
  · analyze_department — "إزاي قسم المبيعات الشهر ده؟" (per-member
    rows + rollup + top/bottom highlights).
  · compare_period — "قارن الشهر ده بالشهر اللي فات" (current +
    prior + delta on any reports.py surface).

Composites let the model answer in ONE tool call instead of 6-8
atomic ones. That's the biggest lever for latency — fewer provider
round-trips per turn. The atomic tools still exist alongside for
follow-up drill-downs; the prompt tells the model to prefer
composites when the question fits.

Permission strategy: composites don't refuse the whole call when a
sensitive slice is denied. They dim that slice to `None` and
populate a per-slice `_note` so the analyst can explain the gap
to the user. That way an `hr_manager` who lacks `payroll.view` still
gets the attendance + task picture, just not the salary numbers.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from app import db
from app.agent.insights_catalog import (
    register, has_perm, parse_date,
)


# ═══════ analyze_employee ═══════════════════════════════════════
@register(
    name="analyze_employee",
    description=(
        "تحليل احترافي لموظف فردي بالاسم أو بالـ id: البيانات "
        "الأساسية + الحضور والغياب + المهام (شهرياً) + السلف + "
        "أحدث تقييم. لو الاسم فيه أكتر من موظف بيرجع قائمة "
        "توضيح بدل تحليل غلط. استخدمه بدل ما تنادي 5 أدوات "
        "مختلفة عشان توفر turns."),
    input_schema={
        "type": "object",
        "properties": {
            "employee": {
                "type": "string",
                "description": "اسم الموظف (بحث fuzzy) أو employee_id.",
            },
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
        },
        "required": ["employee"],
    },
    permission="employees.view",
)
def analyze_employee(args, company_id, user_id):
    if not has_perm(user_id, company_id, "employees.view"):
        from app.agent.insights_catalog import perm_denied
        return perm_denied("employees.view")
    from app.models import Employee, User
    from app.models.crm import Task, TaskStatus
    from sqlalchemy import or_

    raw = str(args.get("employee") or "").strip()
    if not raw:
        return {"error": "employee (اسم أو id) مطلوب"}

    # Resolve — try as id first, then fuzzy name match.
    emp = None
    if raw.isdigit():
        emp = Employee.query.filter_by(
            id=int(raw), company_id=company_id).first()
    if emp is None:
        matches = Employee.query.filter(
            Employee.company_id == company_id,
            or_(Employee.name.ilike(f"%{raw}%"),
                Employee.email.ilike(f"%{raw}%")),
        ).all()
        if len(matches) == 0:
            return {"error": f"مافيش موظف اسمه أو ايميله فيه {raw!r}"}
        if len(matches) > 1:
            return {
                "disambiguation": [
                    {"id": m.id, "name": m.name,
                     "email": getattr(m, "email", None),
                     "department_id": getattr(m, "department_id", None),
                     "start_date": (m.start_date.isoformat()
                                   if getattr(m, "start_date", None)
                                   else None)}
                    for m in matches[:10]
                ],
                "note": (f"في {len(matches)} موظفين اسمهم فيه "
                         f"{raw!r} — نادي الأداة تاني بالـ id "
                         "أو باسم أدق."),
            }
        emp = matches[0]

    today = date.today()
    default_start = today - timedelta(days=30)
    start = parse_date(args.get("start_date"), fallback=default_start)
    end = parse_date(args.get("end_date"), fallback=today)

    # Profile.
    profile = {
        "id": emp.id, "name": emp.name,
        "email": getattr(emp, "email", None),
        "phone": getattr(emp, "phone", None),
        "job_title": getattr(emp, "job_title", None),
        "department_id": getattr(emp, "department_id", None),
        "user_id": emp.user_id,
        "start_date": (emp.start_date.isoformat()
                      if getattr(emp, "start_date", None) else None),
        "status": getattr(emp.status, "value", None)
                  if getattr(emp, "status", None) else None,
    }

    # Attendance slice — needs payroll.view for the salary-adjacent
    # deduction numbers. exceptions_in_period is fine on employees.view.
    attendance = {"note": None}
    try:
        from app.services.leave import (
            exceptions_in_period, attendance_deductions,
        )
        y, m = start.year, start.month
        excs = exceptions_in_period(
            company_id, y, m, employee_id=emp.id)
        attendance["exceptions"] = [
            {"id": e.id,
             "type": (e.type.value
                      if hasattr(e.type, "value") else str(e.type)),
             "date": e.date.isoformat() if e.date else None,
             "hours": float(getattr(e, "hours", 0) or 0),
             "note": getattr(e, "note", None)}
            for e in excs
        ]
        if has_perm(user_id, company_id, "payroll.view"):
            attendance["deductions"] = attendance_deductions(
                emp.id, y, m)
        else:
            attendance["deductions"] = None
            attendance["_note_deductions"] = (
                "احتساب الخصومات محتاج payroll.view — ما رجّعناش الأرقام.")
    except Exception as e:  # noqa: BLE001
        attendance["note"] = f"تعذّر قراءة الحضور: {str(e)[:120]}"

    # Late-permission summary.
    try:
        from app.services.violation import (
            approved_permissions_for,
            resolve_violation_policy_for_employee,
        )
        from app.services.payroll import late_month_breakdown
        y, m = start.year, start.month
        perms = approved_permissions_for(emp.id, y, m)
        attendance["approved_permissions"] = [
            {"id": p.id,
             "date": p.date.isoformat() if p.date else None,
             "minutes": getattr(p, "minutes", None)}
            for p in perms
        ]
        if has_perm(user_id, company_id, "payroll.view"):
            policy = resolve_violation_policy_for_employee(emp.id)
            attendance["late_breakdown"] = late_month_breakdown(
                emp.id, y, m, policy=policy)
    except Exception:  # noqa: BLE001
        pass

    # Tasks slice — via the monthly stats function.
    tasks_slice = {"note": None}
    if emp.user_id:
        try:
            from app.routes.tasks import _employee_monthly_stats
            monthly = _employee_monthly_stats(
                company_id, emp.user_id, months=6)
            # Snapshot right now.
            by_status = {s.value: Task.query.filter(
                Task.company_id == company_id,
                Task.assigned_to_id == emp.user_id,
                Task.status == s,
            ).count() for s in TaskStatus}
            overdue_now = Task.query.filter(
                Task.company_id == company_id,
                Task.assigned_to_id == emp.user_id,
                Task.deadline.isnot(None),
                Task.deadline < today,
                Task.status.notin_((TaskStatus.DONE,
                                    TaskStatus.BLOCKED))
            ).count()
            tasks_slice.update({
                "monthly_closed": monthly.get("closed"),
                "monthly_labels": monthly.get("labels"),
                "user_total_tasks": monthly.get("user_total"),
                "by_status_now": by_status,
                "overdue_now": overdue_now,
            })
        except Exception as e:  # noqa: BLE001
            tasks_slice["note"] = f"تعذّر قراءة المهام: {str(e)[:120]}"
    else:
        tasks_slice["note"] = "الموظف مش مربوط بـ User — مافيش سجل مهام."

    # Advances — payroll.view.
    advances = {"note": None}
    if has_perm(user_id, company_id, "payroll.view"):
        try:
            from app.services.advances import (
                active_advance_for, repayments_for,
            )
            active = active_advance_for(emp.id)
            advances["active"] = None
            if active:
                advances["active"] = {
                    "id": active.id,
                    "amount": float(active.amount or 0),
                    "installment_amount": float(
                        getattr(active, "installment_amount", 0) or 0),
                    "remaining": float(
                        getattr(active, "remaining_amount", 0) or 0),
                    "status": getattr(active.status, "value", None)
                              if getattr(active, "status", None) else None,
                }
                reps = repayments_for(active.id)
                advances["repayments"] = [
                    {"id": r.id,
                     "amount": float(r.amount or 0),
                     "date": r.date.isoformat()
                             if getattr(r, "date", None) else None}
                    for r in reps
                ]
        except Exception as e:  # noqa: BLE001
            advances["note"] = f"تعذّر قراءة السلف: {str(e)[:120]}"
    else:
        advances["note"] = (
            "قراءة السلف محتاجة payroll.view — ما رجّعناش الأرقام.")

    # Latest evaluation, if evaluations module is present.
    evaluation = {"note": None}
    try:
        from app.models import EmployeeEvaluation
        last = (EmployeeEvaluation.query
                .filter_by(employee_id=emp.id)
                .order_by(EmployeeEvaluation.id.desc())
                .first())
        if last:
            evaluation["latest"] = {
                "cycle_id": last.cycle_id,
                "final_score": float(getattr(last, "final_score", 0) or 0),
                "created_at": (last.created_at.isoformat()
                               if getattr(last, "created_at", None)
                               else None),
            }
    except Exception:  # noqa: BLE001
        pass

    return {
        "employee": profile,
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "attendance": attendance,
        "tasks": tasks_slice,
        "advances": advances,
        "evaluation": evaluation,
    }


# ═══════ analyze_department ═════════════════════════════════════
@register(
    name="analyze_department",
    description=(
        "تحليل احترافي لقسم كامل: قائمة الأعضاء + إحصائية "
        "مجمعة على مستوى القسم (مهام مُنجزة، متأخرة، تقييمات) + "
        "أفضل وأسوأ موظف على كل محور. استخدمه لسؤال زي "
        "'إزاي قسم المبيعات الشهر ده'."),
    input_schema={
        "type": "object",
        "properties": {
            "department": {
                "type": "string",
                "description": "اسم القسم أو department_id.",
            },
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
        },
        "required": ["department"],
    },
    permission="employees.view",
)
def analyze_department(args, company_id, user_id):
    if not has_perm(user_id, company_id, "employees.view"):
        from app.agent.insights_catalog import perm_denied
        return perm_denied("employees.view")
    from app.models import Department, Employee
    from app.models.crm import Task, TaskStatus
    from sqlalchemy import or_

    raw = str(args.get("department") or "").strip()
    if not raw:
        return {"error": "department (اسم أو id) مطلوب"}
    dept = None
    if raw.isdigit():
        dept = Department.query.filter_by(
            id=int(raw), company_id=company_id).first()
    if dept is None:
        matches = Department.query.filter(
            Department.company_id == company_id,
            Department.name.ilike(f"%{raw}%"),
        ).all()
        if len(matches) == 0:
            return {"error": f"مافيش قسم اسمه فيه {raw!r}"}
        if len(matches) > 1:
            return {
                "disambiguation": [
                    {"id": d.id, "name": d.name}
                    for d in matches[:10]],
                "note": f"في {len(matches)} أقسام — نادي بالـ id.",
            }
        dept = matches[0]

    today = date.today()
    default_start = today - timedelta(days=30)
    start = parse_date(args.get("start_date"), fallback=default_start)
    end = parse_date(args.get("end_date"), fallback=today)
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end + timedelta(days=1),
                              datetime.min.time())

    members = Employee.query.filter_by(
        company_id=company_id, department_id=dept.id).all()

    per_employee = []
    for e in members:
        row = {"employee_id": e.id, "name": e.name,
               "user_id": e.user_id, "closed": 0, "overdue": 0}
        if e.user_id:
            row["closed"] = Task.query.filter(
                Task.company_id == company_id,
                Task.assigned_to_id == e.user_id,
                Task.status == TaskStatus.DONE,
                Task.updated_at >= start_dt,
                Task.updated_at < end_dt,
            ).count()
            row["overdue"] = Task.query.filter(
                Task.company_id == company_id,
                Task.assigned_to_id == e.user_id,
                Task.deadline.isnot(None),
                Task.deadline < today,
                Task.status.notin_((TaskStatus.DONE,
                                    TaskStatus.BLOCKED))
            ).count()
        # Optional: latest evaluation score.
        try:
            from app.models import EmployeeEvaluation
            last = (EmployeeEvaluation.query
                    .filter_by(employee_id=e.id)
                    .order_by(EmployeeEvaluation.id.desc())
                    .first())
            row["last_evaluation_score"] = (
                float(getattr(last, "final_score", 0) or 0)
                if last else None)
        except Exception:  # noqa: BLE001
            row["last_evaluation_score"] = None
        per_employee.append(row)

    # Rollup.
    total_closed = sum(r["closed"] for r in per_employee)
    total_overdue = sum(r["overdue"] for r in per_employee)
    top_closer = (max(per_employee, key=lambda r: r["closed"])
                  if per_employee else None)
    worst_overdue = (max(per_employee, key=lambda r: r["overdue"])
                     if per_employee else None)
    scored = [r for r in per_employee
              if r["last_evaluation_score"] is not None]
    top_score = (max(scored, key=lambda r: r["last_evaluation_score"])
                 if scored else None)

    return {
        "department": {"id": dept.id, "name": dept.name},
        "period": {"start": start.isoformat(),
                   "end": end.isoformat()},
        "member_count": len(members),
        "rollup": {
            "total_closed": total_closed,
            "total_overdue": total_overdue,
        },
        "members": per_employee,
        "highlights": {
            "top_closer": ({"employee_id": top_closer["employee_id"],
                            "name": top_closer["name"],
                            "closed": top_closer["closed"]}
                           if top_closer and top_closer["closed"] > 0
                           else None),
            "worst_overdue": ({"employee_id": worst_overdue["employee_id"],
                               "name": worst_overdue["name"],
                               "overdue": worst_overdue["overdue"]}
                              if worst_overdue and worst_overdue["overdue"] > 0
                              else None),
            "top_evaluation_score": ({
                "employee_id": top_score["employee_id"],
                "name": top_score["name"],
                "score": top_score["last_evaluation_score"]}
                if top_score else None),
        },
    }


# ═══════ compare_period ═════════════════════════════════════════
_COMPARE_REPORT_KINDS = {
    "income_statement", "dashboard_metrics",
    "balance_sheet", "cash_flow", "aging_report",
    "payroll_summary", "expenses_summary",
}


@register(
    name="compare_period",
    description=(
        "مقارنة تقرير مالي بين فترتين: الحالية vs السابقة "
        "(نفس المدة قبلها بشكل تلقائي، أو تمرّرها انت). "
        "بيرجع {current, prior, delta} لكل رقم قابل للمقارنة. "
        "يدعم: income_statement, dashboard_metrics, "
        "balance_sheet, cash_flow, aging_report, payroll_summary, "
        "expenses_summary."),
    input_schema={
        "type": "object",
        "properties": {
            "report_type": {
                "type": "string",
                "description": "نوع التقرير — واحد من القائمة أعلاه.",
            },
            "curr_start": {"type": "string"},
            "curr_end": {"type": "string"},
            "prior_start": {"type": "string"},
            "prior_end": {"type": "string"},
        },
        "required": ["report_type", "curr_start", "curr_end"],
    },
    permission="reports.view",
)
def compare_period(args, company_id, user_id):
    if not has_perm(user_id, company_id, "reports.view"):
        from app.agent.insights_catalog import perm_denied
        return perm_denied("reports.view")
    kind = str(args.get("report_type") or "").strip().lower()
    if kind not in _COMPARE_REPORT_KINDS:
        return {"error": (f"report_type غير مدعوم: {kind!r}. "
                          f"المدعوم: {sorted(_COMPARE_REPORT_KINDS)}")}
    curr_start = parse_date(args.get("curr_start"))
    curr_end = parse_date(args.get("curr_end"))
    if curr_end < curr_start:
        return {"error": "curr_end قبل curr_start"}

    # Default prior window = same length immediately before current.
    window_days = (curr_end - curr_start).days + 1
    prior_end = parse_date(args.get("prior_end"),
                           fallback=curr_start - timedelta(days=1))
    prior_start = parse_date(
        args.get("prior_start"),
        fallback=prior_end - timedelta(days=window_days - 1))

    def _run(rtype, s, e):
        from app.services import reports as R
        from app.models import Company
        co = db.session.get(Company, company_id)
        if rtype == "income_statement":
            return R.income_statement(company_id, s, e)
        if rtype == "dashboard_metrics":
            return R.dashboard_metrics(company_id, period="month")
        if rtype == "balance_sheet":
            return R.balance_sheet(company_id, e)
        if rtype == "cash_flow":
            return R.cash_flow(company_id, s, e)
        if rtype == "aging_report":
            return R.aging_report(company_id)
        if rtype == "payroll_summary":
            return R.payroll_summary_report(
                company_id, year=s.year, month=s.month)
        if rtype == "expenses_summary":
            # expenses_summary may not exist as a top-level; fall
            # back to income_statement's expenses slice.
            try:
                return R.expenses_summary(company_id, s, e)
            except AttributeError:
                inc = R.income_statement(company_id, s, e)
                return {"expenses": inc.get("expenses")}
        return {"error": f"unsupported: {rtype}"}

    current = _run(kind, curr_start, curr_end)
    prior = _run(kind, prior_start, prior_end)

    # Compute delta on comparable numeric top-level keys.
    def _numeric(d):
        out = {}
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, (int, float)):
                    out[k] = v
        return out

    curr_flat = _numeric(current)
    prior_flat = _numeric(prior)
    delta = {}
    for k, cur_v in curr_flat.items():
        pr_v = prior_flat.get(k, 0)
        abs_d = cur_v - pr_v
        pct = None
        if pr_v not in (0, None):
            pct = round((abs_d / pr_v) * 100, 1)
        delta[k] = {"absolute": round(abs_d, 2),
                    "pct": pct,
                    "current": cur_v, "prior": pr_v}

    return {
        "report_type": kind,
        "current_period": {"start": curr_start.isoformat(),
                           "end": curr_end.isoformat()},
        "prior_period": {"start": prior_start.isoformat(),
                         "end": prior_end.isoformat()},
        "current": current,
        "prior": prior,
        "delta": delta,
    }
