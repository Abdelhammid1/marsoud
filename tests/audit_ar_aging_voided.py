#!/usr/bin/env python3
"""MARSOUD-AR-AGING-VOIDED (Abdelhamid 2026-07-30).

Batch 8 Ticket 1. `aging_report()` excluded PAID / CANCELLED /
REFUNDED but forgot VOIDED. Voided invoices had their JE
reversed (so ledger balance = 0) but still counted as customer
debt in the AR aging output — mismatch with the trial balance
shown at the bottom of the report.

Checks:
  1. Voided posted invoice does NOT show up in aging_report.
  2. Voided invoice's customer total drops by the invoice
     amount vs the pre-fix baseline (approximated via a
     non-voided sibling for comparison).
  3. Non-voided invoices for the same customer stay untouched.
  4. Sparkline helper `_ar_balance_as_of` also excludes VOIDED
     (same fix applied there).
  5. `dashboard_metrics.unpaid` (positive-list filter) already
     excluded VOIDED — regression guard so no accidental change.
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
    db.session.rollback()
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__ARV_%__'"))]
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


def _bootstrap(suffix):
    """Create a company + one customer + two SENT invoices."""
    from app.models import Company, Customer, Invoice, InvoiceStatus
    from app.models.invoice import InvoiceItem
    from app.services.seed_coa import seed_default_coa
    c = Company(name=f"__ARV_{suffix}__", base_currency="EGP",
                 subdomain=f"arv-{suffix.lower()}",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    cust = Customer(company_id=c.id, name="Test Customer")
    db.session.add(cust); db.session.flush()
    from app.services.subsidiary import ensure_customer_account
    ensure_customer_account(cust)

    invs = []
    for i, amount in enumerate([1000, 500], start=1):
        inv = Invoice(company_id=c.id, customer_id=cust.id,
                       number=f"INV-ARV-{suffix}-{i}",
                       issue_date=date.today() - timedelta(days=45),
                       due_date=date.today() - timedelta(days=15),
                       currency="EGP", tax_rate=0,
                       status=InvoiceStatus.SENT,
                       source="MANUAL")
        db.session.add(inv); db.session.flush()
        db.session.add(InvoiceItem(
            invoice_id=inv.id, company_id=c.id,
            description=f"line {i}", quantity=1,
            unit_price=amount))
        inv.recalc()
        invs.append(inv)
    db.session.commit()
    return c, cust, invs


@check("1. VOIDED invoice does NOT appear in aging_report")
def _():
    from app.services.reports import aging_report
    from app.models import InvoiceStatus
    _teardown()
    c, cust, invs = _bootstrap("A")
    # Void one invoice.
    invs[0].status = InvoiceStatus.VOIDED
    invs[0].voided_at = datetime.utcnow()
    db.session.commit()
    report = aging_report(c.id)
    row = next((r for r in report["rows"]
                 if r["customer_id"] == cust.id), None)
    assert row is not None, "customer missing from aging report"
    # Total should be JUST the non-voided invoice (500).
    assert abs(row["total"] - 500.0) < 0.01, \
        f"expected 500, got {row['total']}"
    return f"customer total = {row['total']} (voided 1000 excluded)"


@check("2. All-voided customer disappears from aging_report entirely")
def _():
    from app.services.reports import aging_report
    from app.models import InvoiceStatus
    _teardown()
    c, cust, invs = _bootstrap("B")
    for inv in invs:
        inv.status = InvoiceStatus.VOIDED
        inv.voided_at = datetime.utcnow()
    db.session.commit()
    report = aging_report(c.id)
    row = next((r for r in report["rows"]
                 if r["customer_id"] == cust.id), None)
    assert row is None, \
        f"customer with only voided invoices leaked: {row}"
    return "customer with only voided invoices excluded"


@check("3. Non-voided invoices for the same customer stay intact")
def _():
    from app.services.reports import aging_report
    from app.models import InvoiceStatus
    _teardown()
    c, cust, invs = _bootstrap("C")
    # Don't void anything.
    report = aging_report(c.id)
    row = next((r for r in report["rows"]
                 if r["customer_id"] == cust.id), None)
    assert row is not None
    assert abs(row["total"] - 1500.0) < 0.01, \
        f"expected 1500, got {row['total']}"
    return f"customer total = {row['total']} unchanged"


@check("4. _ar_balance_as_of() also excludes VOIDED")
def _():
    from app.services.reports import _ar_balance_as_of
    from app.models import InvoiceStatus
    _teardown()
    c, cust, invs = _bootstrap("D")
    invs[0].status = InvoiceStatus.VOIDED
    invs[0].voided_at = datetime.utcnow()
    db.session.commit()
    bal = _ar_balance_as_of(c.id, date.today())
    assert abs(bal - 500.0) < 0.01, \
        f"sparkline AR balance leaked voided: {bal}"
    return f"AR sparkline = {bal:.2f} (voided excluded)"


@check("5. dashboard_metrics.unpaid unchanged (positive-list filter)")
def _():
    from app.services.reports import dashboard_metrics
    from app.models import InvoiceStatus
    _teardown()
    c, cust, invs = _bootstrap("E")
    invs[0].status = InvoiceStatus.VOIDED
    invs[0].voided_at = datetime.utcnow()
    db.session.commit()
    # dashboard_metrics builds `unpaid` via positive list —
    # VOIDED never included there. Confirm no regression.
    m = dashboard_metrics(c.id)
    # unpaid_invoices.total should NOT include the voided invoice.
    # The remaining invoice = 500.
    total = m.get("unpaid_invoices", {}).get("total", 0)
    assert abs(total - 500.0) < 0.01, \
        f"unpaid_invoices.total leaked voided: {total}"
    return f"dashboard unpaid_invoices.total = {total:.2f}"


def main():
    app = create_app()
    passed = failed = 0
    for label, fn in CHECKS:
        with app.app_context():
            try:
                _teardown()
                res = fn()
                print(f"PASS  {label}  ⇒ {res}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                failed += 1
                import traceback; traceback.print_exc()
    with app.app_context():
        _teardown()
        print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
