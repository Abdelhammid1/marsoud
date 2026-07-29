#!/usr/bin/env python3
"""MARSOUD-DASHBOARD-RECURRING-TITLE (Abdelhamid 2026-07-29).

Batch 6 Ticket 3. Extends the Batch 5 Ticket 3 treatment from the
'الفواتير المتأخرة' panel to the 'فواتير جايّة عليك' panel:
- Distinguishable title (source bill notes → vendor → interval).
- Per-row link to the underlying VendorBill, not the recurring
  bills list.

Checks:
  1. Upcoming bill dict has title_for_display + source_bill_id.
  2. Title falls back through: notes → vendor → interval.
  3. Bill with notes shows notes as the title.
  4. Bill without notes but with vendor shows vendor as title.
  5. Cross-tenant: company A's forecast doesn't leak into B's.
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
_STATE = {}


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
            "SELECT id FROM companies WHERE name LIKE '__DRB_%__'"))]
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


def _mk_company(suffix):
    from app.models import Company
    from app.services.seed_coa import seed_default_coa
    c = Company(name=f"__DRB_{suffix}__", base_currency="EGP",
                 subdomain=f"drb-{suffix.lower()}",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    return c


def _mk_recurring_bill(company_id, vendor_name, notes, amount=100,
                         days_out=3):
    """Create a VendorBill + RecurringBill so get_due_within
    surfaces an occurrence within the 7-day window."""
    from app.models import Vendor, VendorBill, VendorBillStatus
    from app.models.recurring_bill import RecurringBill
    v = Vendor(company_id=company_id, name=vendor_name)
    db.session.add(v); db.session.flush()
    bill = VendorBill(
        company_id=company_id,
        vendor_id=v.id,
        number=f"VB-{v.id}",
        issue_date=date.today() - timedelta(days=30),
        due_date=date.today(),
        subtotal=amount,
        total=amount,
        tax_amount=0,
        currency="EGP",
        status=VendorBillStatus.POSTED,
        notes=notes,
    )
    db.session.add(bill); db.session.flush()
    # Recurring template starting today so occurrence lands within
    # the 7-day window.
    from datetime import date as _d
    rb = RecurringBill(
        company_id=company_id,
        source_bill_id=bill.id,
        vendor_id=v.id,
        amount=amount,
        currency="EGP",
        interval_unit="MONTH",
        interval_count=1,
        start_date=_d.today() + timedelta(days=days_out),
        active=True,
    )
    db.session.add(rb); db.session.flush()
    return rb, bill, v


@check("1. Upcoming bill dict has title_for_display + source_bill_id")
def _():
    from app.services.reports import dashboard_metrics
    _teardown()
    c = _mk_company("A")
    _mk_recurring_bill(c.id, "شركة X", "إيجار المكتب — شهري",
                        amount=1500, days_out=2)
    db.session.commit()
    metrics = dashboard_metrics(c.id)
    ub = metrics.get("upcoming_bills") or []
    assert ub, "no upcoming bills built"
    row = ub[0]
    assert "title_for_display" in row, \
        f"key missing; keys={list(row.keys())}"
    assert "source_bill_id" in row, \
        f"key missing; keys={list(row.keys())}"
    return f"title={row['title_for_display']!r}, source={row['source_bill_id']}"


@check("2. Bill with notes: title_for_display = notes")
def _():
    from app.services.reports import dashboard_metrics
    _teardown()
    c = _mk_company("B")
    _mk_recurring_bill(c.id, "مورد الأدوات",
                        "استئجار طابعة زيروكس", amount=800, days_out=1)
    db.session.commit()
    ub = (dashboard_metrics(c.id).get("upcoming_bills") or [])
    assert ub[0]["title_for_display"] == "استئجار طابعة زيروكس", \
        f"got {ub[0]['title_for_display']!r}"
    return "notes used"


@check("3. Bill without notes: title_for_display falls back to vendor name")
def _():
    from app.services.reports import dashboard_metrics
    _teardown()
    c = _mk_company("C")
    _mk_recurring_bill(c.id, "شركة الكهرباء", None,
                        amount=350, days_out=4)
    db.session.commit()
    ub = (dashboard_metrics(c.id).get("upcoming_bills") or [])
    assert ub[0]["title_for_display"] == "شركة الكهرباء", \
        f"got {ub[0]['title_for_display']!r}"
    return "vendor used as fallback"


@check("4. Bill with WHITESPACE-only notes: falls back (not blank)")
def _():
    from app.services.reports import dashboard_metrics
    _teardown()
    c = _mk_company("D")
    _mk_recurring_bill(c.id, "مورد الغاز", "   \n  \t  ",
                        amount=200, days_out=5)
    db.session.commit()
    ub = (dashboard_metrics(c.id).get("upcoming_bills") or [])
    title = ub[0]["title_for_display"]
    assert title.strip() and title != "—", \
        f"got blank/dash title: {title!r}"
    assert title == "مورد الغاز", \
        f"expected vendor fallback, got {title!r}"
    return "whitespace-notes skipped"


@check("5. Cross-tenant: company A's forecast doesn't leak to company B")
def _():
    from app.services.reports import dashboard_metrics
    _teardown()
    ca = _mk_company("E1")
    cb = _mk_company("E2")
    _mk_recurring_bill(ca.id, "vendor-in-A",
                        "خاص-بشركة-A", amount=999, days_out=2)
    db.session.commit()
    ub_a = dashboard_metrics(ca.id).get("upcoming_bills") or []
    ub_b = dashboard_metrics(cb.id).get("upcoming_bills") or []
    assert ub_a, "A got zero forecasted bills"
    assert not ub_b, f"B leaked bills: {ub_b}"
    return "isolation OK"


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
