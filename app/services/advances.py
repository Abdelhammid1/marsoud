"""MARSOUD-ADVANCES — employee advance (سلفة) service.

Everything that creates an advance funnels through approve_advance(),
whether it came from an employee request or an accountant adding one
directly. That's the only place the disbursement journal is posted, so
the two paths can't drift apart.

Accounting (MARSOUD-PAYROLL-LEDGER-03 convention — every employee
movement lands on their own sub-account under 2130 so it shows up in
كشف حساب الموظف):

    disbursement    Dr  2130-NNNNNN (employee)      amount
                    Cr  cash / bank                 amount

The employee's leaf now carries a debit — a contra balance meaning the
employee owes the company. Payroll amortises it: run_payroll credits the
leaf with net + the installment it recovered (i.e. the full salary),
while only paying out net. See app/services/payroll.py.
"""
from datetime import date, datetime

from app import db
from app.models import (
    Employee, EmployeeAdvance, AdvanceRequest,
    AdvanceStatus, AdvanceSource, AdvanceRequestStatus,
    PaymentMethod,
)
from app.services.ledger import post_journal, reverse_journal, get_account_by_code


class AdvanceError(Exception):
    """User-facing validation error in the advances flow."""


# ─── Queries ────────────────────────────────────────────────────────────
def active_advance_for(employee_id):
    """The employee's open advance, or None."""
    return EmployeeAdvance.query.filter_by(
        employee_id=employee_id, status=AdvanceStatus.ACTIVE,
    ).first()


def installment_due_for(employee):
    """What the next payroll run should deduct for this employee.

    Used to prefill the payroll form and to show the employee what's
    coming. 0.0 when there's no open advance.
    """
    if employee is None:
        return 0.0
    adv = active_advance_for(employee.id)
    return adv.next_installment if adv else 0.0


def advances_for_company(company_id, status=None):
    q = EmployeeAdvance.query.filter_by(company_id=company_id)
    if status is not None:
        q = q.filter_by(status=status)
    return q.order_by(EmployeeAdvance.created_at.desc()).limit(300).all()


def pending_requests_for_company(company_id):
    return AdvanceRequest.query.filter_by(
        company_id=company_id, status=AdvanceRequestStatus.PENDING,
    ).order_by(AdvanceRequest.created_at.desc()).all()


