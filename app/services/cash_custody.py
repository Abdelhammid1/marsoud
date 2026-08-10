"""MARSOUD-CASH-CUSTODY-01 (2026-08-07) — cash-custody service.

Everything that mutates a custody funnels through here. The pattern
is lifted from `app/services/advances.py::approve_advance`:

  1. Build the source row → `db.session.add(row); db.session.flush()`
  2. `post_journal(source_type="cash_custody", source_id=row.id)`
  3. `row.journal_entry_id = entry.id; db.session.commit()`
  4. Never `db.session.add(JournalEntry)` directly.

Accounting:

  issue           Dr  1180-NNNNNN (holder sub-account)   amount
                  Cr  cash / bank (payment_method)       amount

  close_settlement (single journal per close):
                  Dr  each expense_account_id            line.amount
                  Cr  1180-NNNNNN                        sum(lines)
                  Dr  cash / bank                        returned_amount
                  Cr  1180-NNNNNN                        returned_amount
                  Dr  2130-NNNNNN or "عجز عهدة"          shortfall
                  Cr  1180-NNNNNN                        shortfall

  cancel          `reverse_journal(issue_entry_id)` — atomically undoes
                  the issue via `_undo_source_side_effects("cash_custody")`

Distinct from advances by design: custody is closed by receipts +
return-of-excess / shortfall handling, NOT deducted from salary.
Never entangle the two lifecycles.
"""
from datetime import date, datetime

from app import db
from app.models import (
    Employee, Department, PaymentMethod,
    CashCustody, CashCustodyRequest, CashCustodySettlementLine,
    CustodyHolderType, CustodyStatus, CustodyRequestStatus,
    ShortfallDisposition, EmployeeStatus,
)
from app.services.ledger import (
    post_journal, reverse_journal, get_account_by_code, LedgerError,
)


class CustodyError(Exception):
    """User-facing validation error in the cash-custody flow."""


# ═══════════ Queries ════════════════════════════════════════════
def custodies_for_company(company_id, status=None):
    q = CashCustody.query.filter_by(company_id=company_id)
    if status is not None:
        q = q.filter_by(status=status)
    return q.order_by(CashCustody.created_at.desc()).limit(500).all()


def pending_requests_for_company(company_id):
    return CashCustodyRequest.query.filter_by(
        company_id=company_id, status=CustodyRequestStatus.PENDING,
    ).order_by(CashCustodyRequest.created_at.desc()).all()


def requests_for_company(company_id, status=None):
    q = CashCustodyRequest.query.filter_by(company_id=company_id)
    if status is not None:
        q = q.filter_by(status=status)
    return q.order_by(CashCustodyRequest.created_at.desc()).limit(300).all()


def overdue_custodies_for_company(company_id, as_of=None):
    """Open custodies past their settlement_due_date. Used by the
    open-custody report + the cron overdue sweep."""
    if as_of is None:
        as_of = date.today()
    return CashCustody.query.filter(
        CashCustody.company_id == company_id,
        CashCustody.status.in_((CustodyStatus.ISSUED,
                                CustodyStatus.PARTIALLY_SETTLED)),
        CashCustody.settlement_due_date.isnot(None),
        CashCustody.settlement_due_date < as_of,
    ).order_by(CashCustody.settlement_due_date.asc()).all()


