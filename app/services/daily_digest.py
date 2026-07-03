"""MARSOUD-EMPLOYEE-DAILY-REPORTS — build the daily digest.

Aggregates activity across four existing tables into a single readable
Arabic block, without changing any of the recording paths:

  user_activity_log   → invoices/journals/bills/payroll the user created
  task_activity_logs  → tasks assigned or status-changed
  lead_status_events  → leads whose stage the user moved
  lead_activities     → calls/emails/meetings/notes the user logged

Public entrypoints:
  build_digest(company_id, employee_id, day)
      Idempotent. Creates a DRAFT `EmployeeDailyReport` if the employee
      has ANY activity that day, or returns the existing draft/submitted
      row if one exists. Never mutates a SUBMITTED report.

  run_daily_digest_for_company(company_id, day=None)
      Fan-out helper called by cron. Runs `build_digest` for every
      active employee whose email is linked to a User account.
"""
from datetime import datetime, date, time, timedelta
import json

from app import db
from app.models import (
    Employee, EmployeeStatus, User, UserActivityLog,
    EmployeeDailyReport, DailyReportStatus, EmployeeReportAccess,
)
from app.models.crm import LeadStatusEvent, TaskActivityLog
from app.models.crm_expansion import LeadActivity


# ─── Aggregation helpers (all four sources) ────────────────────────────
def _fetch_user_activity(company_id, user_id, day):
    start = datetime.combine(day, time.min)
    end = datetime.combine(day + timedelta(days=1), time.min)
    return UserActivityLog.query.filter(
        UserActivityLog.company_id == company_id,
        UserActivityLog.user_id == user_id,
        UserActivityLog.created_at >= start,
        UserActivityLog.created_at < end,
        UserActivityLog.action_type == "CREATE",
    ).order_by(UserActivityLog.created_at.asc()).all()


def _fetch_task_activity(company_id, user_id, day):
    start = datetime.combine(day, time.min)
    end = datetime.combine(day + timedelta(days=1), time.min)
    return TaskActivityLog.query.filter(
        TaskActivityLog.company_id == company_id,
        TaskActivityLog.user_id == user_id,
        TaskActivityLog.created_at >= start,
        TaskActivityLog.created_at < end,
    ).order_by(TaskActivityLog.created_at.asc()).all()


def _fetch_lead_status_events(user_id, day):
    start = datetime.combine(day, time.min)
    end = datetime.combine(day + timedelta(days=1), time.min)
    return LeadStatusEvent.query.filter(
        LeadStatusEvent.changed_by_id == user_id,
        LeadStatusEvent.created_at >= start,
        LeadStatusEvent.created_at < end,
    ).order_by(LeadStatusEvent.created_at.asc()).all()


def _fetch_lead_activities(company_id, user_id, day):
    start = datetime.combine(day, time.min)
    end = datetime.combine(day + timedelta(days=1), time.min)
    return LeadActivity.query.filter(
        LeadActivity.company_id == company_id,
        LeadActivity.created_by_id == user_id,
        LeadActivity.created_at >= start,
        LeadActivity.created_at < end,
    ).order_by(LeadActivity.created_at.asc()).all()


# ─── Human-readable summarisation ──────────────────────────────────────
_ENTITY_AR = {
    "invoice": "فاتورة عميل",
    "vendor_bill": "فاتورة مورد",
    "journal": "قيد يومية",
    "payroll": "كشف رواتب",
    "customer": "عميل جديد",
    "vendor": "مورد جديد",
    "product": "منتج جديد",
    "asset": "أصل ثابت",
    "vendor_bill_payment": "دفعة لمورد",
    "vendor_bill_refund": "مرتجع مشتريات",
    "refund": "مرتجع مبيعات",
    "credit_note": "إشعار دائن",
}

_LEAD_ACTIVITY_TYPES_AR = {
    "CALL": "مكالمة",
    "EMAIL": "إيميل",
    "MEETING": "اجتماع",
    "NOTE": "ملاحظة",
    "WHATSAPP": "واتساب",
    "SMS": "رسالة",
}

