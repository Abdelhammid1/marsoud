"""MARSOUD-TKT-HR-DECISIONS-01 (2026-09-02) — قرارات الموظفين service.

Four public functions:
  * `create_decision` — validate + persist as DRAFT.
  * `execute_decision` — DRAFT → EXECUTED (or PENDING_PAYROLL for
    financial-next-payroll). Dispatches by kind. TERMINATION delegates
    to the existing `terminate_employee` so the same pro-rating in
    `billable_days_in_period` applies to mid-month leavers — AC #13.
  * `cancel_decision` — DRAFT / PENDING_PAYROLL → CANCELLED with
    mandatory reason. Refuses on EXECUTED (AC #8).
  * `list_decisions` — filtered listing for the tab + index page.

Deliberately does NOT touch `run_payroll` or mutate Employee fields on
approve — that's Phase 2's job. This keeps Phase 1's diff small +
prevents drift into audited payroll code.
"""
from datetime import datetime, date
from app import db
from app.models import (
    HrDecision, HrDecisionKind, HrDecisionStatus, HrDecisionTiming,
    Employee, EmployeeStatus, TerminationReason,
    hr_decision_category,
)
from app.services.ledger import (
    LedgerError, post_journal, get_account_by_code,
    resolve_financial_account,
)


class HrDecisionError(Exception):
    """Domain-level validation failure. Route catches + flashes."""


_ALLOWED_KIND_VALUES = {k.value for k in HrDecisionKind}
_ALLOWED_TIMING_VALUES = {t.value for t in HrDecisionTiming}


# ─── Reason mapping for terminate_employee ─────────────────────
def _termination_reason(body):
    """Map a free-text body to a TerminationReason enum. Defaults to
    OTHER so `terminate_employee` never chokes on a missing enum. If
    the body contains an obvious keyword, we take it, but this is a
    convenience — auditors read `body`/`notes`, not the enum."""
    b = (body or "").lower()
    if any(w in b for w in ("استقال", "resign")):
        return TerminationReason.RESIGNATION
    if any(w in b for w in ("فصل", "dismis")):
        return TerminationReason.DISMISSAL
    if any(w in b for w in ("انتهاء", "contract")):
        return TerminationReason.CONTRACT_END
    return TerminationReason.OTHER


# ─── Create ─────────────────────────────────────────────────────
def create_decision(company_id, *, employee_id, kind, effective_date,
                     title, body=None, reference=None,
                     timing="IMMEDIATE",
                     amount=None, payment_account_id=None,
                     actor_id=None):
    """Persist a new DRAFT decision. Every guard needed to keep an
    executed decision internally consistent is checked HERE — the
    execute step must be able to trust the row.
    """
    # ─ Kind
    if kind not in _ALLOWED_KIND_VALUES:
        raise HrDecisionError(f"نوع قرار غير معروف: {kind}")
    kind_enum = HrDecisionKind(kind)
    cat = hr_decision_category(kind_enum)

    # ─ Timing
    if timing not in _ALLOWED_TIMING_VALUES:
        raise HrDecisionError("توقيت غير معروف")
    if cat != "FINANCIAL":
        # Only financial decisions have a meaningful timing.
        # Force IMMEDIATE so downstream never guesses.
        timing = HrDecisionTiming.IMMEDIATE.value

    # ─ Employee + tenant match
    emp = db.session.get(Employee, int(employee_id))
    if not emp or emp.company_id != company_id:
        raise HrDecisionError("الموظف غير موجود")
    if (emp.status == EmployeeStatus.TERMINATED
            and kind_enum != HrDecisionKind.TERMINATION):
        raise HrDecisionError(
            "الموظف مُنهى خدمته — لا يمكن اتخاذ قرارات جديدة عليه")

    # ─ Title (required for everyone)
    title = (title or "").strip()
    if not title:
        raise HrDecisionError("عنوان القرار مطلوب")

    # ─ Body — reason mandatory for penalties / warnings / terminations
    body_val = (body or "").strip() or None
    if kind_enum in (HrDecisionKind.PENALTY, HrDecisionKind.WARNING,
                      HrDecisionKind.TERMINATION):
        if not body_val:
            raise HrDecisionError("سبب القرار مطلوب لهذا النوع")

    # ─ Effective date
    eff = effective_date
    if isinstance(eff, str):
        try:
            eff = datetime.strptime(eff, "%Y-%m-%d").date()
        except ValueError:
            raise HrDecisionError("تاريخ التنفيذ غير صالح") from None
    if not isinstance(eff, date):
        raise HrDecisionError("تاريخ التنفيذ مطلوب")

    # ─ Financial amount + payment account
    amt_val = None
    pay_acc_id = None
    if cat == "FINANCIAL":
        try:
            amt_val = float(amount or 0)
        except (TypeError, ValueError):
            raise HrDecisionError("المبلغ غير صالح") from None
        if amt_val <= 0:
            raise HrDecisionError("المبلغ يجب أن يكون أكبر من صفر")
        if timing == HrDecisionTiming.IMMEDIATE.value:
            # Immediate → posts a JE at execute-time; the account is
            # needed. NEXT_PAYROLL doesn't need one — Phase 2 folds
            # into the payroll JE.
            if not payment_account_id:
                raise HrDecisionError(
                    "اختر الخزينة أو البنك المستخدم في العملية")
            acc, _ = resolve_financial_account(
                company_id, payment_account_id)
            pay_acc_id = acc.id

    # ─ Persist
    dec = HrDecision(
        company_id=company_id,
        employee_id=emp.id,
        kind=kind_enum.value,
        status=HrDecisionStatus.DRAFT.value,
        timing=timing,
        effective_date=eff,
        amount=amt_val,
        payment_account_id=pay_acc_id,
        title=title,
        body=body_val,
        reference=(reference or "").strip() or None,
        created_by=actor_id,
    )
    db.session.add(dec)
    db.session.commit()

    _log(dec, action_type="CREATE",
         extra={"kind": dec.kind, "category": cat,
                "timing": dec.timing,
                "amount": amt_val, "effective_date": str(eff)})
    return dec