def sweep_overdue_custodies(company_id):
    """MARSOUD-CASH-CUSTODY-01 (2026-08-07, slice 3) — one-shot bell
    notification for every custody past its settlement_due_date.

    Called from /cron/tick per active company. Uses a dedup column
    (custody_overdue_notified_at) rather than the vendor-bill trick
    of a one-way state flip — a custody stays ISSUED/
    PARTIALLY_SETTLED when overdue (unlike a bill flipping to
    OVERDUE), so we need explicit dedup or every tick would spam.

    The column is nulled again on close_settlement + cancel_custody
    so a re-issued custody with the same id doesn't inherit an old
    notification stamp (matches how VendorBillStatus flips clear
    the "already notified" state implicitly).

    Returns the count of notifications fired (not the count of
    overdue custodies — a custody that was notified last tick and
    is still overdue this tick does not count).
    """
    today = date.today()
    overdue = CashCustody.query.filter(
        CashCustody.company_id == company_id,
        CashCustody.status.in_((CustodyStatus.ISSUED,
                                CustodyStatus.PARTIALLY_SETTLED)),
        CashCustody.settlement_due_date.isnot(None),
        CashCustody.settlement_due_date < today,
        CashCustody.custody_overdue_notified_at.is_(None),
    ).all()
    if not overdue:
        return 0

    from app.models.user import user_companies
    from app.services.opsflow_extras import notify as _notify_bell
    from app.models import NotificationKind
    from flask import url_for
    rows = db.session.execute(
        user_companies.select().where(
            (user_companies.c.company_id == company_id) &
            (user_companies.c.role.in_(
                ["owner", "admin", "accountant"]))
        )
    ).fetchall()
    recipient_ids = {r.user_id for r in rows}

    for cust in overdue:
        days_late = ((today - cust.settlement_due_date).days
                     if cust.settlement_due_date else 0)
        try:
            link = url_for("custody.detail", custody_id=cust.id)
        except Exception:
            link = None
        for uid in recipient_ids:
            try:
                _notify_bell(
                    uid, company_id=company_id,
                    kind=NotificationKind.TASK_ASSIGNED,
                    title=(f"⏰ عهدة نقدية متأخرة: "
                           f"{cust.holder_name}"),
                    body=(f"متأخرة {days_late} يوم — "
                          f"{float(cust.amount_pending):.2f} معلَّق"),
                    link_url=link)
            except Exception:
                from flask import current_app
                current_app.logger.exception(
                    "custody overdue notify failed")
        cust.custody_overdue_notified_at = datetime.utcnow()
    db.session.commit()
    return len(overdue) * max(len(recipient_ids), 1)


def open_custodies_for_holder(holder_type, holder_id):
    """Any custody NOT yet SETTLED or CANCELLED. Used to refuse a
    second request while one is already outstanding."""
    q = CashCustody.query.filter(CashCustody.status.in_(
        (CustodyStatus.ISSUED, CustodyStatus.PARTIALLY_SETTLED)))
    if holder_type == CustodyHolderType.EMPLOYEE:
        return q.filter(CashCustody.employee_id == holder_id).all()
    if holder_type == CustodyHolderType.DEPARTMENT:
        return q.filter(CashCustody.department_id == holder_id).all()
    return []


# ═══════════ Holder resolution ══════════════════════════════════
def _resolve_holder(company_id, holder_type, holder_id):
    """Return the party row + label. Raises CustodyError on mismatch
    or termination."""
    if not isinstance(holder_type, CustodyHolderType):
        try:
            holder_type = CustodyHolderType(str(holder_type).upper())
        except ValueError:
            raise CustodyError("نوع الحامل غير صالح")

    if holder_type == CustodyHolderType.EMPLOYEE:
        emp = db.session.get(Employee, int(holder_id))
        if not emp or emp.company_id != company_id:
            raise CustodyError("الموظف غير موجود")
        # A terminated employee cannot hold a new custody. Existing
        # custodies on them can still be settled.
        try:
            if emp.status == EmployeeStatus.TERMINATED:
                raise CustodyError(
                    "لا يمكن صرف عهدة لموظف موقوف / مُنهى خدمته")
        except Exception:  # noqa: BLE001 — enum-value comparison variant
            if str(getattr(emp.status, "value", emp.status)) == "TERMINATED":
                raise CustodyError(
                    "لا يمكن صرف عهدة لموظف موقوف / مُنهى خدمته")
        return emp, holder_type
    else:
        dept = db.session.get(Department, int(holder_id))
        if not dept or dept.company_id != company_id:
            raise CustodyError("القسم غير موجود")
        if not dept.is_active:
            raise CustodyError("القسم غير نشط")
        return dept, holder_type


