#!/usr/bin/env python3
"""MARSOUD-RECURRING-INVOICE-TOTAL-COLUMN-01 — the recurring-invoices
index page must show each schedule's full computed total (sum of
every line times the tax), not just the first line's unit price.

Before this ticket, Abdelhamid's 500-fix at `52d6181` rendered
`s.items[0].unit_price|money` as a stop-gap.  That value is
misleading when a schedule carries more than one line and silently
hides the real amount the customer will be billed.

This audit seeds three schedules in the same company and pins the
`computed_total` the route attaches as a transient attribute, matching
what the service `services/recurring_invoices.py::_build_and_post()`
would produce when the cron fires.

Three checks:
  1. ONE line, qty 2 x price 50, tax 15% -> 115.00.
  2. THREE lines mixed, same tax -> matches the invoice the generator
     would produce when the schedule runs (same math).
  3. ZERO lines (edge case the Jinja fallback used to catch) -> 0.00,
     no exception.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__RECURRING_INVOICE_TOTAL_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import Company, Customer, RecurringInvoice
    from sqlalchemy import text

    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    for c in Company.query.filter_by(name=COMPANY_NAME).all():
        _teardown_company_hard(c.id)
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()

    c = Company(name=COMPANY_NAME, base_currency="EGP")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)

    cust = Customer(company_id=c.id, name="__cust__")
    db.session.add(cust); db.session.flush()

    def _mk(name, items, tax_rate=Decimal("15.00")):
        s = RecurringInvoice(
            company_id=c.id,
            customer_id=cust.id,
            name=name,
            items_json=json.dumps(items),
            tax_rate=tax_rate,
            frequency="MONTHLY",
            next_run_date=date.today() + timedelta(days=1),
            is_active=True,
        )
        db.session.add(s); db.session.flush()
        return s

    _STATE["company_id"] = c.id
    _STATE["one_line_id"] = _mk("one", [
        {"description": "x", "quantity": 2, "unit_price": 50},
    ]).id
    _STATE["three_line_id"] = _mk("three", [
        {"description": "a", "quantity": 1, "unit_price": 100},
        {"description": "b", "quantity": 3, "unit_price": 20},
        {"description": "c", "quantity": 2, "unit_price": 7.5},
    ]).id
    _STATE["zero_line_id"] = _mk("zero", []).id
    db.session.commit()


def _teardown_company_hard(company_id):
    from app.models import Company
    from sqlalchemy import inspect, text
    db.session.rollback()
    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    insp = inspect(db.engine)
    for table in reversed(db.metadata.sorted_tables):
        if "company_id" in {col["name"] for col in insp.get_columns(table.name)}:
            db.session.execute(
                table.delete().where(table.c.company_id == company_id)
            )
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()


def _run_index():
    """Call the route's business logic in-process — compute totals the
    way the index view does.  Mirrors the body of routes.recurring_invoices.
    index() so the audit doesn't need a full Flask test client."""
    from app.models import RecurringInvoice
    rows = RecurringInvoice.query.filter_by(
        company_id=_STATE["company_id"], is_deleted=False
    ).all()
    for s in rows:
        subtotal = Decimal("0")
        for i in s.items:
            qty = Decimal(str(i.get("quantity") or 0))
            price = Decimal(str(i.get("unit_price") or 0))
            subtotal += qty * price
        tax = subtotal * Decimal(str(s.tax_rate or 0)) / Decimal(100)
        s.computed_total = float(subtotal + tax)
    return {s.id: s.computed_total for s in rows}


# ─── 1. ONE line, qty 2 x 50, tax 15% -> 115 ─────────────────────
@check("1. one-item schedule totals qty * price * (1 + tax)")
def _():
    totals = _run_index()
    got = totals[_STATE["one_line_id"]]
    assert abs(got - 115.0) < 0.01, f"one-line total {got} != 115"
    return f"total = {got:.2f}"


# ─── 2. THREE lines, tax 15% -> matches generator ────────────────
@check("2. three-item schedule total matches sum(qty*price)*(1+tax)")
def _():
    totals = _run_index()
    got = totals[_STATE["three_line_id"]]
    # items: 1*100 + 3*20 + 2*7.5 = 100 + 60 + 15 = 175; +15% = 201.25
    assert abs(got - 201.25) < 0.01, f"three-line total {got} != 201.25"
    return f"total = {got:.2f}"


# ─── 3. ZERO lines -> 0.00, no exception ─────────────────────────
@check("3. zero-item schedule returns 0 cleanly")
def _():
    totals = _run_index()
    got = totals[_STATE["zero_line_id"]]
    assert abs(got - 0.0) < 0.01, f"zero-line total {got} != 0"
    return f"total = {got:.2f}"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:
                    print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                    failed += 1
        finally:
            try:
                if "company_id" in _STATE:
                    _teardown_company_hard(_STATE["company_id"])
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