# Asmaa 2026-07-03 — she saw "STATUS_CHANGED ← مهمة #61" and couldn't
# make sense of any of it. Translate every raw action code + resolve
# every #id to a human name below.
_TASK_ACTION_AR = {
    "CREATED":            "أنشأت مهمة جديدة",
    "STATUS_CHANGED":     "غيّرت حالة المهمة",
    "COMMENT_ADDED":      "أضفت تعليق",
    "ASSIGNEES_CHANGED":  "غيّرت المسؤولين عن المهمة",
    "PRIORITY_CHANGED":   "غيّرت أولوية المهمة",
    "DEADLINE_CHANGED":   "غيّرت الموعد النهائي",
    "TITLE_CHANGED":      "عدّلت عنوان المهمة",
    "DESCRIPTION_CHANGED": "عدّلت وصف المهمة",
    "ARCHIVED":           "أرشفت المهمة",
    "UNARCHIVED":         "استعادت المهمة من الأرشيف",
    "DELETED":            "حذفت المهمة",
}


def _bullet(sec, items):
    if not items:
        return ""
    lines = [f"**{sec}** ({len(items)})"]
    for line in items:
        lines.append(f"  • {line}")
    return "\n".join(lines)


def _task_title(task_id):
    """Cheap lookup for the task title so the digest doesn't say '#61'
    to a person who has no idea what task 61 is."""
    from app.models import Task
    t = db.session.get(Task, task_id)
    if not t:
        return f"مهمة #{task_id}"
    # Keep it short so long titles don't blow the bullet layout.
    title = (t.title or "").strip()
    if len(title) > 60:
        title = title[:60] + "…"
    return title or f"مهمة #{task_id}"


def _lead_name(lead_id):
    from app.models import Lead
    L = db.session.get(Lead, lead_id)
    if not L:
        return f"ليد #{lead_id}"
    return (L.client_name or "").strip() or f"ليد #{lead_id}"


def _task_after_status_done(log):
    """True if a STATUS_CHANGED log moved the task to DONE at the
    moment it was written. Reads after_json (written by log_activity
    in tasks_extras.py) as JSON and checks .status. Silent False on
    malformed rows so a bad log never crashes the digest."""
    if (log.action or "") != "STATUS_CHANGED":
        return False
    try:
        import json as _json
        after = _json.loads(log.after_json or "{}")
    except (ValueError, TypeError):
        return False
    return (after.get("status") or "").upper() == "DONE"