# ═══════════ Request lifecycle ══════════════════════════════════
def request_custody(company_id, holder_type, holder_id, amount,
                    purpose, needed_by_date=None, *, created_by=None):
    """Submit a request. Refuses if the holder already has a
    pending request or an open custody (one at a time)."""
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise CustodyError("المبلغ غير صالح")
    if amount <= 0:
        raise CustodyError("المبلغ يجب أن يكون أكبر من صفر")
    if not (purpose or "").strip():
        raise CustodyError("السبب مطلوب")

    holder, holder_type = _resolve_holder(company_id, holder_type, holder_id)

    # Refuse if the holder already has an open custody OR a pending
    # request. Same guard as advances but scoped to holder identity.
    if open_custodies_for_holder(holder_type, holder.id):
        raise CustodyError(
            f"{holder.name} لديه عهدة مفتوحة بالفعل — لازم تُقفَل قبل عهدة جديدة")
    q = CashCustodyRequest.query.filter_by(
        company_id=company_id, status=CustodyRequestStatus.PENDING)
    if holder_type == CustodyHolderType.EMPLOYEE:
        q = q.filter_by(employee_id=holder.id)
    else:
        q = q.filter_by(department_id=holder.id)
    if q.first():
        raise CustodyError("يوجد طلب عهدة قيد المراجعة بالفعل لهذا الحامل")

    req = CashCustodyRequest(
        company_id=company_id,
        holder_type=holder_type,
        employee_id=holder.id if holder_type == CustodyHolderType.EMPLOYEE else None,
        department_id=holder.id if holder_type == CustodyHolderType.DEPARTMENT else None,
        amount=amount,
        purpose=purpose.strip(),
        needed_by_date=needed_by_date,
        status=CustodyRequestStatus.PENDING,
        created_by=created_by,
    )
    db.session.add(req)
    db.session.commit()
    _notify_approvers(req)
    return req


def approve_custody_request(req, *, reviewer_id, issued_on=None,
                             payment_method_id=None,
                             settlement_due_date=None,
                             review_note=None,
                             amount=None):
    """Approve a PENDING request → real custody + issue journal.
    Delegates to issue_custody so both request-approval and direct-
    issue land in the same funnel.

    MARSOUD-CUSTODY-REQUEST-APPROVE-01 (2026-08-10) — `amount` grew
    from an implicit `req.amount` copy to an optional override so
    the accountant can approve a different figure than the
    employee requested (e.g. partial disbursement, or a higher
    figure if the employee undershot). Passing None keeps the old
    behaviour bit-for-bit — approves for exactly what was asked.
    The override lands on CashCustody.amount_issued only;
    req.amount stays untouched so the audit trail of what the
    employee originally requested is preserved.
    """
    if req.status != CustodyRequestStatus.PENDING:
        raise CustodyError("يمكن اعتماد الطلبات في حالة الانتظار فقط")

    from decimal import Decimal, InvalidOperation
    if amount is None:
        effective_amount = req.amount
    else:
        try:
            effective_amount = (amount if isinstance(amount, Decimal)
                                 else Decimal(str(amount)))
        except (InvalidOperation, TypeError):
            raise CustodyError("المبلغ غير صالح")
    if effective_amount <= 0:
        raise CustodyError("المبلغ يجب أن يكون أكبر من صفر")

    custody = issue_custody(
        req.company_id,
        holder_type=req.holder_type,
        holder_id=(req.employee_id
                   if req.holder_type == CustodyHolderType.EMPLOYEE
                   else req.department_id),
        amount=float(effective_amount),
        purpose=req.purpose,
        issued_on=issued_on or date.today(),
        settlement_due_date=settlement_due_date,
        payment_method_id=payment_method_id,
        actor_id=reviewer_id,
        request=req,
        note=review_note,
    )

    req.status = CustodyRequestStatus.APPROVED
    req.reviewed_by = reviewer_id
    req.reviewed_at = datetime.utcnow()
    if review_note:
        req.review_note = review_note
    db.session.commit()
    return custody


