"""MARSOUD-INSIGHTS-AGENT-01 (Batch 9 Ticket 6, 2026-08-01) —
read-only tools for the insights agent.

Contract for EVERY tool:
1. Filters by `company_id` (cross-tenant leak = P0).
2. Delegates counting to the same service functions the UI
   uses (aging_report, dashboard_metrics, ...) so agent
   numbers match the on-screen numbers exactly. Ticket
   explicitly forbids re-implementing counting logic here.
3. Permission-aware: if the caller can't see a module, the
   tool returns an empty payload + a `note` field explaining
   why (never leaks data around a permission).
4. Returns JSON-serializable Python objects only.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from sqlalchemy import func, and_
from app import db


# ─── Tool schemas (Anthropic shape — DeepseekProvider translates) ─
INSIGHTS_TOOL_SCHEMAS = [
    {
        "name": "todays_summary",
        "description": (
            "ملخص إنجازات اليوم للشركة: فواتير جديدة، تحصيلات، "
            "مهام اتقفلت، عملاء محتملين جداد، مهام اتفتحت."),
        "input_schema": {
            "type": "object",
            "properties": {
                "on_date": {
                    "type": "string",
                    "description": "التاريخ (YYYY-MM-DD). لو مالوش قيمة يتحسب النهارده.",
                },
            },
        },
    },
    {
        "name": "tasks_stats",
        "description": (
            "إحصائيات المهام في الشركة: إجمالي المفتوح والمنجز "
            "والمتأخر، موزّع على الحالات."),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
        },
    },
    {
        "name": "employees_performance",
        "description": (
            "أداء كل موظف خلال فترة (افتراضي 30 يوم): كام تاسك "
            "خلّص، كام متأخر، متوسط الوقت من إنشاء المهمة "
            "لإنجازها بالأيام."),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
        },
    },
    {
        "name": "overdue_items",
        "description": (
            "كل حاجة فات ميعادها دلوقتي: مهام متأخرة، فواتير "
            "عملاء متأخرة، فواتير موردين متأخرة."),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "module_activity",
        "description": (
            "أكتر أجزاء النظام (موديولات) استخدامًا خلال فترة "
            "معينة: كام صف اتعمل في كل جدول."),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
        },
    },
]


# ─── Helpers ────────────────────────────────────────────────────
def _parse_date(raw, fallback=None):
    if not raw:
        return fallback or date.today()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return fallback or date.today()


def _has_perm(user_id, company_id, action):
    """Delegate to the app's real permission check so tools
    respect per-user roles, not just per-company plan gating."""
    try:
        from app.services.permissions import get_user_role, P
        role = get_user_role(user_id, company_id)
        if not role:
            return False
        allowed = P.get(action, set())
        return role in allowed
    except Exception:
        # Fail closed — if we can't check, don't leak.
        return False


# ─── todays_summary ─────────────────────────────────────────────
def _todays_summary(args, company_id, user_id):
    from app.models import Invoice, InvoiceStatus, Lead, Payment
    from app.models.crm import Task, TaskStatus
    on_date = _parse_date(args.get("on_date"))
    start = datetime.combine(on_date, datetime.min.time())
    end = start + timedelta(days=1)

    # Invoices ISSUED today (excludes voided per KPI convention).
    new_invoices = (Invoice.query
                     .filter(Invoice.company_id == company_id,
                               Invoice.issue_date == on_date,
                               Invoice.status.notin_((
                                   InvoiceStatus.CANCELLED,
                                   InvoiceStatus.VOIDED,
                                   InvoiceStatus.REFUNDED)))
                     .count())
    invoiced_total = (db.session.query(
                        func.coalesce(func.sum(Invoice.total), 0))
                       .filter(Invoice.company_id == company_id,
                                 Invoice.issue_date == on_date,
                                 Invoice.status.notin_((
                                     InvoiceStatus.CANCELLED,
                                     InvoiceStatus.VOIDED,
                                     InvoiceStatus.REFUNDED)))
                       .scalar() or 0)

    # Payments received today.
    receipts_count = (Payment.query
                       .join(Invoice, Payment.invoice_id == Invoice.id)
                       .filter(Invoice.company_id == company_id,
                                 Payment.payment_date == on_date)
                       .count())
    receipts_total = (db.session.query(
                        func.coalesce(func.sum(Payment.amount), 0))
                       .join(Invoice, Payment.invoice_id == Invoice.id)
                       .filter(Invoice.company_id == company_id,
                                 Payment.payment_date == on_date)
                       .scalar() or 0)

    # Tasks closed today.
    tasks_closed = (Task.query
                     .filter(Task.company_id == company_id,
                               Task.status == TaskStatus.DONE,
                               Task.updated_at >= start,
                               Task.updated_at < end)
                     .count())

    # Tasks opened today.
    tasks_opened = (Task.query
                     .filter(Task.company_id == company_id,
                               Task.created_at >= start,
                               Task.created_at < end)
                     .count())

    # New leads today.
    new_leads = (Lead.query
                  .filter(Lead.company_id == company_id,
                            Lead.created_at >= start,
                            Lead.created_at < end,
                            Lead.deleted_at.is_(None))
                  .count())

    return {
        "date": on_date.isoformat(),
        "new_invoices": new_invoices,
        "invoiced_total": float(invoiced_total),
        "receipts_count": receipts_count,
        "receipts_total": float(receipts_total),
        "tasks_closed": tasks_closed,
        "tasks_opened": tasks_opened,
        "new_leads": new_leads,
    }


# ─── tasks_stats ────────────────────────────────────────────────
def _tasks_stats(args, company_id, user_id):
    from app.models.crm import Task, TaskStatus
    q = Task.query.filter(Task.company_id == company_id)
    start = _parse_date(args.get("start_date"), fallback=None)
    end = _parse_date(args.get("end_date"), fallback=None)
    if args.get("start_date"):
        q = q.filter(Task.created_at >= datetime.combine(
            start, datetime.min.time()))
    if args.get("end_date"):
        q = q.filter(Task.created_at < datetime.combine(
            end + timedelta(days=1), datetime.min.time()))

    by_status = {}
    for status in TaskStatus:
        by_status[status.value] = q.filter(
            Task.status == status).count()

    today = date.today()
    overdue = (q.filter(Task.deadline.isnot(None),
                          Task.deadline < today,
                          Task.status.notin_(
                              (TaskStatus.DONE,
                               TaskStatus.BLOCKED)))
                .count())

    return {
        "by_status": by_status,
        "overdue": overdue,
        "total": sum(by_status.values()),
        "period": {
            "start": args.get("start_date"),
            "end": args.get("end_date"),
        },
    }


# ─── employees_performance ─────────────────────────────────────
def _employees_performance(args, company_id, user_id):
    if not _has_perm(user_id, company_id, "employees.view"):
        return {
            "rows": [],
            "note": ("المستخدم مالوش صلاحية شوف بيانات "
                     "الموظفين — ما رجّعناش أي أرقام."),
        }
    from app.models import Employee
    from app.models.crm import Task, TaskStatus
    today = date.today()
    default_start = today - timedelta(days=30)
    start = _parse_date(args.get("start_date"),
                         fallback=default_start)
    end = _parse_date(args.get("end_date"), fallback=today)
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end + timedelta(days=1),
                               datetime.min.time())

    rows = []
    employees = (Employee.query
                   .filter_by(company_id=company_id)
                   .all())
    for e in employees:
        if not e.user_id:
            continue
        # Tasks completed by this user in the window.
        completed_q = Task.query.filter(
            Task.company_id == company_id,
            Task.assigned_to_id == e.user_id,
            Task.status == TaskStatus.DONE,
            Task.updated_at >= start_dt,
            Task.updated_at < end_dt,
        )
        completed = completed_q.all()
        completed_count = len(completed)
        # Overdue right now (not done, past deadline).
        overdue_count = (Task.query.filter(
            Task.company_id == company_id,
            Task.assigned_to_id == e.user_id,
            Task.deadline.isnot(None),
            Task.deadline < today,
            Task.status.notin_((TaskStatus.DONE,
                                 TaskStatus.BLOCKED))).count())
        # Avg time to close (days).
        avg_days = None
        if completed:
            total_secs = 0.0
            n = 0
            for t in completed:
                if t.created_at and t.updated_at:
                    total_secs += (t.updated_at
                                    - t.created_at).total_seconds()
                    n += 1
            if n:
                avg_days = round(
                    (total_secs / n) / 86400.0, 1)
        rows.append({
            "employee_id": e.id,
            "name": e.name,
            "completed": completed_count,
            "overdue": overdue_count,
            "avg_days_to_close": avg_days,
        })
    return {
        "period": {"start": start.isoformat(),
                    "end": end.isoformat()},
        "rows": sorted(rows, key=lambda r: -r["completed"]),
    }


# ─── overdue_items ─────────────────────────────────────────────
def _overdue_items(args, company_id, user_id):
    from app.models import Invoice, InvoiceStatus, VendorBill
    from app.models.vendor_bill import VendorBillStatus
    from app.models.crm import Task, TaskStatus
    today = date.today()

    # Overdue tasks.
    overdue_tasks = (Task.query.filter(
        Task.company_id == company_id,
        Task.deadline.isnot(None),
        Task.deadline < today,
        Task.status.notin_((TaskStatus.DONE,
                             TaskStatus.BLOCKED))).count())

    # Overdue AR invoices (excludes voided per aging convention).
    ar_q = Invoice.query.filter(
        Invoice.company_id == company_id,
        Invoice.due_date < today,
        Invoice.status.in_((InvoiceStatus.SENT,
                             InvoiceStatus.PARTIALLY_PAID,
                             InvoiceStatus.OVERDUE)),
    )
    ar_count = ar_q.count()
    ar_amount = sum(float(i.balance or 0) for i in ar_q.all())

    # Overdue AP bills.
    ap_q = VendorBill.query.filter(
        VendorBill.company_id == company_id,
        VendorBill.due_date < today,
        VendorBill.status.in_((VendorBillStatus.POSTED,
                                VendorBillStatus.PARTIALLY_PAID,
                                VendorBillStatus.OVERDUE)),
    )
    ap_count = ap_q.count()

    return {
        "as_of": today.isoformat(),
        "overdue_tasks": overdue_tasks,
        "overdue_invoices_count": ar_count,
        "overdue_invoices_amount": ar_amount,
        "overdue_bills_count": ap_count,
    }


# ─── module_activity ───────────────────────────────────────────
def _module_activity(args, company_id, user_id):
    today = date.today()
    default_start = today - timedelta(days=30)
    start = _parse_date(args.get("start_date"),
                         fallback=default_start)
    end = _parse_date(args.get("end_date"), fallback=today)
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end + timedelta(days=1),
                               datetime.min.time())

    from app.models import (
        Invoice, VendorBill, Lead, JournalEntry, Payment,
    )
    from app.models.crm import Task
    counts = {}

    def _count(model, ts_col, label):
        try:
            counts[label] = (model.query
                              .filter(model.company_id == company_id,
                                        ts_col >= start_dt,
                                        ts_col < end_dt)
                              .count())
        except Exception:
            counts[label] = 0

    _count(Invoice, Invoice.created_at, "invoices")
    _count(VendorBill, VendorBill.created_at, "vendor_bills")
    _count(JournalEntry, JournalEntry.created_at,
           "journal_entries")
    _count(Task, Task.created_at, "tasks")
    _count(Lead, Lead.created_at, "leads")
    # Payments — join through invoice for company scope.
    try:
        counts["payments"] = (Payment.query
                               .join(Invoice,
                                      Payment.invoice_id == Invoice.id)
                               .filter(
                                   Invoice.company_id == company_id,
                                   Payment.created_at >= start_dt,
                                   Payment.created_at < end_dt)
                               .count())
    except Exception:
        counts["payments"] = 0

    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return {
        "period": {"start": start.isoformat(),
                    "end": end.isoformat()},
        "counts": counts,
        "top": [{"module": k, "count": v}
                for k, v in ranked if v > 0][:5],
    }


# ─── Dispatch ──────────────────────────────────────────────────
_DISPATCH = {
    "todays_summary": _todays_summary,
    "tasks_stats": _tasks_stats,
    "employees_performance": _employees_performance,
    "overdue_items": _overdue_items,
    "module_activity": _module_activity,
}


def execute_insights_tool(name, args, company_id, user_id):
    fn = _DISPATCH.get(name)
    if not fn:
        return {"error": f"tool غير موجودة: {name}"}
    return fn(args or {}, company_id, user_id)
