"""MARSOUD-INSIGHTS-AGENT-PROFESSIONAL (2026-08-06) — Tasks reads.

Wraps the pure task-stats functions that already exist in
`services/tasks_extras.py` + the two private helpers on
`routes/tasks.py` (they're pure, safe to call from an agent).
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from app import db
from app.agent.insights_catalog import (
    register, has_perm, perm_denied, parse_date,
)


# ─── tasks_team_stats ───────────────────────────────────────────
@register(
    name="tasks_team_stats",
    description=(
        "إحصائيات مفصلة لكل عضو في الفريق: عدد المهام، الحالات، "
        "avg_time_to_close، velocity_30d، overdue_ratio، "
        "on_time_rate، والـ badges التلقائية."),
    input_schema={
        "type": "object",
        "properties": {
            "since": {
                "type": "string",
                "description": "تاريخ يقصر النتائج على مهام أُنشئت من التاريخ ده لحد النهارده.",
            },
        },
    },
    permission="tasks.view_all",
)
def tasks_team_stats(args, company_id, user_id):
    if not has_perm(user_id, company_id, "tasks.view_all"):
        return perm_denied("tasks.view_all")
    from app.services.tasks_extras import team_stats
    since = None
    if args.get("since"):
        since = parse_date(args["since"])
    out = team_stats(company_id, since=since)
    # Strip User ORM refs from the rows (JSON-primitive contract).
    rows = []
    for r in out.get("rows", []):
        row = dict(r)
        u = row.pop("user", None)
        if u is not None:
            row["user_id"] = getattr(u, "id", None)
            row["user_name"] = getattr(u, "full_name", None)
        rows.append(row)
    return {
        "rows": rows,
        "closed_per_week": out.get("closed_per_week"),
        "status_dist": out.get("status_dist"),
    }


# ─── tasks_employee_buckets ────────────────────────────────────
@register(
    name="tasks_employee_buckets",
    description=(
        "إجمالي/منجز/جاري/متأخر/بانتظار المراجعة لكل موظف. "
        "مثالي لعرض cards على شاشة الموظفين."),
    input_schema={"type": "object", "properties": {}},
    permission="tasks.view_all",
)
def tasks_employee_buckets(args, company_id, user_id):
    if not has_perm(user_id, company_id, "tasks.view_all"):
        return perm_denied("tasks.view_all")
    from app.routes.tasks import _employee_task_buckets
    rows = _employee_task_buckets(company_id)
    out = []
    for r in rows:
        row = dict(r)
        u = row.pop("user", None)
        if u is not None:
            row["user_id"] = getattr(u, "id", None)
            row["user_name"] = getattr(u, "full_name", None)
        out.append(row)
    return {"rows": out}


# ─── tasks_monthly_for_employee ────────────────────────────────
@register(
    name="tasks_monthly_for_employee",
    description=(
        "توزيع المهام المُقفلة (assignee-closed) على آخر N شهر "
        "لموظف واحد بالـ user_id. مفيد للتريند الشهري."),
    input_schema={
        "type": "object",
        "properties": {
            "user_id": {"type": "integer"},
            "months": {"type": "integer"},
        },
        "required": ["user_id"],
    },
    permission="tasks.view_all",
)
def tasks_monthly_for_employee(args, company_id, user_id):
    if not has_perm(user_id, company_id, "tasks.view_all"):
        return perm_denied("tasks.view_all")
    from app.routes.tasks import _employee_monthly_stats
    months = int(args.get("months") or 6)
    return _employee_monthly_stats(
        company_id, int(args["user_id"]), months=months)


# ─── tasks_get_task ─────────────────────────────────────────────
@register(
    name="tasks_get_task",
    description=(
        "تفاصيل مهمة واحدة بالـ id: العنوان، الحالة، الأولوية، "
        "الـ deadline، المشروع، المكلَّفون، المنشئ."),
    input_schema={
        "type": "object",
        "properties": {"task_id": {"type": "integer"}},
        "required": ["task_id"],
    },
    permission="tasks.view",
)
def tasks_get_task(args, company_id, user_id):
    if not has_perm(user_id, company_id, "tasks.view"):
        return perm_denied("tasks.view")
    from app.models import Task
    from app.services.tasks_extras import assignee_ids_for
    t = Task.query.filter_by(
        id=int(args["task_id"]), company_id=company_id).first()
    if not t:
        return {"error": "task غير موجود"}
    return {
        "id": t.id, "title": t.title, "description": t.description,
        "status": t.status.value if t.status else None,
        "priority": t.priority.value if t.priority else None,
        "deadline": t.deadline.isoformat() if t.deadline else None,
        "project_id": t.project_id, "milestone_id": t.milestone_id,
        "assignee_ids": sorted(assignee_ids_for(t)),
        "created_by_id": t.created_by_id,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "completed_at": (t.completed_at.isoformat()
                         if t.completed_at else None),
    }