# ─── Execute ─────────────────────────────────────────────────────
def execute_decision(dec, *, actor_id=None):
    """Post the side-effect. Dispatch by kind + timing. Idempotent:
    refuses to re-execute an already-EXECUTED / PENDING_PAYROLL /
    CANCELLED row.
    """
    if dec.status != HrDecisionStatus.DRAFT.value:
        raise HrDecisionError(
            f"لا يمكن تنفيذ قرار في حالة {dec.status_ar}")

    cat = dec.category
    kind_enum = dec.kind_enum

    # ─ TERMINATION → delegate to the existing service (AC #13)
    if cat == "TERMINATION":
        from app.services.payroll import terminate_employee
        emp = db.session.get(Employee, dec.employee_id)
        if not emp:
            raise HrDecisionError("الموظف غير موجود")
        terminate_employee(
            emp,
            reason=_termination_reason(dec.body),
            termination_date=dec.effective_date,
            notes=(dec.body or dec.reference or None),
        )
        dec.status = HrDecisionStatus.EXECUTED.value
        dec.executed_by = actor_id
        dec.executed_at = datetime.utcnow()
        db.session.commit()
        _log(dec, action_type="UPDATE",
             extra={"outcome": "executed", "delegated_to": "terminate_employee"})
        return dec

    # ─ FINANCIAL — pending goes on the queue for Phase 2
    if cat == "FINANCIAL":
        if dec.timing == HrDecisionTiming.NEXT_PAYROLL.value:
            # AC #3 — do NOT post a JE. Just flip status.
            dec.status = HrDecisionStatus.PENDING_PAYROLL.value
            dec.executed_by = actor_id
            dec.executed_at = datetime.utcnow()
            db.session.commit()
            _log(dec, action_type="UPDATE",
                 extra={"outcome": "queued_for_payroll"})
            return dec

        # Immediate — post the JE now (AC #5)
        if not dec.payment_account_id or not dec.amount:
            raise HrDecisionError(
                "قرار مالي فوري بدون حساب أو مبلغ — راجع الإدخال")
        amt = float(dec.amount)
        emp = db.session.get(Employee, dec.employee_id)
        emp_name = emp.name if emp else "موظف"

        if kind_enum == HrDecisionKind.BONUS:
            # Dr 5220 مكافآت / Cr cash|bank
            bonus_acc = (get_account_by_code(dec.company_id, "5220")
                         or get_account_by_code(dec.company_id, "5200"))
            if not bonus_acc:
                raise HrDecisionError(
                    "حساب المكافآت (5220) غير موجود في دليل الحسابات")
            desc = f"مكافأة {emp_name} — {dec.title}"
            entry = post_journal(
                company_id=dec.company_id,
                description=desc,
                lines=[
                    {"account_id": bonus_acc.id, "debit": amt, "credit": 0},
                    {"account_id": dec.payment_account_id,
                     "debit": 0, "credit": amt},
                ],
                entry_date=dec.effective_date,
                created_by=actor_id,
                source_type="hr_decision",
                source_id=dec.id,
            )
        else:  # PENALTY — cash received now
            # Dr cash|bank / Cr 4500 إيرادات أخرى.
            # AC #4 permits mapping to either revenue or a liability
            # depending on company policy; 4500 is the safe default,
            # the body line documents the reason.
            inc_acc = get_account_by_code(dec.company_id, "4500")
            if not inc_acc:
                raise HrDecisionError(
                    "حساب الإيرادات الأخرى (4500) غير موجود")
            desc = f"جزاء مالي — {emp_name} — {dec.title}"
            entry = post_journal(
                company_id=dec.company_id,
                description=desc,
                lines=[
                    {"account_id": dec.payment_account_id,
                     "debit": amt, "credit": 0},
                    {"account_id": inc_acc.id, "debit": 0, "credit": amt},
                ],
                entry_date=dec.effective_date,
                created_by=actor_id,
                source_type="hr_decision",
                source_id=dec.id,
            )

        dec.journal_entry_id = entry.id
        dec.status = HrDecisionStatus.EXECUTED.value
        dec.executed_by = actor_id
        dec.executed_at = datetime.utcnow()
        db.session.commit()
        _log(dec, action_type="UPDATE",
             extra={"outcome": "executed", "journal_entry_id": entry.id,
                    "amount": amt})
        return dec

    # ─ ADMIN — record-only in Phase 1 (AC #1: لا قيد)
    dec.status = HrDecisionStatus.EXECUTED.value
    dec.executed_by = actor_id
    dec.executed_at = datetime.utcnow()
    db.session.commit()
    _log(dec, action_type="UPDATE",
         extra={"outcome": "executed_admin_record_only"})
    return dec


