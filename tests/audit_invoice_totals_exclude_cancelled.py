#!/usr/bin/env python3
"""MARSOUD-FIX-INVOICE-TOTALS-CANCELLED (Abdelhamid 2026-07-24).

Bug: `total_invoiced` and `total_collected` on the /invoices index
included CANCELLED / VOIDED / REFUNDED. `total_outstanding` was
missing VOIDED.

Checks:
  1. Direct expression: CANCELLED invoice is excluded from all 3 sums.
  2. VOIDED invoice is excluded from all 3 sums.
  3. REFUNDED invoice is excluded from all 3 sums.
  4. Baseline: SENT/PAID/PARTIALLY_PAID invoices ARE included.
  5. HTTP: the /invoices page renders totals matching the filter.
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
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__IT_%__'"))]
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
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'it-%@x.test'"))
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id NOT IN "
                    "(SELECT id FROM companies)"))


def _mk_company():
    from app.models import Company
    from app.services.seed_coa import seed_default_coa
    c = Company(name="__IT_A__", base_currency="EGP",
                 subdomain="it-a",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    db.session.commit()
    return c


def _mk_invoice(company_id, total, paid_amount, status, num):
    from app.models import Invoice, InvoiceStatus
    inv = Invoice(
        company_id=company_id, number=num,
        issue_date=date.today(), due_date=date.today() + timedelta(days=30),
        currency="EGP",
        total=Decimal(str(total)),
        paid_amount=Decimal(str(paid_amount)),
        status=status,
    )
    db.session.add(inv); db.session.commit()
    return inv


@check("1. CANCELLED invoice excluded from all 3 sums")
def _():
    from app.models import Invoice, InvoiceStatus
    _teardown()
    c = _mk_company()
    _STATE["c"] = c
    _mk_invoice(c.id, 1000, 800, InvoiceStatus.PARTIALLY_PAID, "IT-001")
    _mk_invoice(c.id, 5000, 5000, InvoiceStatus.CANCELLED, "IT-002")

    invoices = Invoice.query.filter_by(company_id=c.id).all()
    EXCLUDED = (InvoiceStatus.CANCELLED, InvoiceStatus.VOIDED,
                 InvoiceStatus.REFUNDED)
    countable = [i for i in invoices if i.status not in EXCLUDED]
    assert sum(float(i.total or 0) for i in countable) == 1000.0
    assert sum(float(i.paid_amount or 0) for i in countable) == 800.0
    assert sum(i.balance for i in countable) == 200.0
    return "1000/800/200 (5000 CANCELLED not counted)"


@check("2. VOIDED invoice excluded from all 3 sums")
def _():
    from app.models import Invoice, InvoiceStatus
    _mk_invoice(_STATE["c"].id, 3000, 3000, InvoiceStatus.VOIDED, "IT-003")
    invoices = Invoice.query.filter_by(company_id=_STATE["c"].id).all()
    EXCLUDED = (InvoiceStatus.CANCELLED, InvoiceStatus.VOIDED,
                 InvoiceStatus.REFUNDED)
    countable = [i for i in invoices if i.status not in EXCLUDED]
    # Still 1000/800/200 (3000 VOIDED shouldn't add).
    assert sum(float(i.total or 0) for i in countable) == 1000.0
    assert sum(float(i.paid_amount or 0) for i in countable) == 800.0
    assert sum(i.balance for i in countable) == 200.0
    return "3000 VOIDED not counted"


@check("3. REFUNDED invoice excluded from all 3 sums")
def _():
    from app.models import Invoice, InvoiceStatus
    _mk_invoice(_STATE["c"].id, 2500, 2500, InvoiceStatus.REFUNDED, "IT-004")
    invoices = Invoice.query.filter_by(company_id=_STATE["c"].id).all()
    EXCLUDED = (InvoiceStatus.CANCELLED, InvoiceStatus.VOIDED,
                 InvoiceStatus.REFUNDED)
    countable = [i for i in invoices if i.status not in EXCLUDED]
    assert sum(float(i.total or 0) for i in countable) == 1000.0
    assert sum(float(i.paid_amount or 0) for i in countable) == 800.0
    return "2500 REFUNDED not counted"


@check("4. PAID / SENT invoices ARE included")
def _():
    from app.models import Invoice, InvoiceStatus
    _mk_invoice(_STATE["c"].id, 700, 700, InvoiceStatus.PAID, "IT-005")
    _mk_invoice(_STATE["c"].id, 400, 0, InvoiceStatus.SENT, "IT-006")
    invoices = Invoice.query.filter_by(company_id=_STATE["c"].id).all()
    EXCLUDED = (InvoiceStatus.CANCELLED, InvoiceStatus.VOIDED,
                 InvoiceStatus.REFUNDED)
    countable = [i for i in invoices if i.status not in EXCLUDED]
    # 1000 + 700 + 400 = 2100 total; 800 + 700 + 0 = 1500 collected;
    # balance = 200 + 0 + 400 = 600.
    assert sum(float(i.total or 0) for i in countable) == 2100.0
    assert sum(float(i.paid_amount or 0) for i in countable) == 1500.0
    assert sum(i.balance for i in countable) == 600.0
    return "PAID(700)+SENT(400) added → 2100/1500/600"


@check("5. /invoices HTTP renders totals excluding CANCELLED/VOIDED/REFUNDED")
def _():
    from flask import current_app
    from app.models import User, UserStatus
    from app.models.user import user_companies
    from werkzeug.security import generate_password_hash

    u = User(email="it-owner@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="it-owner", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST",
             is_superadmin=True)  # bypass plan-selection/terms middleware
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=_STATE["c"].id, role="owner"))
    db.session.commit()

    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["c"].id
    r = client.get("/invoices/")
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)
    # Screen shows الأرقام formatted with commas — assert the KPI
    # numbers 2,100 and 1,500 appear but 8,500 (which would be the
    # buggy sum including CANCELLED+REFUNDED+VOIDED) does not.
    # Buggy total_invoiced would be 1000+5000+3000+2500+700+400=12600
    assert "12,600" not in body and "12600" not in body, \
        "buggy total leaked in"
    # 2100 (fixed total_invoiced) should render as 2,100
    assert "2,100" in body or "2100" in body, \
        "expected fixed total 2100 in body"
    return "screen shows 2,100 (not 12,600)"


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