# ─── The single approval funnel ─────────────────────────────────────────
def approve_advance(company_id, employee_id, amount, months, disbursed_on, *,
                    source, actor_id, payment_method_id=None, note=None,
                    request=None):
    """Create an EmployeeAdvance and post the disbursement journal.

    The ONLY place an advance comes into existence. Both the approved-
    request path and the direct-add path end up here.

    `months` splits the amount into equal installments; the last one
    absorbs any rounding remainder because the payroll deduction is
    min(monthly_installment, remaining).

    Returns the EmployeeAdvance.
    """
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise AdvanceError("المبلغ غير صالح")
    if amount <= 0:
        raise AdvanceError("مبلغ السلفة يجب أن يكون أكبر من صفر")

    try:
        months = int(months)
    except (TypeError, ValueError):
        raise AdvanceError("عدد الشهور غير صالح")
    if months < 1:
        raise AdvanceError("عدد الشهور يجب أن يكون شهر واحد على الأقل")

    emp = db.session.get(Employee, employee_id)
    if not emp or emp.company_id != company_id:
        raise AdvanceError("الموظف غير موجود")

    # Scope: one open advance per employee (see the model docstring).
    if active_advance_for(emp.id):
        raise AdvanceError(
            f"{emp.name} لديه سلفة نشطة بالفعل — يجب سدادها أو إلغاؤها أولاً"
        )

    disbursed_on = disbursed_on or date.today()

    # Where the money leaves from. Same resolution as vendor payments.
    if payment_method_id:
        pm = db.session.get(PaymentMethod, int(payment_method_id))
        if not pm or pm.company_id != company_id or not pm.is_active:
            raise AdvanceError("طريقة الصرف غير صالحة")
        pay_account = pm.account
        pay_label = pm.name_ar or pm.name
    else:
        pay_account = get_account_by_code(company_id, "1110")
        pay_label = "نقدي"
    if not pay_account:
        raise AdvanceError("حساب النقدية (1110) غير موجود — راجع شجرة الحسابات")

    from app.services.subsidiary import party_payroll_account
    emp_account = party_payroll_account(emp)
    if not emp_account:
        raise AdvanceError("حساب الموظف الفرعي غير موجود")

    adv = EmployeeAdvance(
        company_id=company_id,
        employee_id=emp.id,
        amount=amount,
        remaining=amount,
        months=months,
        monthly_installment=round(amount / months, 2),
        disbursed_on=disbursed_on,
        status=AdvanceStatus.ACTIVE,
        source=source,
        request_id=request.id if request else None,
        note=note,
        approved_by=actor_id,
        created_by=actor_id,
    )
    db.session.add(adv)
    db.session.flush()   # need adv.id for the journal reference

    # post_journal commits — everything flushed above lands with it.
    entry = post_journal(
        company_id=company_id,
        description=f"صرف سلفة — {emp.name} ({amount:.2f})",
        lines=[
            {"account_id": emp_account.id, "debit": amount, "credit": 0,
             "memo": f"سلفة — {emp.name}"},
            {"account_id": pay_account.id, "debit": 0, "credit": amount,
             "memo": f"صرف سلفة {pay_label} — {emp.name}"},
        ],
        entry_date=disbursed_on,
        reference=f"ADV-{adv.id}",
        created_by=actor_id,
        source_type="employee_advance",
        source_id=adv.id,
    )
    adv.journal_entry_id = entry.id
    db.session.commit()

    _notify_employee(
        adv, title=f"💵 تم اعتماد سلفة: {amount:.2f}",
        body=(f"سيتم خصمها على {months} "
              f"{'شهر' if months == 1 else 'شهور'} "
              f"({float(adv.monthly_installment):.2f} شهرياً)"),
    )
    _log(adv, "CREATE", f"صرف سلفة {amount:.2f} — {emp.name}")
    return adv


# ─── Employee-request path ──────────────────────────────────────────────
def submit_advance_request(company_id, employee_id, amount, *,
                           reason=None, created_by=None):
    """An employee asking for an advance from /my/."""
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise AdvanceError("المبلغ غير صالح")
    if amount <= 0:
        raise AdvanceError("مبلغ السلفة يجب أن يكون أكبر من صفر")

    emp = db.session.get(Employee, employee_id)
    if not emp or emp.company_id != company_id:
        raise AdvanceError("الموظف غير موجود")

    if active_advance_for(emp.id):
        raise AdvanceError("لديك سلفة نشطة بالفعل — لا يمكن طلب سلفة جديدة قبل سدادها")

    pending = AdvanceRequest.query.filter_by(
        employee_id=emp.id, status=AdvanceRequestStatus.PENDING,
    ).first()
    if pending:
        raise AdvanceError("لديك طلب سلفة قيد المراجعة بالفعل")

    req = AdvanceRequest(
        company_id=company_id,
        employee_id=emp.id,
        amount=amount,
        reason=(reason or "").strip() or None,
        status=AdvanceRequestStatus.PENDING,
        created_by=created_by,
    )
    db.session.add(req)
    db.session.commit()
    _notify_approvers(req)
    return req


def approve_advance_request(req, *, reviewer_id, months, disbursed_on=None,
                            payment_method_id=None, review_note=None):
    """Approve a PENDING request → a real advance + its journal."""
    if req.status != AdvanceRequestStatus.PENDING:
        raise AdvanceError("يمكن اعتماد الطلبات في حالة الانتظار فقط")

    adv = approve_advance(
        req.company_id, req.employee_id, req.amount, months,
        disbursed_on or date.today(),
        source=AdvanceSource.REQUEST, actor_id=reviewer_id,
        payment_method_id=payment_method_id,
        note=review_note, request=req,
    )

    req.status = AdvanceRequestStatus.APPROVED
    req.reviewed_by = reviewer_id
    req.reviewed_at = datetime.utcnow()
    if review_note:
        req.review_note = review_note
    db.session.commit()
    return adv