def reject_custody_request(req, *, reviewer_id, review_note=None):
    if req.status != CustodyRequestStatus.PENDING:
        raise CustodyError("يمكن رفض الطلبات في حالة الانتظار فقط")
    req.status = CustodyRequestStatus.REJECTED
    req.reviewed_by = reviewer_id
    req.reviewed_at = datetime.utcnow()
    if review_note:
        req.review_note = review_note
    db.session.commit()

    holder = req.holder
    user_id = getattr(holder, "user_id", None) if holder else None
    if user_id:
        _notify(user_id, req.company_id,
                title=f"تم رفض طلب العهدة ({float(req.amount):.2f})",
                body=(review_note or "").strip() or None)
    return req


# ═══════════ THE FUNNEL — issue_custody ═════════════════════════
def issue_custody(company_id, *, holder_type, holder_id, amount,
                   purpose, issued_on=None, settlement_due_date=None,
                   payment_method_id=None, actor_id=None,
                   request=None, note=None):
    """Create the CashCustody row and post the disbursement journal.

    The ONLY place a custody comes into existence. Both approve-
    request and accountant direct-issue paths land here."""
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise CustodyError("المبلغ غير صالح")
    if amount <= 0:
        raise CustodyError("المبلغ يجب أن يكون أكبر من صفر")

    holder, holder_type = _resolve_holder(
        company_id, holder_type, holder_id)

    # One open custody per holder. Same guard as advances scoped
    # differently.
    if open_custodies_for_holder(holder_type, holder.id):
        raise CustodyError(
            f"{holder.name} لديه عهدة مفتوحة بالفعل — لازم تُقفَل أولاً")

    issued_on = issued_on or date.today()

    # Where the money leaves from. Same resolution as advances, plus
    # MARSOUD-CUSTODY-BUGS-01 (2026-08-08) — cascading fallback so a
    # company whose seed pre-dated account 1110 (or whose super-admin
    # renamed / re-parented cash accounts) can still issue custody
    # without the accountant having to pick a payment method:
    #   1. explicit pick — the payment method the accountant chose
    #   2. the tenant's default PaymentMethod (points at 1110 out of
    #      the box; a super-admin can re-point without breaking us)
    #   3. any postable descendant of the Cash + Banks roots via the
    #      same walker the cash-flow statement + POS already use
    # Only raise when the company truly has no cash-side account.
    if payment_method_id:
        pm = db.session.get(PaymentMethod, int(payment_method_id))
        if not pm or pm.company_id != company_id or not pm.is_active:
            raise CustodyError("طريقة الصرف غير صالحة")
        pay_account = pm.account
        pay_label = pm.name_ar or pm.name
    else:
        default_pm = PaymentMethod.query.filter_by(
            company_id=company_id, is_default=True, is_active=True,
        ).first()
        if default_pm and default_pm.account:
            pay_account = default_pm.account
            pay_label = default_pm.name_ar or default_pm.name or "نقدي"
        else:
            from app.services.ledger import cash_accounts
            leaves = cash_accounts(company_id, active_only=True)
            pay_account = leaves[0] if leaves else None
            pay_label = pay_account.name_ar if pay_account else "نقدي"
    if not pay_account:
        raise CustodyError(
            "لا يوجد حساب نقدية أو بنك مُفعّل في شجرة الحسابات — "
            "أضف طريقة دفع أو حسابًا تحت 1110/1120 من الإعدادات")

    # Holder's 1180-NNNNNN sub-account, minted lazily.
    from app.services.subsidiary import party_custody_account
    try:
        custody_account = party_custody_account(holder)
    except ValueError as e:
        # Raised by create_party_subaccount when 1180 header missing.
        raise CustodyError(str(e))
    if not custody_account:
        raise CustodyError("حساب العهدة الفرعي غير متاح")

    custody = CashCustody(
        company_id=company_id,
        holder_type=holder_type,
        employee_id=holder.id if holder_type == CustodyHolderType.EMPLOYEE else None,
        department_id=holder.id if holder_type == CustodyHolderType.DEPARTMENT else None,
        amount_issued=amount,
        status=CustodyStatus.ISSUED,
        payment_method_id=int(payment_method_id) if payment_method_id else None,
        purpose=(purpose or "").strip() or None,
        issued_on=issued_on,
        settlement_due_date=settlement_due_date,
        request_id=request.id if request else None,
        note=note,
        approved_by=actor_id,
        created_by=actor_id,
    )
    db.session.add(custody)
    db.session.flush()   # need custody.id for the journal reference

    entry = post_journal(
        company_id=company_id,
        description=f"صرف عهدة نقدية — {holder.name} ({amount:.2f})",
        lines=[
            {"account_id": custody_account.id, "debit": amount, "credit": 0,
             "memo": f"عهدة — {holder.name}"},
            {"account_id": pay_account.id, "debit": 0, "credit": amount,
             "memo": f"صرف عهدة {pay_label} — {holder.name}"},
        ],
        entry_date=issued_on,
        reference=f"CUST-{custody.id}",
        created_by=actor_id,
        source_type="cash_custody",
        source_id=custody.id,
    )
    custody.journal_entry_id = entry.id
    db.session.commit()

    _notify_holder(
        custody, title=f"💵 تم صرف عهدة نقدية: {amount:.2f}",
        body=(f"للسبب: {custody.purpose or '—'}. "
              f"موعد التسوية: "
              f"{settlement_due_date.isoformat() if settlement_due_date else 'غير محدد'}"))
    _log(custody, "CREATE",
         f"صرف عهدة {amount:.2f} — {holder.name}")
    return custody


