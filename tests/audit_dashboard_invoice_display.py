#!/usr/bin/env python3
"""MARSOUD-DASHBOARD-INVOICE-TITLE (Abdelhamid 2026-07-29).

Dashboard late-invoices panel showed only invoice.number as
subtitle — users couldn't tell invoices apart. Ticket wants a
descriptive fallback: notes[:60] → number.

Checks:
  1. Invoice with `notes` set → title_for_display = notes trimmed
     to 60 chars.
  2. Invoice with empty notes → title_for_display = invoice.number.
  3. Invoice with whitespace-only notes → title_for_display =
     invoice.number (whitespace isn't a real title).
  4. Long notes (>60 chars) → truncated to 60.
"""
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__DIT_%__'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})


def _bootstrap():
    from app.models import (
        Company, Customer, Invoice, InvoiceStatus, InvoiceItem,
    )
    from app.services.seed_coa import seed_default_coa
    c = Company(name="__DIT_CO__", base_currency="EGP",
                 subdomain="dit-co",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    cust = Customer(company_id=c.id, name="عميل الاختبار")
    db.session.add(cust); db.session.commit()
    return c, cust


def _mk_invoice(company, customer, notes, number):
    from app.models import Invoice, InvoiceStatus, InvoiceItem
    inv = Invoice(
        company_id=company.id, customer_id=customer.id,
        number=number,
        issue_date=date.today() - timedelta(days=60),
        due_date=date.today() - timedelta(days=30),   # overdue
        currency="EGP", tax_rate=Decimal("0.00"),
        status=InvoiceStatus.SENT,
        total=Decimal("1000"), paid_amount=Decimal("0"),
        notes=notes,
    )
    db.session.add(inv); db.session.commit()
    return inv


@check("1. Invoice with notes → title_for_display = notes")
def _():
    from app.services.reports import dashboard_metrics
    _teardown()
    c, cust = _bootstrap()
    _mk_invoice(c, cust, "دفعة أولى لعقد الصيانة السنوي", "IT-101")
    metrics = dashboard_metrics(c.id, period="month")
    matches = [x for x in metrics["late_invoices"]
               if x["number"] == "IT-101"]
    assert matches, "IT-101 not in late_invoices"
    assert matches[0]["title_for_display"] == \
        "دفعة أولى لعقد الصيانة السنوي", \
        f"got {matches[0]['title_for_display']!r}"
    return "notes surfaces as title"


@check("2. Invoice with empty notes → title = number")
def _():
    from app.services.reports import dashboard_metrics
    _teardown()
    c, cust = _bootstrap()
    _mk_invoice(c, cust, "", "IT-102")
    metrics = dashboard_metrics(c.id, period="month")
    matches = [x for x in metrics["late_invoices"]
               if x["number"] == "IT-102"]
    assert matches
    assert matches[0]["title_for_display"] == "IT-102"
    return "empty notes → number"


@check("3. Whitespace-only notes → title = number")
def _():
    from app.services.reports import dashboard_metrics
    _teardown()
    c, cust = _bootstrap()
    _mk_invoice(c, cust, "   \n\t  ", "IT-103")
    metrics = dashboard_metrics(c.id, period="month")
    matches = [x for x in metrics["late_invoices"]
               if x["number"] == "IT-103"]
    assert matches
    assert matches[0]["title_for_display"] == "IT-103", \
        f"got {matches[0]['title_for_display']!r}"
    return "whitespace-only → number"


@check("4. Long notes → truncated to 60 chars")
def _():
    from app.services.reports import dashboard_metrics
    _teardown()
    c, cust = _bootstrap()
    long_notes = "أ" * 100
    _mk_invoice(c, cust, long_notes, "IT-104")
    metrics = dashboard_metrics(c.id, period="month")
    matches = [x for x in metrics["late_invoices"]
               if x["number"] == "IT-104"]
    assert matches
    assert len(matches[0]["title_for_display"]) == 60, \
        f"len={len(matches[0]['title_for_display'])}"
    return "trimmed to 60"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _teardown()
            for label, fn in CHECKS:
                try:
                    res = fn()
                    print(f"PASS  {label}  ⇒ {res}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback; traceback.print_exc()
        finally:
            _teardown()
            print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