def reject_advance_request(req, *, reviewer_id, review_note=None):
    if req.status != AdvanceRequestStatus.PENDING:
        raise AdvanceError("يمكن رفض الطلبات في حالة الانتظار فقط")
    req.status = AdvanceRequestStatus.REJECTED
    req.reviewed_by = reviewer_id
    req.reviewed_at = datetime.utcnow()
    if review_note:
        req.review_note = review_note
    db.session.commit()

    emp = req.employee
    if emp and emp.user_id:
        _notify(emp.user_id, req.company_id,
                title=f"تم رفض طلب السلفة ({float(req.amount):.2f})",
                body=(review_note or "").strip() or None)
    return req


# ─── Cancellation ───────────────────────────────────────────────────────
def cancel_advance(adv, *, actor_id, reason=None):
    """Reverse the disbursement journal and stop future deductions.

    Deliberately does NOT claw back installments already deducted from
    past payroll runs — that would mean rewriting closed payroll
    journals. If an entry error needs a full unwind, the payroll entry
    itself gets reversed by hand.
    """
    if adv.status != AdvanceStatus.ACTIVE:
        raise AdvanceError("يمكن إلغاء السلف النشطة فقط")

    if adv.journal_entry_id:
        entry = reverse_journal(adv.journal_entry_id, created_by=actor_id)
        adv.reversal_entry_id = entry.id

    # reverse_journal() already flips status/remaining through
    # _undo_source_side_effects; setting them again keeps this correct
    # for an advance that somehow has no journal, and is idempotent.
    adv.status = AdvanceStatus.CANCELLED
    adv.remaining = 0
    adv.cancelled_by = actor_id
    adv.cancelled_at = datetime.utcnow()
    adv.cancel_reason = (reason or "").strip() or None
    db.session.commit()

    _notify_employee(
        adv, title=f"تم إلغاء السلفة ({float(adv.amount):.2f})",
        body=adv.cancel_reason,
    )
    _log(adv, "UPDATE", f"إلغاء سلفة — {adv.employee.name if adv.employee else ''}")
    return adv


# ─── Payroll hook ───────────────────────────────────────────────────────
def apply_advance_deduction(employee, amount, *, run=None,
                            period_year=None, period_month=None,
                            payroll_line=None):
    """Recover an instalment from the employee's open advance.

    MARSOUD-ADVANCE-INSTALMENTS (2026-08-05) — two things changed here.

    1. `amount=None` means "work it out", not "deduct nothing". The
       caller used to pass `inputs.get("advance", 0) or 0`, so the whole
       automation lived in the payroll FORM: any other path — a script,
       the agent, a future importer — deducted zero and the advance
       stayed open forever. The balance is the source of truth now; the
       form merely displays what this will do.

       amount is None  → the instalment, from the open balance
       amount == 0     → a deliberate skip, recorded as a zero row
       amount > 0      → respected exactly as typed

    2. Every instalment writes an AdvanceRepayment row linked to the run
       and stamped with the period. That row is what makes a redone
       payroll safe: the unique constraint on (advance_id, run_id) plus
       the check below mean the same advance cannot be recovered twice
       for the same run.

    Returns how much was ACTUALLY applied to a tracked advance — 0.0
    when there is no advance behind the number. run_payroll uses the
    return value, not the raw input, to decide how much to add back to
    the salary-payable credit, so untracked manual deductions keep
    behaving exactly as before.

    Does not commit — run_payroll's own commit covers it.
    """
    if employee is None:
        return 0.0

    adv = active_advance_for(employee.id)
    if not adv:
        # Nothing tracked. A hand-typed number still shows on the payslip
        # (run_payroll put it on the line); it just has no balance to
        # draw from, which is the pre-existing behaviour.
        return 0.0

    manual = amount is not None and str(amount).strip() != ""
    if manual:
        try:
            requested = round(float(amount), 2)
        except (TypeError, ValueError):
            return 0.0
        if requested < 0:
            return 0.0
    else:
        requested = adv.next_installment

    # Idempotency, keyed on the PERIOD rather than the run.
    #
    # Today run_payroll already refuses a second run for a period
    # ("كشف رواتب {month}/{year} موجود بالفعل") and there is no delete
    # path for a run anywhere in the app — no route, no service — so a
    # literal re-run cannot happen through the UI. What IS reachable is
    # the service being called twice by a script, the agent, or any
    # future caller, which is the same gap that let the form own the
    # automation in the first place.
    #
    # Keying on (year, month) rather than run_id covers both: a second
    # call for a period that already has an instalment returns what was
    # taken instead of taking it again, whichever run it came from. The
    # unique constraint on (advance_id, payroll_run_id) backs it at the
    # database level.
    year = period_year if period_year is not None else (
        run.period_year if run is not None else None)
    month = period_month if period_month is not None else (
        run.period_month if run is not None else None)
    if year is not None and month is not None:
        from app.models import AdvanceRepayment
        already = AdvanceRepayment.query.filter_by(
            advance_id=adv.id, period_year=year, period_month=month).first()
        if already is not None:
            return float(already.amount or 0)

    applied = min(requested, round(float(adv.remaining or 0), 2))
    if applied < 0:
        applied = 0.0

    if applied > 0:
        adv.remaining = round(float(adv.remaining) - applied, 2)
        if float(adv.remaining) < 0.005:
            adv.remaining = 0
            adv.status = AdvanceStatus.SETTLED
            adv.settled_at = datetime.utcnow()

    # The row is written even for a zero instalment: "the accountant
    # skipped this month" and "this month was never run" are different
    # facts, and only a row can tell them apart.
    if run is not None:
        from app.models import AdvanceRepayment
        db.session.add(AdvanceRepayment(
            company_id=adv.company_id,
            advance_id=adv.id,
            payroll_run_id=run.id,
            payroll_line_id=payroll_line.id if payroll_line else None,
            period_year=year,
            period_month=month,
            amount=applied,
            manual=manual,
        ))
        db.session.flush()

    return applied