# ═══════════ Settlement lines (accumulate without posting) ═════
def add_settlement_line(custody, *, expense_account_id, amount,
                         receipt_note=None, actor_id=None):
    """Add an expense receipt to the custody. Does NOT post a
    journal — the journal is posted once at close_settlement,
    aggregating all lines together."""
    if custody.status not in (CustodyStatus.ISSUED,
                               CustodyStatus.PARTIALLY_SETTLED):
        raise CustodyError(
            "يمكن إضافة بند تسوية فقط لعهدة مفتوحة أو قيد التسوية")
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise CustodyError("المبلغ غير صالح")
    if amount <= 0:
        raise CustodyError("المبلغ يجب أن يكون أكبر من صفر")

    from app.models import Account
    acc = db.session.get(Account, int(expense_account_id))
    if not acc or acc.company_id != custody.company_id:
        raise CustodyError("حساب المصروف غير موجود")
    if not acc.is_postable:
        raise CustodyError(
            "حساب المصروف لا يقبل قيود مباشرة — اختر حساب فرعي")

    # Refuse over-settlement — don't let a caller queue lines whose
    # sum exceeds what was issued.
    existing = float(custody.amount_settled or 0)
    if existing + amount > float(custody.amount_issued or 0) + 0.005:
        raise CustodyError(
            f"مجموع بنود التسوية سيتجاوز المبلغ المصروف "
            f"({float(custody.amount_issued):.2f})")

    line = CashCustodySettlementLine(
        company_id=custody.company_id,
        custody_id=custody.id,
        expense_account_id=acc.id,
        amount=amount,
        receipt_note=(receipt_note or "").strip() or None,
        created_by=actor_id,
    )
    db.session.add(line)
    custody.amount_settled = round(existing + amount, 2)
    if custody.status == CustodyStatus.ISSUED:
        custody.status = CustodyStatus.PARTIALLY_SETTLED
    db.session.commit()
    return line


