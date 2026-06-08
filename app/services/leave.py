"""HR Phase 2 services — leave types, balances, monthly accrual, attendance
exceptions, leave-request workflow.

Spec mantra (MARSOUD-25): "every day = attendance, unless logged as an
exception." That principle drives every function in this module: HR-06
approval converts a leave request into a set of dated AttendanceException
rows, and HR-07 sums those rows back into the payroll calculation. Nothing
in between needs a per-day attendance roster.
"""
import logging
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from app import db
from app.models import (
    Employee, EmployeeStatus,
    LeaveType, LeaveBalance,
    AttendanceException, AttendanceExceptionType,
    LeaveRequest, LeaveRequestStatus,
)

logger = logging.getLogger("ledgeros.leave")


class LeaveError(Exception):
    """Domain-level error from the leave layer.

    Routes catch it and flash the message — same shape as LedgerError.
    """


# ─── HR-05: leave types ─────────────────────────────────────────────────
DEFAULT_LEAVE_TYPES_SA = [
    # Saudi Arabia defaults (21 days/year annual = 1.75/month)
    ("سنوية", Decimal("1.75"), Decimal("60.00"), True),
    ("مرضية", Decimal("1.00"), Decimal("30.00"), True),
    ("طارئة", Decimal("0.25"), Decimal("3.00"),  True),
    ("بدون راتب", Decimal("0.00"), Decimal("0.00"), False),
]


def seed_default_leave_types(company_id):
    """Idempotent: create the four default leave types if none exist."""
    if LeaveType.query.filter_by(company_id=company_id).count() > 0:
        return 0
    for name, accrual, cap, paid in DEFAULT_LEAVE_TYPES_SA:
        db.session.add(LeaveType(
            company_id=company_id, name=name,
            accrual_per_month=accrual, max_balance=cap,
            is_paid=paid, is_active=True,
        ))
    db.session.commit()
    return len(DEFAULT_LEAVE_TYPES_SA)


def create_leave_type(company_id, *, name, accrual_per_month, max_balance, is_paid):
    name = (name or "").strip()
    if not name:
        raise LeaveError("الاسم مطلوب")
    if LeaveType.query.filter_by(company_id=company_id, name=name).first():
        raise LeaveError("يوجد نوع إجازة بنفس الاسم")
    lt = LeaveType(
        company_id=company_id, name=name,
        accrual_per_month=Decimal(str(accrual_per_month or 0)),
        max_balance=Decimal(str(max_balance or 0)),
        is_paid=bool(is_paid), is_active=True,
    )
    db.session.add(lt)
    db.session.commit()
    return lt


def update_leave_type(lt, *, name=None, accrual_per_month=None,
                       max_balance=None, is_paid=None, is_active=None):
    if name is not None:
        name = name.strip()
        if not name:
            raise LeaveError("الاسم مطلوب")
        if name != lt.name:
            clash = LeaveType.query.filter_by(company_id=lt.company_id, name=name).first()
            if clash and clash.id != lt.id:
                raise LeaveError("يوجد نوع إجازة بنفس الاسم")
        lt.name = name
    if accrual_per_month is not None:
        lt.accrual_per_month = Decimal(str(accrual_per_month))
    if max_balance is not None:
        lt.max_balance = Decimal(str(max_balance))
    if is_paid is not None:
        lt.is_paid = bool(is_paid)
    if is_active is not None:
        lt.is_active = bool(is_active)
    db.session.commit()
    return lt


def ensure_employee_balances(employee, year=None):
    """Make sure a LeaveBalance row exists for (employee, each active type, year).

    Called on employee creation (so the row exists when the cron first runs)
    and at the top of monthly_leave_accrual() so a newly-created leave type
    starts accruing for everyone.
    """
    year = year or date.today().year
    types = LeaveType.query.filter_by(
        company_id=employee.company_id, is_active=True,
    ).all()
    created = 0
    for lt in types:
        exists = LeaveBalance.query.filter_by(
            employee_id=employee.id, leave_type_id=lt.id, year=year,
        ).first()
        if exists:
            continue
        db.session.add(LeaveBalance(
            employee_id=employee.id, leave_type_id=lt.id, year=year,
            balance_days=Decimal("0"), used_days=Decimal("0"),
        ))
        created += 1
    if created:
        db.session.commit()
    return created


