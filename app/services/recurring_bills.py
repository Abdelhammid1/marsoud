"""Recurring vendor bills — projection layer (no GL).

Templates live in `recurring_bills`; per-date overrides (SKIP/AMEND) in
`recurring_bill_overrides`. The "forecast" function expands templates
into virtual occurrences on the fly, so we never store a row per
projected occurrence — that would balloon over time.

This module NEVER posts a journal. The actual vendor bill is posted by
the user when it really arrives, using the existing vendor_bills flow.
"""
from datetime import date, timedelta
from app import db
from app.models import (
    RecurringBill, RecurringBillOverride, VendorBill,
    INTERVAL_UNITS, OVERRIDE_ACTIONS,
)


class RecurringBillError(Exception):
    """Raised by the recurring-bills service on validation errors."""


# ─── Creation + lifecycle ────────────────────────────────────────────
def create_recurring_from_bill(
    *, bill_id, interval_unit, interval_count, start_date, end_date,
    company_id, user_id,
):
    """Build a recurring template from an existing vendor bill.

    The vendor / amount / currency are copied from the source bill so
    the user doesn't have to re-enter them. The source bill itself is
    unchanged (it's a real, posted bill — we just remember it as the
    template seed).
    """
    if interval_unit not in INTERVAL_UNITS:
        raise RecurringBillError(f"interval_unit must be one of {INTERVAL_UNITS}")
    try:
        interval_count = int(interval_count)
    except (TypeError, ValueError):
        raise RecurringBillError("interval_count يجب أن يكون رقماً صحيحاً")
    if interval_count < 1:
        raise RecurringBillError("interval_count يجب أن يكون ≥ 1")

    bill = db.session.get(VendorBill, bill_id)
    if not bill or bill.company_id != company_id:
        raise RecurringBillError("الفاتورة غير موجودة أو من شركة أخرى")

    if end_date and end_date < start_date:
        raise RecurringBillError("تاريخ النهاية قبل تاريخ البداية")

    rb = RecurringBill(
        company_id=company_id,
        source_bill_id=bill.id,
        vendor_id=bill.vendor_id,
        amount=bill.total or 0,
        currency=bill.currency or "SAR",
        interval_unit=interval_unit,
        interval_count=interval_count,
        start_date=start_date,
        end_date=end_date,
        active=True,
        created_by=user_id,
    )
    db.session.add(rb)
    db.session.commit()
    return rb


def deactivate_recurring(recurring_bill_id, company_id):
    rb = db.session.get(RecurringBill, recurring_bill_id)
    if not rb or rb.company_id != company_id:
        raise RecurringBillError("غير موجود")
    rb.active = False
    db.session.commit()
    return rb


def set_override(*, recurring_bill_id, occurrence_date, action,
                 amount=None, company_id):
    if action not in OVERRIDE_ACTIONS:
        raise RecurringBillError(f"action must be one of {OVERRIDE_ACTIONS}")
    rb = db.session.get(RecurringBill, recurring_bill_id)
    if not rb or rb.company_id != company_id:
        raise RecurringBillError("غير موجود")
    # Upsert by (recurring_bill_id, occurrence_date)
    existing = RecurringBillOverride.query.filter_by(
        recurring_bill_id=rb.id, occurrence_date=occurrence_date,
    ).first()
    if existing:
        existing.action = action
        existing.amount = amount if action == "AMEND" else None
    else:
        db.session.add(RecurringBillOverride(
            company_id=company_id,
            recurring_bill_id=rb.id,
            occurrence_date=occurrence_date,
            action=action,
            amount=amount if action == "AMEND" else None,
        ))
    db.session.commit()


# ─── Date arithmetic ─────────────────────────────────────────────────
def _add_interval(d, unit, count):
    """Add `count` units to `d` and return the new date. Pure function."""
    count = int(count)
    if unit == "DAY":
        return d + timedelta(days=count)
    if unit == "WEEK":
        return d + timedelta(weeks=count)
    if unit == "MONTH":
        # naive month arithmetic that preserves the day-of-month when
        # possible, else clamps to the last day of the target month.
        month = d.month - 1 + count
        year = d.year + month // 12
        month = month % 12 + 1
        # Clamp day for short months (Feb in non-leap, etc.)
        from calendar import monthrange
        day = min(d.day, monthrange(year, month)[1])
        return date(year, month, day)
    if unit == "YEAR":
        try:
            return date(d.year + count, d.month, d.day)
        except ValueError:
            # Feb 29 → Mar 1 on non-leap years
            return date(d.year + count, d.month, 28)
    raise ValueError(f"unknown interval unit: {unit}")


# ─── Expansion ───────────────────────────────────────────────────────
def expand_occurrences(recurring_bill, range_start, range_end):
    """Return a list of dicts describing the occurrences of one template
    in the date range. Applies overrides:
      - SKIP   → occurrence omitted entirely
      - AMEND  → amount overridden for that occurrence
    Stops at end_date if set. Iterates with a safety cap of 10000
    occurrences to refuse pathologically tiny intervals over huge ranges.
    """
    if not recurring_bill.active:
        return []
    # Pull overrides for this template, keyed by occurrence date.
    overrides = {
        o.occurrence_date: o
        for o in RecurringBillOverride.query.filter_by(
            recurring_bill_id=recurring_bill.id
        ).all()
    }
    out = []
    cur = recurring_bill.start_date
    hard_end = recurring_bill.end_date
    cap = 10000
    while cap > 0:
        cap -= 1
        if hard_end and cur > hard_end:
            break
        if cur > range_end:
            break
        if cur >= range_start:
            ov = overrides.get(cur)
            if ov and ov.action == "SKIP":
                pass   # skipped per override
            else:
                amount = (float(ov.amount) if (ov and ov.action == "AMEND"
                                                 and ov.amount is not None)
                          else float(recurring_bill.amount or 0))
                out.append({
                    "recurring_bill_id": recurring_bill.id,
                    "source_bill_id": recurring_bill.source_bill_id,
                    "vendor_id": recurring_bill.vendor_id,
                    "vendor_name": (recurring_bill.vendor.name
                                     if recurring_bill.vendor else "—"),
                    "date": cur,
                    "amount": amount,
                    "currency": recurring_bill.currency,
                    "is_amended": bool(ov and ov.action == "AMEND"),
                    "template_label": recurring_bill.label_ar,
                })
        cur = _add_interval(cur, recurring_bill.interval_unit,
                            recurring_bill.interval_count)
    return out


# ─── Forecast (aggregate across templates) ───────────────────────────
def forecast(company_id, range_start, range_end):
    """Return all projected vendor-bill occurrences for the date range
    across every active template in the company, sorted ascending.
    Includes a totals dict aggregated per currency."""
    templates = RecurringBill.query.filter_by(
        company_id=company_id, active=True,
    ).all()
    rows = []
    for t in templates:
        rows.extend(expand_occurrences(t, range_start, range_end))
    rows.sort(key=lambda r: r["date"])
    totals = {}
    for r in rows:
        totals[r["currency"]] = totals.get(r["currency"], 0.0) + r["amount"]
    return {"rows": rows, "totals": totals,
            "range_start": range_start, "range_end": range_end}


def get_due_within(company_id, days=7):
    today = date.today()
    return forecast(company_id, today, today + timedelta(days=days))