# ═══════════ close_settlement — the second journal ═════════════
def close_settlement(custody, *, actor_id, returned_amount=0,
                      shortfall_disposition=None,
                      settlement_date=None):
    """Post the settlement journal + finalize the custody.

    Validates `sum(lines) + returned + shortfall == amount_issued`
    BEFORE posting. Any drift means the accountant missed a line
    or typed a wrong return; refuse rather than silently produce
    a non-zero custody balance.

    Shortfall = amount_issued - sum(lines) - returned. Must be ≥ 0.
    Positive shortfall requires shortfall_disposition to be set:
      · EMPLOYEE_LIABILITY → push the residual to the employee's
        2130 sub-account (recover later via payroll deduction or
        a manual receipt). Only valid when holder is an employee.
      · EXPENSE → book as an operating expense under a lazily-
        created "عجز عهدة" account (5991). Loss absorbed by co."""
    if custody.status not in (CustodyStatus.ISSUED,
                               CustodyStatus.PARTIALLY_SETTLED):
        raise CustodyError("يمكن إقفال التسوية لعهدة مفتوحة فقط")

    try:
        returned_amount = round(float(returned_amount or 0), 2)
    except (TypeError, ValueError):
        raise CustodyError("مبلغ الفائض غير صالح")
    if returned_amount < 0:
        raise CustodyError("الفائض لا يمكن أن يكون سالباً")

    lines = list(custody.settlement_lines or [])
    lines_total = round(sum(float(l.amount or 0) for l in lines), 2)
    issued = round(float(custody.amount_issued or 0), 2)
    shortfall = round(issued - lines_total - returned_amount, 2)
    if shortfall < -0.005:
        raise CustodyError(
            f"البنود ({lines_total:.2f}) + الفائض ({returned_amount:.2f}) "
            f"تتجاوز المبلغ المصروف ({issued:.2f})")

    if shortfall > 0.005 and not shortfall_disposition:
        raise CustodyError(
            "لازم تحدد كيف يتم معالجة العجز "
            "(EMPLOYEE_LIABILITY أو EXPENSE)")

    if shortfall > 0.005:
        if not isinstance(shortfall_disposition, ShortfallDisposition):
            try:
                shortfall_disposition = ShortfallDisposition(
                    str(shortfall_disposition).upper())
            except ValueError:
                raise CustodyError("خيار معالجة العجز غير صالح")
        if (shortfall_disposition == ShortfallDisposition.EMPLOYEE_LIABILITY
                and custody.holder_type != CustodyHolderType.EMPLOYEE):
            raise CustodyError(
                "تحويل العجز لحساب موظف متاح فقط للعهد الفردية")

    # Resolve accounts we'll credit / debit.
    from app.models import Account
    custody_account = db.session.get(
        Account, custody_account_id_for(custody))
    if not custody_account:
        raise CustodyError("حساب العهدة الفرعي غير موجود")

    journal_lines = []
    # Aggregate lines by expense_account_id so a settlement with
    # three receipts on the same 5100 posts one Dr row, not three.
    by_account = {}
    for l in lines:
        by_account[l.expense_account_id] = round(
            by_account.get(l.expense_account_id, 0)
            + float(l.amount or 0), 2)
    for acc_id, amt in by_account.items():
        acc = db.session.get(Account, acc_id)
        journal_lines.append({
            "account_id": acc_id, "debit": amt, "credit": 0,
            "memo": f"مصروف عهدة — {custody.holder_name} "
                    f"({acc.code if acc else '?'})",
        })

    # Returned excess → Dr cash/bank / Cr custody_account.
    if returned_amount > 0.005:
        if custody.payment_method_id:
            pm = db.session.get(PaymentMethod, custody.payment_method_id)
            back_account = pm.account if pm else None
        else:
            back_account = get_account_by_code(custody.company_id, "1110")
        if not back_account:
            raise CustodyError("حساب النقدية/البنك غير متاح لإرجاع الفائض")
        journal_lines.append({
            "account_id": back_account.id, "debit": returned_amount,
            "credit": 0,
            "memo": f"استرداد فائض عهدة — {custody.holder_name}",
        })

    # Shortfall → Dr employee-liability OR "عجز عهدة" expense.
    if shortfall > 0.005:
        if shortfall_disposition == ShortfallDisposition.EMPLOYEE_LIABILITY:
            from app.services.subsidiary import party_payroll_account
            emp_account = party_payroll_account(custody.employee)
            journal_lines.append({
                "account_id": emp_account.id, "debit": shortfall,
                "credit": 0,
                "memo": f"عجز عهدة على ذمة الموظف — {custody.holder_name}",
            })
        else:  # EXPENSE
            expense_acc = _ensure_shortfall_expense_account(custody.company_id)
            journal_lines.append({
                "account_id": expense_acc.id, "debit": shortfall,
                "credit": 0,
                "memo": f"عجز عهدة — {custody.holder_name}",
            })

    # Cr custody sub-account for the full issued amount (settled +
    # returned + shortfall). This zeros the holder's 1180 leaf.
    journal_lines.append({
        "account_id": custody_account.id, "debit": 0,
        "credit": issued,
        "memo": f"إقفال عهدة — {custody.holder_name}",
    })

    entry = post_journal(
        company_id=custody.company_id,
        description=f"إقفال عهدة نقدية — {custody.holder_name} "
                     f"(بنود={lines_total:.2f}, فائض={returned_amount:.2f}, "
                     f"عجز={shortfall:.2f})",
        lines=journal_lines,
        entry_date=settlement_date or date.today(),
        reference=f"CUST-SET-{custody.id}",
        created_by=actor_id,
        source_type="cash_custody_settlement",
        source_id=custody.id,
    )

    custody.settlement_journal_entry_id = entry.id
    custody.amount_returned = returned_amount
    custody.amount_shortfall = shortfall
    custody.shortfall_disposition = shortfall_disposition if shortfall > 0.005 else None
    custody.status = CustodyStatus.SETTLED
    custody.settled_by = actor_id
    custody.settled_at = datetime.utcnow()
    custody.custody_overdue_notified_at = None  # clear so a re-issue doesn't dedup
    db.session.commit()

    _notify_holder(
        custody, title=f"تم إقفال العهدة ({issued:.2f})",
        body=(f"إجمالي المصروفات: {lines_total:.2f} · "
              f"فائض معاد: {returned_amount:.2f} · "
              f"عجز: {shortfall:.2f}"))
    _log(custody, "UPDATE",
         f"إقفال تسوية عهدة {issued:.2f} — {custody.holder_name}")
    return custody