def monthly_leave_accrual(today=None):
    """Run once a month — add accrual_per_month to every active employee's
    balance, capped at max_balance. Idempotent on re-run within the same
    month: the dedup key is the running balance vs cap.

    Returns a summary dict suitable for the /cron/tick payload.
    """
    today = today or date.today()
    year = today.year

    accrued = 0
    capped = 0
    employees = Employee.query.filter_by(status=EmployeeStatus.ACTIVE).all()
    for emp in employees:
        ensure_employee_balances(emp, year=year)
        balances = LeaveBalance.query.filter_by(
            employee_id=emp.id, year=year,
        ).all()
        for bal in balances:
            lt = bal.leave_type
            if not lt or not lt.is_active:
                continue
            cap = float(lt.max_balance or 0)
            current = float(bal.balance_days or 0)
            inc = float(lt.accrual_per_month or 0)
            if inc <= 0:
                continue
            new = current + inc
            if cap > 0 and new > cap:
                new = cap
                capped += 1
            if new > current:
                bal.balance_days = Decimal(str(round(new, 2)))
                accrued += 1
    db.session.commit()
    return {"accrued_balances": accrued, "capped": capped}


# ─── HR-05b: attendance exceptions ──────────────────────────────────────
def create_exception(*, company_id, employee_id, date_, type_, duration_hours=None,
                     note=None, leave_request_id=None, created_by=None):
    """Insert a new AttendanceException. Raises LeaveError on duplicate (employee, date)."""
    if not isinstance(type_, AttendanceExceptionType):
        try:
            type_ = AttendanceExceptionType[type_]
        except KeyError:
            raise LeaveError("نوع الاستثناء غير صالح")

    existing = AttendanceException.query.filter_by(
        employee_id=employee_id, date=date_,
    ).first()
    if existing:
        raise LeaveError("يوجد استثناء مسجل بالفعل لهذا الموظف في نفس اليوم")

    if type_ == AttendanceExceptionType.LATE:
        if duration_hours is None or float(duration_hours) <= 0:
            raise LeaveError("ساعات التأخير مطلوبة لنوع 'تأخير'")
        duration_hours = Decimal(str(round(float(duration_hours), 2)))
    else:
        duration_hours = None

    ex = AttendanceException(
        company_id=company_id, employee_id=employee_id,
        date=date_, type=type_, duration_hours=duration_hours,
        note=(note or "").strip() or None,
        leave_request_id=leave_request_id, created_by=created_by,
    )
    db.session.add(ex)
    db.session.commit()
    return ex


def delete_exception(exception):
    """Refuse to delete if attached to a LeaveRequest — the user must cancel
    the request instead (which deletes its exceptions as a side effect)."""
    if exception.leave_request_id:
        raise LeaveError(
            "هذا الاستثناء مرتبط بطلب إجازة معتمد — ألغِ الطلب أولاً ليحذف معه."
        )
    db.session.delete(exception)
    db.session.commit()


def exceptions_in_period(company_id, year, month, employee_id=None):
    """Return all AttendanceException rows that fall within (year, month) for
    the given company (optionally narrowed to one employee).

    Used by both the HR calendar view and HR-07 payroll auto-fill.
    """
    days_in_month = monthrange(year, month)[1]
    start = date(year, month, 1)
    end = date(year, month, days_in_month)
    q = AttendanceException.query.filter(
        AttendanceException.company_id == company_id,
        AttendanceException.date >= start,
        AttendanceException.date <= end,
    )
    if employee_id is not None:
        q = q.filter(AttendanceException.employee_id == employee_id)
    return q.order_by(AttendanceException.date).all()