def _summarise(user_logs, task_logs, lead_events, lead_acts):
    """Turn the four raw lists into an Arabic block a normal user can
    actually read. Abdelhamid ticket 2026-07-03 §1–7 all implemented:

      §1 — every raw action code translated
      §2 — every #id resolved to a name/title
      §3 — empty subject/body rendered clean (no dash lines)
      §4 — sorted chronologically WITHIN each section
      §5 — task section split into "خلّصها" vs "لسه شغال عليها"
      §6 — LeadActivityType icons used instead of repeating type name
      §7 — summary line at the top of each section

    Data source is NOT touched — this is display-only formatting.
    Event count in = event count out (verified in the audit).
    """
    from collections import defaultdict
    sections = []

    # ─── §7 helper: build a "**title** (count …)" header line. ──────
    def _header(title, count, sub=""):
        core = f"**{title}** ({count})"
        return f"{core} — {sub}" if sub else core

    # ─── User activity (invoices, journals, ...) ────────────────────
    if user_logs:
        entries = []
        for log in sorted(user_logs, key=lambda x: x.created_at or 0):
            kind_ar = _ENTITY_AR.get(log.entity_type, log.entity_type or "؟")
            label = (log.entity_label or "").strip() or f"#{log.entity_id}"
            entries.append(f"{kind_ar}: {label}")
        sections.append(
            _header("إنشاء وتعديلات في النظام", len(user_logs))
            + "\n" + "\n".join(f"  • {e}" for e in entries)
        )

    # ─── Tasks: split into closed vs still-open. ────────────────────
    if task_logs:
        # Group every log by task_id, chronologically. §4.
        by_task = defaultdict(list)
        for log in sorted(task_logs, key=lambda x: x.created_at or 0):
            by_task[log.task_id].append(log)

        # A task is "closed today" iff any STATUS_CHANGED log in its
        # log-stream ended at DONE. Reading after_json guarantees we
        # answer the historical question ("did they close it during
        # the report window?") not the current-state question.
        closed_ids = {
            tid for tid, logs in by_task.items()
            if any(_task_after_status_done(l) for l in logs)
        }
        open_ids = set(by_task) - closed_ids

        n_total = len(by_task)
        n_closed = len(closed_ids)
        n_open = n_total - n_closed

        lines = [_header(
            "المهام", n_total,
            f"{n_closed} خلصوا، {n_open} لسه شغالين",
        )]

        # Order buckets by earliest activity so the timeline still
        # reads top→bottom in time.
        def _first_ts(tid):
            return by_task[tid][0].created_at or 0

        if closed_ids:
            lines.append("  ✅ **خلّصها:**")
            for tid in sorted(closed_ids, key=_first_ts):
                actions = "، ".join(
                    _TASK_ACTION_AR.get(l.action, l.action or "تعديل")
                    for l in by_task[tid]
                )
                lines.append(f"    • **{_task_title(tid)}** — {actions}")
        if open_ids:
            lines.append("  🔄 **لسه شغال عليها:**")
            for tid in sorted(open_ids, key=_first_ts):
                actions = "، ".join(
                    _TASK_ACTION_AR.get(l.action, l.action or "تعديل")
                    for l in by_task[tid]
                )
                lines.append(f"    • **{_task_title(tid)}** — {actions}")

        sections.append("\n".join(lines))

    # ─── Lead status stage changes ──────────────────────────────────
    if lead_events:
        entries = []
        for ev in sorted(lead_events, key=lambda e: e.created_at or 0):
            frm = ev.from_status.label_ar if ev.from_status else "—"
            to = ev.to_status.label_ar if ev.to_status else "—"
            entries.append(
                f"{_lead_name(ev.lead_id)}: {frm} ← {to}"
            )
        sections.append(
            _header("مراحل العملاء المحتملين", len(lead_events))
            + "\n" + "\n".join(f"  • {e}" for e in entries)
        )

    # ─── Lead activity touchpoints (calls, meetings, notes) ──────────
    if lead_acts:
        # §7 summary: count by type + emit compact "📞 مكالمة×2 + 🤝 اجتماع×1".
        type_count = defaultdict(int)
        for act in lead_acts:
            key = act.type.value if hasattr(act.type, "value") else str(act.type)
            type_count[key] += 1
        # LeadActivityType enum has both .label_ar and .icon; use them
        # so the summary line matches the bullet icons visually.
        from app.models.crm_expansion import LeadActivityType as _LAT
        summary_bits = []
        for tkey, n in sorted(type_count.items(), key=lambda kv: -kv[1]):
            try:
                enum_val = _LAT(tkey)
                summary_bits.append(f"{enum_val.icon} {enum_val.label_ar} × {n}")
            except ValueError:
                summary_bits.append(f"{tkey} × {n}")

        entries = []
        for act in sorted(lead_acts, key=lambda a: a.created_at or 0):
            # Icon + label direct off the enum → §6.
            type_ar = act.type.label_ar if hasattr(act.type, "label_ar") \
                else _LEAD_ACTIVITY_TYPES_AR.get(str(act.type), str(act.type))
            icon = act.type.icon if hasattr(act.type, "icon") else "•"
            subj = (act.subject or "").strip()
            name = _lead_name(act.lead_id)
            if subj:
                entries.append(f"{icon} {type_ar}: {subj} — {name}")
            else:
                # §3 — no dash line when subject is blank.
                entries.append(f"{icon} {type_ar} مع {name}")

        sections.append(
            _header("متابعات العملاء المحتملين", len(lead_acts),
                     " + ".join(summary_bits))
            + "\n" + "\n".join(f"  • {e}" for e in entries)
        )

    return "\n\n".join(s for s in sections if s)


