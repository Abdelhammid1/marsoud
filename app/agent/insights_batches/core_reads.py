"""MARSOUD-INSIGHTS-AGENT-PROFESSIONAL (2026-08-06) — the 5 original
insights tools, ported to the registry.

Each one gained "return individual details next to the summary" per
the ticket:

  · overdue_items now returns WHO owns each overdue task/invoice/
    bill, not just counts (the ticket's headline example).
  · todays_summary appends `top_new_invoices` + `top_new_leads`.
  · tasks_stats appends `top_overdue` (up to 10 rows).
  · employees_performance keeps every row + adds `tasks_by_status`
    and `on_time_rate` (both were already computable, just dropped).
  · module_activity's `top[]` now carries row identifiers, not just
    the module label + count.

The gate on `_employees_performance` was `employees.view`; kept.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from sqlalchemy import func
from app import db
from app.agent.insights_catalog import (
    register, has_perm, perm_denied, parse_date,
)


# ─── todays_summary ─────────────────────────────────────────────
@register(
    name="todays_summary",
    description=(
        "ملخص إنجازات اليوم للشركة: فواتير جديدة (مع أعلى 5 قيمة)، "
        "تحصيلات، مهام اتقفلت أو اتفتحت، عملاء محتملين جداد (بأسماء "
        "أعلى 5). لأي تاريخ (default = النهارده)."),
    input_schema={
        "type": "object",
        "properties": {
            "on_date": {
                "type": "string",
                "description": "التاريخ (YYYY-MM-DD). لو مالوش قيمة يتحسب النهارده.",
            },
        },
    },
    permission=None,
)
def todays_summary(args, company_id, user_id):
    from app.models import Invoice, InvoiceStatus, Lead, Payment
    from app.models.crm import Task, TaskStatus
    on_date = parse_date(args.get("on_date"))
    start = datetime.combine(on_date, datetime.min.time())
    end = start + timedelta(days=1)

    _EXCLUDED = (InvoiceStatus.CANCELLED, InvoiceStatus.VOIDED,
                 InvoiceStatus.REFUNDED)
    inv_rows = (Invoice.query
                .filter(Invoice.company_id == company_id,
                        Invoice.issue_date == on_date,
                        Invoice.status.notin_(_EXCLUDED))
                .order_by(Invoice.total.desc()).all())
    invoiced_total = float(sum((r.total or 0) for r in inv_rows))
    top_new_invoices = [
        {"id": r.id, "number": r.number,
         "customer_id": r.customer_id,
         "customer_name": (r.customer.name if r.customer else None),
         "total": float(r.total or 0)}
        for r in inv_rows[:5]
    ]

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

    tasks_closed = (Task.query
                    .filter(Task.company_id == company_id,
                            Task.status == TaskStatus.DONE,
                            Task.updated_at >= start,
                            Task.updated_at < end).count())
    tasks_opened = (Task.query
                    .filter(Task.company_id == company_id,
                            Task.created_at >= start,
                            Task.created_at < end).count())

    lead_rows = (Lead.query
                 .filter(Lead.company_id == company_id,
                         Lead.created_at >= start,
                         Lead.created_at < end,
                         Lead.deleted_at.is_(None))
                 .order_by(Lead.created_at.desc()).all())
    top_new_leads = [
        {"id": l.id, "name": l.client_name,
         "service_needed": l.service_needed,
         "assigned_to_id": l.assigned_to_id}
        for l in lead_rows[:5]
    ]

    return {
        "date": on_date.isoformat(),
        "new_invoices": len(inv_rows),
        "invoiced_total": invoiced_total,
        "top_new_invoices": top_new_invoices,
        "receipts_count": receipts_count,
        "receipts_total": float(receipts_total),
        "tasks_closed": tasks_closed,
        "tasks_opened": tasks_opened,
        "new_leads": len(lead_rows),
        "top_new_leads": top_new_leads,
    }


# ─── tasks_stats ────────────────────────────────────────────────
@register(
    name="tasks_stats",
    description=(
        "إحصائيات المهام في الشركة موزّعة على الحالات، بالإضافة "
        "لأعلى 10 مهام متأخرة بأسماء المكلَّفين."),
    input_schema={
        "type": "object",
        "properties": {
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
        },
    },
    permission=None,
)
def tasks_stats(args, company_id, user_id):
    from app.models.crm import Task, TaskStatus
    from app.models import User
    q = Task.query.filter(Task.company_id == company_id)
    start = parse_date(args.get("start_date"), fallback=None)
    end = parse_date(args.get("end_date"), fallback=None)
    if args.get("start_date"):
        q = q.filter(Task.created_at >= datetime.combine(
            start, datetime.min.time()))
    if args.get("end_date"):
        q = q.filter(Task.created_at < datetime.combine(
            end + timedelta(days=1), datetime.min.time()))

    by_status = {s.value: q.filter(Task.status == s).count()
                 for s in TaskStatus}

    today = date.today()
    overdue_q = q.filter(
        Task.deadline.isnot(None),
        Task.deadline < today,
        Task.status.notin_((TaskStatus.DONE, TaskStatus.BLOCKED)),
    ).order_by(Task.deadline.asc())
    overdue_all = overdue_q.all()
    top_overdue = []
    for t in overdue_all[:10]:
        owner = (db.session.get(User, t.assigned_to_id)
                 if t.assigned_to_id else None)
        top_overdue.append({
            "id": t.id, "title": t.title,
            "deadline": t.deadline.isoformat() if t.deadline else None,
            "days_late": (today - t.deadline).days if t.deadline else None,
            "assigned_to_id": t.assigned_to_id,
            "assigned_to_name": owner.full_name if owner else None,
            "status": t.status.value,
        })

    return {
        "by_status": by_status,
        "overdue": len(overdue_all),
        "top_overdue": top_overdue,
        "total": sum(by_status.values()),
        "period": {
            "start": args.get("start_date"),
            "end": args.get("end_date"),
        },
    }


# ─── employees_performance ─────────────────────────────────────
@register(
    name="employees_performance",
    description=(
        "أداء كل موظف خلال فترة (افتراضي 30 يوم): مهام مُنجزة، "
        "متأخرة، متوسط وقت الإنجاز، توزيع المهام على الحالات، "
        "on_time_rate."),
    input_schema={
        "type": "object",
        "properties": {
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
        },
    },
    permission="employees.view",
)
def employees_performance(args, company_id, user_id):
    if not has_perm(user_id, company_id, "employees.view"):
        return perm_denied("employees.view")
    from app.models import Employee
    from app.models.crm import Task, TaskStatus
    today = date.today()
    default_start = today - timedelta(days=30)
    start = parse_date(args.get("start_date"), fallback=default_start)
    end = parse_date(args.get("end_date"), fallback=today)
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end + timedelta(days=1),
                              datetime.min.time())

    rows = []
    employees = Employee.query.filter_by(company_id=company_id).all()
    for e in employees:
        if not e.user_id:
            continue
        # Tasks completed by this user in the window.
        completed = Task.query.filter(
            Task.company_id == company_id,
            Task.assigned_to_id == e.user_id,
            Task.status == TaskStatus.DONE,
            Task.updated_at >= start_dt,
            Task.updated_at < end_dt,
        ).all()
        completed_count = len(completed)
        # Overdue right now.
        overdue_count = Task.query.filter(
            Task.company_id == company_id,
            Task.assigned_to_id == e.user_id,
            Task.deadline.isnot(None),
            Task.deadline < today,
            Task.status.notin_((TaskStatus.DONE, TaskStatus.BLOCKED))
        ).count()
        # By-status snapshot right now.
        tasks_by_status = {}
        for s in TaskStatus:
            tasks_by_status[s.value] = Task.query.filter(
                Task.company_id == company_id,
                Task.assigned_to_id == e.user_id,
                Task.status == s,
            ).count()
        # Avg time to close (days) + on-time rate.
        avg_days = None
        on_time = 0
        eligible = 0
        if completed:
            secs = 0.0
            n = 0
            for t in completed:
                if t.created_at and t.updated_at:
                    secs += (t.updated_at - t.created_at).total_seconds()
                    n += 1
                if t.deadline:
                    eligible += 1
                    closed_on = (t.updated_at.date()
                                 if t.updated_at else today)
                    if closed_on <= t.deadline:
                        on_time += 1
            if n:
                avg_days = round(secs / n / 86400.0, 1)
        on_time_rate = (round(on_time / eligible * 100, 1)
                        if eligible else None)

        rows.append({
            "employee_id": e.id,
            "user_id": e.user_id,
            "name": e.name,
            "completed": completed_count,
            "overdue": overdue_count,
            "avg_days_to_close": avg_days,
            "tasks_by_status": tasks_by_status,
            "on_time_rate": on_time_rate,
        })
    return {
        "period": {"start": start.isoformat(),
                   "end": end.isoformat()},
        "rows": sorted(rows, key=lambda r: -r["completed"]),
    }


# ─── overdue_items ─────────────────────────────────────────────
@register(
    name="overdue_items",
    description=(
        "كل حاجة فات ميعادها دلوقتي مع تفاصيل كل عنصر: مهام متأخرة "
        "بأسماء المكلَّفين، فواتير عملاء متأخرة بأسماء العملاء، "
        "فواتير موردين متأخرة. المحلل يقدر يقول بالضبط مين المسؤول "
        "عن كل متأخر بدل ما يقول 'عندك 12 متأخر'."),
    input_schema={
        "type": "object",
        "properties": {},
    },
    permission=None,
)
def overdue_items(args, company_id, user_id):
    """MARSOUD-INSIGHTS-AGENT-PROFESSIONAL — the ticket's headline
    example. Was returning counts only ("12 overdue tasks"); now
    returns row-level detail so the analyst can name the owner."""
    from app.models import Invoice, InvoiceStatus, VendorBill, User
    from app.models.vendor_bill import VendorBillStatus
    from app.models.crm import Task, TaskStatus
    today = date.today()

    task_rows = Task.query.filter(
        Task.company_id == company_id,
        Task.deadline.isnot(None),
        Task.deadline < today,
        Task.status.notin_((TaskStatus.DONE, TaskStatus.BLOCKED))
    ).order_by(Task.deadline.asc()).all()
    tasks_out = []
    for t in task_rows:
        owner = (db.session.get(User, t.assigned_to_id)
                 if t.assigned_to_id else None)
        tasks_out.append({
            "id": t.id, "title": t.title,
            "deadline": t.deadline.isoformat(),
            "days_late": (today - t.deadline).days,
            "assigned_to_id": t.assigned_to_id,
            "assigned_to_name": owner.full_name if owner else None,
            "status": t.status.value,
        })

    ar_rows = Invoice.query.filter(
        Invoice.company_id == company_id,
        Invoice.due_date < today,
        Invoice.status.in_((InvoiceStatus.SENT,
                            InvoiceStatus.PARTIALLY_PAID,
                            InvoiceStatus.OVERDUE)),
    ).order_by(Invoice.due_date.asc()).all()
    invoices_out = []
    ar_total = 0.0
    for i in ar_rows:
        bal = float(i.balance or 0)
        ar_total += bal
        invoices_out.append({
            "id": i.id, "number": i.number,
            "customer_id": i.customer_id,
            "customer_name": (i.customer.name if i.customer else None),
            "due_date": i.due_date.isoformat() if i.due_date else None,
            "days_late": ((today - i.due_date).days
                          if i.due_date else None),
            "balance": bal,
        })

    ap_rows = VendorBill.query.filter(
        VendorBill.company_id == company_id,
        VendorBill.due_date < today,
        VendorBill.status.in_((VendorBillStatus.POSTED,
                               VendorBillStatus.PARTIALLY_PAID,
                               VendorBillStatus.OVERDUE)),
    ).order_by(VendorBill.due_date.asc()).all()
    bills_out = []
    ap_total = 0.0
    for b in ap_rows:
        bal = float(b.balance or 0) if hasattr(b, "balance") else 0.0
        ap_total += bal
        bills_out.append({
            "id": b.id,
            "number": getattr(b, "number", None),
            "vendor_id": getattr(b, "vendor_id", None),
            "vendor_name": (b.vendor.name
                            if getattr(b, "vendor", None) else None),
            "due_date": (b.due_date.isoformat()
                         if b.due_date else None),
            "days_late": ((today - b.due_date).days
                          if b.due_date else None),
            "balance": bal,
        })

    return {
        "as_of": today.isoformat(),
        "tasks": tasks_out,
        "invoices": invoices_out,
        "bills": bills_out,
        "totals": {
            "overdue_tasks": len(tasks_out),
            "overdue_invoices_count": len(invoices_out),
            "overdue_invoices_amount": round(ar_total, 2),
            "overdue_bills_count": len(bills_out),
            "overdue_bills_amount": round(ap_total, 2),
        },
    }


# ─── module_activity ───────────────────────────────────────────
@register(
    name="module_activity",
    description=(
        "أكتر أجزاء النظام استخدامًا خلال فترة معينة: عدد الصفوف "
        "الجديدة في كل موديول + قائمة top مع row ids."),
    input_schema={
        "type": "object",
        "properties": {
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
        },
    },
    permission=None,
)
def module_activity(args, company_id, user_id):
    from app.models import Invoice, VendorBill, Lead, JournalEntry, Payment
    from app.models.crm import Task
    today = date.today()
    default_start = today - timedelta(days=30)
    start = parse_date(args.get("start_date"), fallback=default_start)
    end = parse_date(args.get("end_date"), fallback=today)
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end + timedelta(days=1),
                              datetime.min.time())

    counts = {}
    tops: dict = {}

    def _count_and_top(model, ts_col, label, id_attr="id",
                       label_attr=None):
        try:
            q = (model.query
                 .filter(model.company_id == company_id,
                         ts_col >= start_dt, ts_col < end_dt))
            rows = q.order_by(ts_col.desc()).limit(5).all()
            counts[label] = q.count()
            tops[label] = [
                {"id": getattr(r, id_attr),
                 "label": (getattr(r, label_attr, None)
                           if label_attr else None)}
                for r in rows
            ]
        except Exception:
            counts[label] = 0
            tops[label] = []

    _count_and_top(Invoice, Invoice.created_at, "invoices",
                   label_attr="number")
    _count_and_top(VendorBill, VendorBill.created_at, "vendor_bills",
                   label_attr="number")
    _count_and_top(JournalEntry, JournalEntry.created_at,
                   "journal_entries", label_attr="number")
    _count_and_top(Task, Task.created_at, "tasks",
                   label_attr="title")
    _count_and_top(Lead, Lead.created_at, "leads",
                   label_attr="client_name")

    try:
        pay_q = (Payment.query
                 .join(Invoice, Payment.invoice_id == Invoice.id)
                 .filter(Invoice.company_id == company_id,
                         Payment.created_at >= start_dt,
                         Payment.created_at < end_dt))
        counts["payments"] = pay_q.count()
        pay_rows = pay_q.order_by(Payment.created_at.desc()).limit(5).all()
        tops["payments"] = [
            {"id": p.id, "label": f"{float(p.amount or 0):.2f}"}
            for p in pay_rows
        ]
    except Exception:
        counts["payments"] = 0
        tops["payments"] = []

    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    top = [{"module": k, "count": v, "sample": tops.get(k, [])}
           for k, v in ranked if v > 0][:5]
    return {
        "period": {"start": start.isoformat(),
                   "end": end.isoformat()},
        "counts": counts,
        "top": top,
    }