# ═══════════ Cancel ═════════════════════════════════════════════
def cancel_custody(custody, *, actor_id, reason=None):
    """Reverse the disbursement journal and stop the custody.

    Refuses if any settlement line has been added — those receipts
    must be dealt with by close_settlement first (they can't be
    silently thrown away)."""
    if custody.status not in (CustodyStatus.ISSUED,
                               CustodyStatus.PARTIALLY_SETTLED):
        raise CustodyError("يمكن إلغاء العهد المفتوحة فقط")
    if float(custody.amount_settled or 0) > 0.005:
        raise CustodyError(
            "لا يمكن إلغاء عهدة عليها بنود تسوية — أقفل التسوية أولاً")

    if custody.journal_entry_id:
        entry = reverse_journal(custody.journal_entry_id,
                                 created_by=actor_id)
        custody.reversal_entry_id = entry.id
    # _undo_source_side_effects("cash_custody") also flips status;
    # setting it here is idempotent + safe for the no-journal edge case.
    custody.status = CustodyStatus.CANCELLED
    custody.cancelled_by = actor_id
    custody.cancelled_at = datetime.utcnow()
    custody.cancel_reason = (reason or "").strip() or None
    custody.custody_overdue_notified_at = None
    db.session.commit()

    _notify_holder(
        custody, title=f"تم إلغاء العهدة ({float(custody.amount_issued):.2f})",
        body=custody.cancel_reason)
    _log(custody, "UPDATE",
         f"إلغاء عهدة — {custody.holder_name}")
    return custody