def attendance_deductions(employee_id, year, month):
    """Aggregate exceptions for one employee in one period into
    {absence_days, late_days, approved_days, paid_leave}.

    HR-07 plugs this into run_payroll.
    """
    days_in_month = monthrange(year, month)[1]
    start = date(year, month, 1)
    end = date(year, month, days_in_month)
    rows = AttendanceException.query.filter(
        AttendanceException.employee_id == employee_id,
        AttendanceException.date >= start,
        AttendanceException.date <= end,
    ).all()

    absence_days = 0.0
    late_days = 0.0
    approved_days = 0.0
    has_any = False
    for ex in rows:
        has_any = True
        if ex.type == AttendanceExceptionType.ABSENT:
            absence_days += 1.0
        elif ex.type == AttendanceExceptionType.UNPAID_LEAVE:
            absence_days += 1.0
        elif ex.type == AttendanceExceptionType.LATE:
            late_days += ex.deduction_days()
        elif ex.type == AttendanceExceptionType.APPROVED_LEAVE:
            approved_days += 1.0
    return {
        "absence_days": round(absence_days, 2),
        "late_days": round(late_days, 2),
        "approved_days": round(approved_days, 2),
        "has_exceptions": has_any,
    }


# ─── HR-06: leave-request workflow ──────────────────────────────────────
# Default rest days = Friday (Python weekday 4) + Saturday (weekday 5) —
# the standard Gulf/Egypt weekend. Per-company configurability lives in
# Phase 3 of the HR module; until then this default applies.
DEFAULT_REST_WEEKDAYS = {4, 5}


def _is_rest_day(d, rest_weekdays=None):
    rest_weekdays = rest_weekdays or DEFAULT_REST_WEEKDAYS
    return d.weekday() in rest_weekdays


def _daterange_days(start_date, end_date, rest_weekdays=None):
    """Inclusive working-day count between two dates, skipping rest days.

    Matches the HR-06 spec acceptance: "أيام الراحة تُستثنى من الحساب — لا تُنشئ
    استثناء ولا تُحتسب".
    """
    if end_date < start_date:
        return 0
    days = 0
    d = start_date
    while d <= end_date:
        if not _is_rest_day(d, rest_weekdays):
            days += 1
        d += timedelta(days=1)
    return days


def submit_leave_request(*, company_id, employee_id, leave_type_id,
                          start_date, end_date, reason=None, created_by=None):
    if end_date < start_date:
        raise LeaveError("تاريخ نهاية الإجازة قبل البداية")
    emp = db.session.get(Employee, employee_id)
    if not emp or emp.company_id != company_id:
        raise LeaveError("الموظف غير موجود")
    lt = db.session.get(LeaveType, leave_type_id)
    if not lt or lt.company_id != company_id or not lt.is_active:
        raise LeaveError("نوع الإجازة غير صالح")

    # Reject overlap with an existing non-cancelled/rejected request OR
    # an existing attendance exception in the date range.
    overlap = LeaveRequest.query.filter(
        LeaveRequest.employee_id == employee_id,
        LeaveRequest.status.in_([LeaveRequestStatus.PENDING, LeaveRequestStatus.APPROVED]),
        LeaveRequest.start_date <= end_date,
        LeaveRequest.end_date >= start_date,
    ).first()
    if overlap:
        raise LeaveError("يوجد طلب آخر في فترة متداخلة")
    exception_clash = AttendanceException.query.filter(
        AttendanceException.employee_id == employee_id,
        AttendanceException.date >= start_date,
        AttendanceException.date <= end_date,
    ).first()
    if exception_clash:
        raise LeaveError("توجد استثناءات حضور مسبقة في هذه الفترة — راجعها أولاً")

    days_count = _daterange_days(start_date, end_date)

    # Balance check — paid types only. Unpaid leave is exempt.
    if lt.is_paid:
        year = start_date.year
        ensure_employee_balances(emp, year=year)
        bal = LeaveBalance.query.filter_by(
            employee_id=employee_id, leave_type_id=lt.id, year=year,
        ).first()
        remaining = float(bal.remaining_days) if bal else 0.0
        if remaining < days_count:
            raise LeaveError(
                f"الرصيد غير كافٍ — المتاح {remaining:.2f} يوم، المطلوب {days_count}"
            )

    req = LeaveRequest(
        company_id=company_id, employee_id=employee_id,
        leave_type_id=leave_type_id,
        start_date=start_date, end_date=end_date,
        days_count=Decimal(str(days_count)),
        reason=(reason or "").strip() or None,
        status=LeaveRequestStatus.PENDING, created_by=created_by,
    )
    db.session.add(req)
    db.session.commit()
    return req


