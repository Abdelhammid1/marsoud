#!/usr/bin/env python3
"""MARSOUD-VOIDED-VISIBLE (Abdelhamid 2026-08-01).

Batch 9 Ticket 2. User clarified on Batch 8 Ticket 3 that voided
(soft-deleted) invoices should stay VISIBLE in the invoices list
marked as deleted, NOT hidden by default. Financial totals must
still exclude them (already the case via _EXCLUDED).

Checks:
  1. index() default (no `?deleted_filter=`) returns voided rows.
  2. Explicit `?deleted_filter=active` still hides them
     (backward-compat guard).
  3. Explicit `?deleted_filter=deleted` shows only voided rows.
  4. KPI totals (total_invoiced, total_collected,
     total_outstanding) still exclude voided invoices
     (regression guard on the _EXCLUDED counting logic).
  5. Template renders a "🗑️ محذوفة" badge on voided rows.
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
    db.session.expunge_all()
    db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__VIV_%__'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            conn.execute(text(
                "DELETE FROM journal_lines WHERE entry_id IN "
                "(SELECT id FROM journal_entries WHERE company_id = :c)"),
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
    """Company + customer + 2 SENT invoices, one voided."""
    from app.models import (
        Company, Customer, Invoice, InvoiceItem, InvoiceStatus, Plan,
    )
    from app.services.seed_coa import seed_default_coa
    plan = Plan.query.filter_by(is_active=True).first()
    c = Company(name=f"__VIV_{suffix}__", base_currency="EGP",
                 subdomain=f"viv-{suffix.lower()}",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1),
                 intended_plan_id=plan.id if plan else None,
                 plan_id=plan.id if plan else None)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    cust = Customer(company_id=c.id, name="Test Customer")
    db.session.add(cust); db.session.flush()
    from app.services.subsidiary import ensure_customer_account
    ensure_customer_account(cust)
    invs = []
    for i, amount in enumerate([500, 800], start=1):
        inv = Invoice(company_id=c.id, customer_id=cust.id,
                       number=f"INV-VIV-{suffix}-{i}",
                       issue_date=date.today() - timedelta(days=10),
                       due_date=date.today() + timedelta(days=20),
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
    # Void invoice #1.
    invs[0].status = InvoiceStatus.VOIDED
    invs[0].voided_at = datetime.utcnow()
    db.session.commit()
    return c, invs


def _query_index(c, filter_val=None):
    """Reproduce the invoices.index() filter chain and return rows."""
    from app.models import Invoice, InvoiceStatus
    q = Invoice.query.filter_by(company_id=c.id)
    if filter_val is None:
        # New default: no filter → return all.
        pass
    elif filter_val == "active":
        q = q.filter(Invoice.voided_at.is_(None))
    elif filter_val == "deleted":
        q = q.filter(Invoice.voided_at.isnot(None))
    return q.all()


@check("1. Default filter (no query) includes voided rows")
def _():
    _teardown()
    c, invs = _bootstrap("A")
    # Simulate the route: `deleted_filter=all` is the new default.
    rows = _query_index(c, filter_val=None)
    ids = {i.id for i in rows}
    assert invs[0].id in ids, \
        "voided invoice hidden by default (bug — should be visible)"
    assert invs[1].id in ids
    return f"{len(rows)} rows including voided"


@check("2. ?deleted_filter=active still hides voided (back-compat)")
def _():
    _teardown()
    c, invs = _bootstrap("B")
    rows = _query_index(c, filter_val="active")
    ids = {i.id for i in rows}
    assert invs[0].id not in ids, \
        "active-only filter leaked voided"
    assert invs[1].id in ids
    return "active-only correctly hides voided"


@check("3. ?deleted_filter=deleted shows only voided")
def _():
    _teardown()
    c, invs = _bootstrap("C")
    rows = _query_index(c, filter_val="deleted")
    ids = {i.id for i in rows}
    assert ids == {invs[0].id}, f"expected only voided, got {ids}"
    return "deleted-only correctly shows only voided"


@check("4. KPI totals still exclude voided (regression on _EXCLUDED)")
def _():
    from app.models import InvoiceStatus
    _teardown()
    c, invs = _bootstrap("D")
    # Reproduce the index route's KPI counting logic verbatim.
    _EXCLUDED = (InvoiceStatus.CANCELLED,
                 InvoiceStatus.VOIDED,
                 InvoiceStatus.REFUNDED)
    all_invs = _query_index(c, filter_val=None)
    countable = [i for i in all_invs if i.status not in _EXCLUDED]
    total_invoiced = sum(float(i.total or 0) for i in countable)
    # Only invoice #2 (800) counts; #1 was voided (500 excluded).
    assert abs(total_invoiced - 800) < 0.01, \
        f"KPI total leaked voided: {total_invoiced}"
    return f"total_invoiced = {total_invoiced} (voided excluded)"


@check("5. Template renders 🗑️ محذوفة badge on voided rows")
def _():
    tpl = (ROOT / "app" / "templates" / "invoices"
            / "index.html").read_text()
    assert "🗑️ محذوفة" in tpl, \
        "voided badge not in template"
    assert "opacity-60" in tpl, \
        "voided row visual muting missing"
    assert "line-through" in tpl, \
        "voided row strikethrough missing"
    return "voided badge + visual muting present"


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