def _title_for(employee, day, counts):
    # Human title: "تقرير اليوم — 2026-07-02 (3 فواتير + 2 مهام)".
    parts = []
    if counts.get("user"):
        parts.append(f"{counts['user']} سجل")
    if counts.get("task"):
        parts.append(f"{counts['task']} مهمة")
    if counts.get("lead_events"):
        parts.append(f"{counts['lead_events']} ليد")
    if counts.get("lead_acts"):
        parts.append(f"{counts['lead_acts']} متابعة")
    parts_str = " + ".join(parts) if parts else "نشاط"
    return f"تقرير {day.isoformat()} — {parts_str}"


# ─── Public entrypoints ────────────────────────────────────────────────
def build_digest(company_id, employee_id, day=None):
    """Build (or return existing) the DRAFT report for one employee-day.

    Returns:
        EmployeeDailyReport, or None if the employee has no activity
        that day AND no report row already exists.

    Never overwrites a SUBMITTED report — safe to call repeatedly."""
    day = day or date.today()

    employee = db.session.get(Employee, employee_id)
    if not employee or employee.company_id != company_id:
        return None
    # Resolve the linked User to look up activity by user_id.
    user = None
    if employee.email:
        user = User.query.filter(
            db.func.lower(User.email) == employee.email.strip().lower(),
        ).first()
    if not user:
        # No linked user → no activity to aggregate. Return None so the
        # caller doesn't create an empty row.
        return None

    # Idempotence: existing row wins.
    existing = EmployeeDailyReport.query.filter_by(
        company_id=company_id, employee_id=employee.id, report_date=day,
    ).first()
    if existing:
        # Refuse to touch a SUBMITTED report — the employee already
        # signed off on it, the auto-generated body is now historical.
        if existing.status == DailyReportStatus.SUBMITTED:
            return existing
        # DRAFT: safe to refresh the body in case new activity landed
        # after the first run (edge case if cron ran mid-day).
        user_logs = _fetch_user_activity(company_id, user.id, day)
        task_logs = _fetch_task_activity(company_id, user.id, day)
        lead_events = _fetch_lead_status_events(user.id, day)
        lead_acts = _fetch_lead_activities(company_id, user.id, day)
        body = _summarise(user_logs, task_logs, lead_events, lead_acts)
        counts = {
            "user": len(user_logs), "task": len(task_logs),
            "lead_events": len(lead_events), "lead_acts": len(lead_acts),
        }
        if not any(counts.values()):
            # Nothing to report — leave the draft as-is so we don't
            # send a false "nothing done" summary.
            return existing
        existing.body = body
        existing.title = _title_for(employee, day, counts)
        db.session.flush()
        return existing

    # Fresh build.
    user_logs = _fetch_user_activity(company_id, user.id, day)
    task_logs = _fetch_task_activity(company_id, user.id, day)
    lead_events = _fetch_lead_status_events(user.id, day)
    lead_acts = _fetch_lead_activities(company_id, user.id, day)
    counts = {
        "user": len(user_logs), "task": len(task_logs),
        "lead_events": len(lead_events), "lead_acts": len(lead_acts),
    }
    if not any(counts.values()):
        # No activity — do NOT create a report row. The ticket says
        # "الموظف اللي مالوش نشاط في يوم معين مايتعملوش مسودة ليه".
        return None

    report = EmployeeDailyReport(
        company_id=company_id,
        employee_id=employee.id,
        report_date=day,
        title=_title_for(employee, day, counts),
        body=_summarise(user_logs, task_logs, lead_events, lead_acts),
        status=DailyReportStatus.DRAFT,
    )
    db.session.add(report)
    db.session.flush()

    # Best-effort notify the employee. Failure to notify must not block
    # the digest itself — the report is still there in /my/ for them.
    try:
        from app.models import Notification, NotificationKind
        if user:
            n = Notification(
                company_id=company_id,
                user_id=user.id,
                kind=NotificationKind.DIGEST_DRAFT_READY,
                title="تقرير يومي جاهز للمراجعة",
                body=(f"تقرير نشاطك ليوم {day.isoformat()} جاهز — "
                        f"راجعه، ضيف ملاحظاتك، وابعته للمالك."),
                link_url="/my/daily-reports",
            )
            db.session.add(n)
            db.session.flush()
    except Exception:
        pass

    return report


