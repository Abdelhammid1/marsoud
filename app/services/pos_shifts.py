"""MARSOUD-ERP-01 Phase 3 — cashier shifts.

When `Company.shift_required_for_pos == True`, a cashier must have an
OPEN shift before the POS register works. Each POS order tags
invoice.shift_id with the open shift's id. At close, the shift summary
shows expected vs actual cash so the drawer can be reconciled.
"""
from datetime import datetime

from app import db
from app.models import (
    CashierShift, CashierShiftStatus, Invoice, InvoiceStatus, Payment,
    PaymentMethod, Account, Company,
)


class ShiftError(Exception):
    """Raised when a shift operation can't complete."""


def open_shift(*, company_id, cashier_id, opening_cash=0, notes=None):
    """Refuse if cashier already has an open shift."""
    existing = CashierShift.query.filter_by(
        company_id=company_id, cashier_id=cashier_id,
        status=CashierShiftStatus.OPEN.value,
    ).first()
    if existing:
        raise ShiftError(
            f"يوجد وردية مفتوحة بالفعل (#{existing.id}). أغلقها أولاً."
        )
    sh = CashierShift(
        company_id=company_id,
        cashier_id=cashier_id,
        opening_cash=float(opening_cash or 0),
        notes=(notes or "").strip() or None,
        status=CashierShiftStatus.OPEN.value,
    )
    db.session.add(sh)
    db.session.commit()
    return sh


def close_shift(shift, *, closing_cash, notes=None):
    """Compute expected cash + variance, flip status to CLOSED."""
    if shift.status != CashierShiftStatus.OPEN.value:
        raise ShiftError("الوردية مقفلة بالفعل")
    expected = _expected_cash_for(shift)
    variance = float(closing_cash or 0) - expected
    shift.closing_cash = float(closing_cash or 0)
    shift.expected_cash = expected
    shift.variance = variance
    if notes:
        shift.notes = (shift.notes or "") + "\n" + notes
    shift.closed_at = datetime.now()
    shift.status = CashierShiftStatus.CLOSED.value
    db.session.commit()
    return shift


def current_open_shift_for(user_id, company_id):
    return CashierShift.query.filter_by(
        company_id=company_id, cashier_id=user_id,
        status=CashierShiftStatus.OPEN.value,
    ).order_by(CashierShift.opened_at.desc()).first()


def shift_required(company_id):
    co = Company.query.get(company_id)
    return bool(getattr(co, "shift_required_for_pos", False))


# ─── Internal: expected cash math ────────────────────────────────────────
def _expected_cash_for(shift):
    """opening_cash + Σ(cash payments on this shift's non-voided POS orders).

    A cash payment is identified by the PaymentMethod row's account_id
    pointing to 1110 (cash). That handles companies with multiple cash
    methods (drawer, petty, etc.) — only the cash family counts toward
    drawer reconciliation.
    """
    opening = float(shift.opening_cash or 0)
    pms = PaymentMethod.query.filter_by(company_id=shift.company_id).all()
    cash_pm_ids = set()
    for pm in pms:
        acc = db.session.get(Account, pm.account_id) if pm.account_id else None
        if acc and acc.code == "1110":
            cash_pm_ids.add(pm.id)

    invs = Invoice.query.filter(
        Invoice.company_id == shift.company_id,
        Invoice.shift_id == shift.id,
        Invoice.status != InvoiceStatus.VOIDED,
    ).all()
    cash_in = 0.0
    for inv in invs:
        for pay in inv.payments:
            if pay.payment_method_id in cash_pm_ids:
                cash_in += float(pay.amount or 0)
    return round(opening + cash_in, 2)


# ─── Shift Z-report (used by the detail page) ────────────────────────────
def shift_summary(shift):
    invs = Invoice.query.filter(
        Invoice.company_id == shift.company_id,
        Invoice.shift_id == shift.id,
    ).all()
    orders = 0
    voids = 0
    gross = 0.0
    net = 0.0
    by_method = {}
    for inv in invs:
        orders += 1
        if inv.is_voided:
            voids += 1
            continue
        gross += float(inv.total or 0)
        net += float(inv.total or 0)
        for pay in inv.payments:
            mname = pay.payment_method.name_ar if pay.payment_method else (pay.method or "غير محدد")
            by_method[mname] = by_method.get(mname, 0) + float(pay.amount or 0)
    return {
        "orders": orders, "voids": voids,
        "gross": gross, "net": net,
        "by_method": by_method,
    }