# ═══════════ Helpers ════════════════════════════════════════════
def custody_account_id_for(custody):
    """Resolve the holder's 1180 sub-account id — lazily creates if
    the holder has no leaf yet (matches ensure_custody_account
    idempotency; used during close so a custody issued before the
    sub-account was minted still closes cleanly)."""
    holder = custody.holder
    if not holder:
        return None
    from app.services.subsidiary import ensure_custody_account
    acc = ensure_custody_account(holder)
    return acc.id if acc else None


def _ensure_shortfall_expense_account(company_id):
    """Return the "عجز عهدة" expense account, creating it under
    5990 (Other Operating Expenses) on first use. Lazy so the seed
    CoA doesn't need a fresh row for every company."""
    from app.models import Account, AccountType, NormalSide
    acc = Account.query.filter_by(
        company_id=company_id, code="5991").first()
    if acc:
        return acc
    parent = Account.query.filter_by(
        company_id=company_id, code="5990").first()
    if not parent:
        # Fall back to expense root if 5990 wasn't seeded.
        parent = Account.query.filter_by(
            company_id=company_id, code="5000").first()
    acc = Account(
        company_id=company_id, code="5991",
        name="Cash Custody Shortfall", name_ar="عجز عهدة نقدية",
        type=AccountType.EXPENSE,
        normal_side=NormalSide.DEBIT,
        parent_id=parent.id if parent else None,
        is_postable=True,
    )
    db.session.add(acc)
    db.session.flush()
    return acc


# ═══════════ Notifications ══════════════════════════════════════
def _notify(user_id, company_id, *, title, body=None, link_url=None):
    try:
        from app.services.opsflow_extras import notify
        from app.models import NotificationKind
        notify(user_id, company_id=company_id,
               kind=NotificationKind.TASK_ASSIGNED,
               title=title, body=body, link_url=link_url)
    except Exception:
        from flask import current_app
        current_app.logger.exception("custody notify failed")


def _notify_holder(custody, *, title, body=None):
    """Ping the employee-user tied to the custody. Department
    custodies have no direct user — skip silently (the accountant
    is already in the loop via _log)."""
    if custody.holder_type != CustodyHolderType.EMPLOYEE:
        return
    emp = custody.employee
    if not emp or not emp.user_id:
        return
    from flask import url_for
    try:
        link = url_for("portal_emp.custody_list")
    except Exception:
        link = None
    _notify(emp.user_id, custody.company_id,
            title=title, body=body, link_url=link)


def _notify_approvers(req):
    """Ping everyone who can act on the request (custody.manage roles)."""
    try:
        from flask import url_for
        from app.models.user import user_companies
        rows = db.session.execute(
            user_companies.select().where(
                (user_companies.c.company_id == req.company_id) &
                (user_companies.c.role.in_(["owner", "admin", "accountant"]))
            )
        ).fetchall()
        holder_name = req.holder_name
        try:
            link = url_for("custody.requests")
        except Exception:
            link = None
        for r in rows:
            _notify(r.user_id, req.company_id,
                    title=f"💵 طلب عهدة نقدية جديد: {holder_name}",
                    body=f"{float(req.amount):.2f}",
                    link_url=link)
    except Exception:
        from flask import current_app
        current_app.logger.exception("custody request notify failed")


def _log(custody, action_type, label):
    try:
        from app.services.activity import log_action
        log_action(action_type=action_type,
                   entity_type="cash_custody",
                   entity_id=custody.id, entity_label=label,
                   company_id=custody.company_id)
    except Exception:
        pass