def submit_report(report_id, employee_user_id):
    """Employee-triggered: DRAFT → SUBMITTED.

    Notifies every user with permission to see this employee's reports
    (owners + explicit `employee_report_access` rows). Idempotent — a
    second submit is a no-op."""
    r = db.session.get(EmployeeDailyReport, report_id)
    if not r:
        return None
    if r.status == DailyReportStatus.SUBMITTED:
        return r
    r.status = DailyReportStatus.SUBMITTED
    r.submitted_at = datetime.utcnow()
    db.session.flush()

    try:
        from app.models import Notification, NotificationKind
        from app.models.user import user_companies
        # Owners of the company — always notified.
        owner_ids = [row[0] for row in db.session.execute(
            db.select(user_companies.c.user_id).where(
                user_companies.c.company_id == r.company_id,
                user_companies.c.role == "owner",
            ),
        ).all()]
        # Extra admins who have explicit access to THIS employee.
        extra_ids = [
            row[0] for row in db.session.query(
                EmployeeReportAccess.viewer_user_id,
            ).filter(
                EmployeeReportAccess.company_id == r.company_id,
                EmployeeReportAccess.employee_id == r.employee_id,
            ).all()
        ]
        notified = set()
        for uid in owner_ids + extra_ids:
            if uid in notified:
                continue
            notified.add(uid)
            db.session.add(Notification(
                company_id=r.company_id,
                user_id=uid,
                kind=NotificationKind.EMPLOYEE_REPORT_SUBMITTED,
                title=f"تقرير موظف جديد: {r.employee.name}",
                body=(f"تقرير يوم {r.report_date.isoformat()} "
                        f"وصلك من {r.employee.name}."),
                link_url=f"/reports/employees/{r.employee_id}/{r.id}",
            ))
        db.session.flush()
    except Exception:
        pass
    return r


def can_view_reports_for(user, employee_id, company_id):
    """Owners see everyone. Anyone else must have an explicit
    `employee_report_access` row for that employee. Returns bool."""
    from app.models.user import user_companies
    if not user or not user.is_authenticated:
        return False
    # Owner check
    is_owner = db.session.execute(
        db.select(user_companies.c.user_id).where(
            user_companies.c.user_id == user.id,
            user_companies.c.company_id == company_id,
            user_companies.c.role == "owner",
        ),
    ).first() is not None
    if is_owner:
        return True
    row = EmployeeReportAccess.query.filter_by(
        company_id=company_id, viewer_user_id=user.id,
        employee_id=employee_id,
    ).first()
    return row is not None


def visible_employees_for(user, company_id):
    """Return the list of Employee rows this user is allowed to see
    reports for."""
    from app.models.user import user_companies
    if not user or not user.is_authenticated:
        return []
    is_owner = db.session.execute(
        db.select(user_companies.c.user_id).where(
            user_companies.c.user_id == user.id,
            user_companies.c.company_id == company_id,
            user_companies.c.role == "owner",
        ),
    ).first() is not None
    if is_owner:
        return Employee.query.filter_by(company_id=company_id).all()
    employee_ids = [r.employee_id for r in EmployeeReportAccess.query.filter_by(
        company_id=company_id, viewer_user_id=user.id,
    ).all()]
    if not employee_ids:
        return []
    return Employee.query.filter(
        Employee.company_id == company_id,
        Employee.id.in_(employee_ids),
    ).all()


def run_daily_digest_for_company(company_id, day=None):
    """Fan-out: build a digest for every active employee in the
    company. Cron-callable; safe to run multiple times per day (the
    unique constraint + idempotence in build_digest guarantees no
    duplicate rows)."""
    day = day or date.today() - timedelta(days=1)  # digest = yesterday
    employees = Employee.query.filter_by(
        company_id=company_id, status=EmployeeStatus.ACTIVE,
    ).all()
    built = 0
    skipped = 0
    for e in employees:
        r = build_digest(company_id, e.id, day)
        if r:
            built += 1
        else:
            skipped += 1
    db.session.commit()
    return {"built": built, "skipped": skipped, "day": day.isoformat()}