# ─── Cancel ─────────────────────────────────────────────────────
def cancel_decision(dec, *, reason, actor_id=None):
    """DRAFT / PENDING_PAYROLL → CANCELLED. Reason is mandatory.

    An EXECUTED decision is IMMUTABLE by ticket AC #8 — "أي تصحيح لازم
    يبقى بقرار عكسي جديد". This function refuses; the operator must
    create a new inverse decision instead.
    """
    reason = (reason or "").strip()
    if not reason:
        raise HrDecisionError("سبب الإلغاء مطلوب")
    if dec.status == HrDecisionStatus.EXECUTED.value:
        raise HrDecisionError(
            "لا يمكن إلغاء قرار منفّذ — اتخذ قرار عكسي جديد بدلاً من ذلك")
    if dec.status == HrDecisionStatus.CANCELLED.value:
        raise HrDecisionError("هذا القرار ملغى بالفعل")

    dec.status = HrDecisionStatus.CANCELLED.value
    dec.cancelled_by = actor_id
    dec.cancelled_at = datetime.utcnow()
    dec.cancel_reason = reason
    db.session.commit()
    _log(dec, action_type="DELETE",
         extra={"outcome": "cancelled", "reason": reason})
    return dec


# ─── List ───────────────────────────────────────────────────────
def list_decisions(company_id, *, employee_id=None, status=None,
                    kind=None, order="desc", limit=None):
    q = HrDecision.query.filter_by(company_id=company_id)
    if employee_id:
        q = q.filter_by(employee_id=int(employee_id))
    if status:
        q = q.filter_by(status=status)
    if kind:
        q = q.filter_by(kind=kind)
    order_col = HrDecision.created_at
    q = q.order_by(order_col.desc() if order == "desc"
                    else order_col.asc())
    if limit:
        q = q.limit(int(limit))
    return q.all()


# ─── Internal ───────────────────────────────────────────────────
def _log(dec, *, action_type, extra=None):
    """Wrap `activity.log_action` — never breaks the business action."""
    try:
        from app.services.activity import log_action
        emp = getattr(dec, "employee", None)
        emp_name = emp.name if emp else "?"
        log_action(
            action_type=action_type,
            entity_type="hr_decision",
            entity_id=dec.id,
            entity_label=f"قرار {dec.kind_ar} — {emp_name}",
            company_id=dec.company_id,
            extra_data=extra or {},
        )
    except Exception:
        pass