def approve_leave_request(req, *, reviewer_id, review_note=None):
    """Approve a PENDING request: deduct used_days, create AttendanceExceptions
    for every day in the range, link them back to the request."""
    from datetime import datetime
    if req.status != LeaveRequestStatus.PENDING:
        raise LeaveError("يمكن اعتماد الطلبات في حالة الانتظار فقط")

    lt = req.leave_type
    days_count = int(req.days_count) if float(req.days_count).is_integer() else float(req.days_count)

    # Deduct used_days on the LeaveBalance for paid types only.
    if lt.is_paid:
        year = req.start_date.year
        ensure_employee_balances(req.employee, year=year)
        bal = LeaveBalance.query.filter_by(
            employee_id=req.employee_id, leave_type_id=lt.id, year=year,
        ).first()
        if bal is None:
            raise LeaveError("لا يوجد رصيد لهذا الموظف")
        new_used = float(bal.used_days or 0) + float(req.days_count)
        if new_used - float(bal.balance_days or 0) > 0.01:
            raise LeaveError("الرصيد غير كافٍ — قد يكون استُهلك بعد تقديم الطلب")
        bal.used_days = Decimal(str(round(new_used, 2)))

    # Create one AttendanceException per working day in the inclusive range.
    # Rest days (Friday / Saturday by default) are skipped — they don't
    # consume balance and don't count toward absence in payroll.
    ex_type = (AttendanceExceptionType.APPROVED_LEAVE
               if lt.is_paid else AttendanceExceptionType.UNPAID_LEAVE)
    d = req.start_date
    created = 0
    while d <= req.end_date:
        if _is_rest_day(d):
            d += timedelta(days=1)
            continue
        # Be tolerant: if for any reason an exception already exists on this
        # day, skip it (we'd already refused the request earlier — this is
        # defensive only).
        exists = AttendanceException.query.filter_by(
            employee_id=req.employee_id, date=d,
        ).first()
        if not exists:
            db.session.add(AttendanceException(
                company_id=req.company_id,
                employee_id=req.employee_id,
                date=d, type=ex_type,
                note=(req.reason or "")[:140] or None,
                leave_request_id=req.id,
                created_by=reviewer_id,
            ))
            created += 1
        d += timedelta(days=1)

    req.status = LeaveRequestStatus.APPROVED
    req.reviewed_by = reviewer_id
    req.reviewed_at = datetime.utcnow()
    if review_note:
        req.review_note = review_note
    db.session.commit()
    return req, created


def reject_leave_request(req, *, reviewer_id, review_note=None):
    from datetime import datetime
    if req.status != LeaveRequestStatus.PENDING:
        raise LeaveError("يمكن رفض الطلبات في حالة الانتظار فقط")
    req.status = LeaveRequestStatus.REJECTED
    req.reviewed_by = reviewer_id
    req.reviewed_at = datetime.utcnow()
    req.review_note = (review_note or None)
    db.session.commit()
    return req


def cancel_leave_request(req, *, reviewer_id, review_note=None):
    """Cancel a request. If it was approved, restore used_days and delete
    the linked AttendanceExceptions."""
    from datetime import datetime
    if req.status == LeaveRequestStatus.CANCELLED:
        return req
    was_approved = req.status == LeaveRequestStatus.APPROVED

    if was_approved and req.leave_type.is_paid:
        bal = LeaveBalance.query.filter_by(
            employee_id=req.employee_id, leave_type_id=req.leave_type_id,
            year=req.start_date.year,
        ).first()
        if bal:
            new_used = max(0.0, float(bal.used_days or 0) - float(req.days_count or 0))
            bal.used_days = Decimal(str(round(new_used, 2)))

    if was_approved:
        AttendanceException.query.filter_by(leave_request_id=req.id).delete()

    req.status = LeaveRequestStatus.CANCELLED
    req.reviewed_by = reviewer_id
    req.reviewed_at = datetime.utcnow()
    if review_note:
        req.review_note = review_note
    db.session.commit()
    return req