def repayments_for(advance_id):
    """Every instalment taken against one advance, oldest first."""
    from app.models import AdvanceRepayment
    return (AdvanceRepayment.query
            .filter_by(advance_id=advance_id)
            .order_by(AdvanceRepayment.period_year.asc(),
                      AdvanceRepayment.period_month.asc(),
                      AdvanceRepayment.id.asc())
            .all())


# ─── Notifications ──────────────────────────────────────────────────────
def _notify(user_id, company_id, *, title, body=None, link_url=None):
    try:
        from app.services.opsflow_extras import notify
        from app.models import NotificationKind
        notify(user_id, company_id=company_id,
               kind=NotificationKind.TASK_ASSIGNED,
               title=title, body=body, link_url=link_url)
    except Exception:
        from flask import current_app
        current_app.logger.exception("advance notify failed")


def _notify_employee(adv, *, title, body=None):
    emp = adv.employee
    if not emp or not emp.user_id:
        return
    from flask import url_for
    try:
        link = url_for("portal_emp.account") + "#advances"
    except Exception:
        link = None
    _notify(emp.user_id, adv.company_id, title=title, body=body, link_url=link)


def _notify_approvers(req):
    """Ping everyone who can act on the request (advances.manage roles)."""
    try:
        from flask import url_for
        from app.models.user import user_companies
        rows = db.session.execute(
            user_companies.select().where(
                (user_companies.c.company_id == req.company_id) &
                (user_companies.c.role.in_(["owner", "admin", "accountant"]))
            )
        ).fetchall()
        emp_name = req.employee.name if req.employee else ""
        link = url_for("advances.requests")
        for r in rows:
            _notify(r.user_id, req.company_id,
                    title=f"💵 طلب سلفة جديد: {emp_name}",
                    body=f"{float(req.amount):.2f}", link_url=link)
    except Exception:
        from flask import current_app
        current_app.logger.exception("advance request notify failed")


def _log(adv, action_type, label):
    try:
        from app.services.activity import log_action
        log_action(action_type=action_type, entity_type="employee_advance",
                   entity_id=adv.id, entity_label=label,
                   company_id=adv.company_id)
    except Exception:
        pass
