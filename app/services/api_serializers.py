"""MARSOUD-MOBILE-FLUTTER — shared JSON serializers.

Every mobile API blueprint (`api_v1_*.py`) imports from here so no route
file re-invents field mapping. Enums serialize as {value, label_ar} so
the mobile UI can render status chips without extra API calls.

Rules:
- All datetimes/dates as ISO strings (via `iso`).
- All Numeric columns as floats (mobile Dart doesn't distinguish Decimal).
- Enum columns as {value, label_ar} where a label exists, else {value}.
- One `_to_dict` per model; wide models expose both `_brief` and `_full`
  variants where mobile lists don't need every field.
- Never dereference a relationship implicitly — always guard with `if x`.
"""
from decimal import Decimal


# ─── Primitives ────────────────────────────────────────────────────────
def iso(dt):
    return dt.isoformat() if dt else None


def num(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    return v


def enum_of(e):
    """Serialize a python Enum value to {value, label_ar}. Falls back to
    just the value string when there's no label_ar attribute on the
    enum member (most Marsoud enums add one; some don't)."""
    if e is None:
        return None
    val = getattr(e, "value", str(e))
    label = getattr(e, "label_ar", None)
    return {"value": val, "label_ar": label or val}


# ─── User / Company (mirrors api_v1._user_brief) ───────────────────────
def user_brief(u):
    if not u:
        return None
    return {
        "id": u.id,
        "name": u.full_name,
        "email": u.email,
    }


def company_brief(c):
    if not c:
        return None
    return {
        "id": c.id,
        "name": c.name,
        "base_currency": getattr(c, "base_currency", None),
    }


# ─── Employee (HR) ─────────────────────────────────────────────────────
def employee_brief(e):
    if not e:
        return None
    return {
        "id": e.id,
        "employee_number": e.employee_number,
        "name": e.name,
        "job_title": e.job_title,
        "email": e.email,
        "phone": e.phone,
    }


def employee_full(e):
    if not e:
        return None
    base = employee_brief(e)
    base.update({
        "start_date": iso(e.start_date),
        "contract_type": enum_of(e.contract_type),
        "status": enum_of(e.status),
        "basic_salary": num(e.basic_salary),
        "allowances": num(e.allowances),
        "deductions": num(e.deductions),
        "national_id": e.national_id,
        "nationality": e.nationality,
        "date_of_birth": iso(e.date_of_birth),
        "gender": enum_of(e.gender),
        "contract_end_date": iso(e.contract_end_date),
        "department_id": e.department_id,
        "notes": e.notes,
    })
    return base


# ─── Payroll ───────────────────────────────────────────────────────────
def payroll_line_brief(line, run=None):
    if not line:
        return None
    r = run or getattr(line, "run", None)
    return {
        "id": line.id,
        "run_id": line.run_id,
        "period_year": r.period_year if r else None,
        "period_month": r.period_month if r else None,
        "basic": num(line.basic),
        "allowances": num(line.allowances),
        "overtime": num(line.overtime),
        "bonus": num(line.bonus),
        "deductions": num(line.deductions),
        "absence_deduction": num(line.absence_deduction),
        "late_deduction": num(line.late_deduction),
        "advance_deduction": num(line.advance_deduction),
        "insurance_deduction": num(line.insurance_deduction),
        "income_tax_deduction": num(line.income_tax_deduction),
        "net": num(line.net),
        "amount_paid": num(line.amount_paid),
        "payment_method": line.payment_method,
        "payment_date": iso(line.payment_date),
    }


# ─── Leave ─────────────────────────────────────────────────────────────
def leave_type_brief(lt):
    if not lt:
        return None
    return {
        "id": lt.id,
        "name": lt.name,
        "is_active": lt.is_active,
    }


def leave_balance_brief(b):
    if not b:
        return None
    return {
        "id": b.id,
        "leave_type_id": b.leave_type_id,
        "leave_type_name": b.leave_type.name if b.leave_type else None,
        "year": b.year,
        "granted": num(b.balance_days),
        "used": num(b.used_days),
        "remaining": num(b.remaining_days),
    }


def leave_request_brief(r):
    if not r:
        return None
    return {
        "id": r.id,
        "leave_type_id": r.leave_type_id,
        "leave_type_name": r.leave_type.name if r.leave_type else None,
        "start_date": iso(r.start_date),
        "end_date": iso(r.end_date),
        "days_count": num(r.days_count),
        "reason": r.reason,
        "status": enum_of(r.status),
        "created_at": iso(r.created_at),
    }


# ─── Late-permission (استئذان) ─────────────────────────────────────────
def permission_request_brief(r):
    if not r:
        return None
    return {
        "id": r.id,
        "request_date": iso(r.request_date),
        "hours_count": num(r.hours_count),
        "start_time": r.start_time.strftime("%H:%M") if r.start_time else None,
        "end_time": r.end_time.strftime("%H:%M") if r.end_time else None,
        "reason": r.reason,
        "status": enum_of(r.status),
        "created_at": iso(r.created_at),
    }


# ─── Advances ──────────────────────────────────────────────────────────
def advance_request_brief(r):
    if not r:
        return None
    return {
        "id": r.id,
        "amount": num(r.amount),
        "reason": r.reason,
        "status": enum_of(r.status),
        "created_at": iso(r.created_at),
    }


def advance_brief(a):
    """EmployeeAdvance summary (current active advance)."""
    if not a:
        return None
    return {
        "id": a.id,
        "amount": num(a.amount),
        "remaining": num(a.remaining),
        "monthly_installment": num(a.monthly_installment),
        "months": a.months,
        "paid_amount": a.paid_amount,
        "next_installment": a.next_installment,
        "disbursed_on": iso(a.disbursed_on),
        "status": enum_of(a.status),
        "source": enum_of(a.source),
        "created_at": iso(a.created_at),
    }


def advance_repayment_brief(r):
    if not r:
        return None
    return {
        "id": r.id,
        "amount": num(r.amount),
        "at": iso(getattr(r, "created_at", None)),
        "note": getattr(r, "note", None),
    }


# ─── Attendance ────────────────────────────────────────────────────────
def checkin_brief(c):
    if not c:
        return None
    return {
        "id": c.id,
        "date": iso(c.date),
        "check_in_time": iso(c.check_in_time),
        "check_out_time": iso(c.check_out_time),
        "check_in_lat": num(c.check_in_lat),
        "check_in_lng": num(c.check_in_lng),
        "check_out_lat": num(c.check_out_lat),
        "check_out_lng": num(c.check_out_lng),
        "is_open": c.is_open,
        "worked_hours": c.worked_hours,
    }


# ─── Cash custody ──────────────────────────────────────────────────────
def cash_custody_brief(c):
    if not c:
        return None
    return {
        "id": c.id,
        "amount_issued": num(c.amount_issued),
        "amount_settled": num(c.amount_settled),
        "amount_returned": num(c.amount_returned),
        "amount_shortfall": num(c.amount_shortfall),
        "amount_pending": c.amount_pending,
        "purpose": c.purpose,
        "issued_on": iso(c.issued_on),
        "settlement_due_date": iso(c.settlement_due_date),
        "status": enum_of(c.status),
        "created_at": iso(c.created_at),
    }


def cash_custody_request_brief(r):
    if not r:
        return None
    return {
        "id": r.id,
        "amount": num(r.amount),
        "purpose": getattr(r, "purpose", None),
        "needed_by_date": iso(getattr(r, "needed_by_date", None)),
        "status": enum_of(r.status),
        "created_at": iso(r.created_at),
    }


# ─── Item custody ──────────────────────────────────────────────────────
def custody_item_brief(i):
    if not i:
        return None
    return {
        "id": i.id,
        "name": i.name,
        "serial_number": getattr(i, "serial_number", None),
        "description": getattr(i, "description", None),
    }


def item_custody_brief(c):
    if not c:
        return None
    item = None
    try:
        from app.models import CustodyItem
        item = c.item if hasattr(c, "item") else None
    except Exception:
        item = None
    return {
        "id": c.id,
        "item_id": c.item_id,
        "item_name": item.name if item else None,
        "handed_over_on": iso(c.handed_over_on),
        "settled_on": iso(c.settled_on),
        "condition_at_handover": c.condition_at_handover,
        "condition_at_return": c.condition_at_return,
        "status": enum_of(c.status),
        "created_at": iso(c.created_at) if hasattr(c, "created_at") else None,
    }


def item_custody_request_brief(r):
    if not r:
        return None
    return {
        "id": r.id,
        "item_id": r.item_id,
        "item_name": r.item.name if getattr(r, "item", None) else None,
        "purpose": getattr(r, "purpose", None),
        "status": enum_of(r.status),
        "created_at": iso(r.created_at),
    }


# ─── Daily reports ─────────────────────────────────────────────────────
def daily_report_brief(r):
    if not r:
        return None
    return {
        "id": r.id,
        "report_date": iso(r.report_date),
        "status": enum_of(r.status),
        "created_at": iso(r.created_at),
    }


def daily_report_full(r):
    base = daily_report_brief(r)
    if base:
        base.update({
            "body": getattr(r, "body", None),
            "employee_notes": getattr(r, "employee_notes", None),
            "submitted_at": iso(getattr(r, "submitted_at", None)),
        })
    return base


# ─── Notification ──────────────────────────────────────────────────────
def notification_brief(n):
    if not n:
        return None
    # NotificationKind is `str, enum.Enum` (see models/opsflow_extras.py:59)
    # so `n.kind` is both the string value AND an enum member. Route it
    # through enum_of() to match the module's {value, label_ar} contract
    # — a client that reads notif["kind"]["value"] on the wire needs
    # the same shape it gets from every other status field.
    return {
        "id": n.id,
        "kind": enum_of(n.kind),
        "title": n.title,
        "body": n.body,
        "link_url": n.link_url,
        "read_at": iso(n.read_at),
        "is_read": n.read_at is not None,
        "created_at": iso(n.created_at),
    }


# ─── Task (mirrors api_v1._task_brief so mobile matches) ───────────────
def task_brief(t):
    if not t:
        return None
    return {
        "id": t.id,
        "title": t.title,
        "status": enum_of(t.status),
        "priority": enum_of(getattr(t, "priority", None)),
        "deadline": iso(getattr(t, "deadline", None)),
        "project_id": getattr(t, "project_id", None),
        "archived_at": iso(getattr(t, "archived_at", None)),
    }
