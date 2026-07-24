#!/usr/bin/env python3
"""MARSOUD-INSTALLMENT-PLAN-01 (Abdelhamid 2026-07-24).

Checks:
  1. create_installment_plan(inv, [1000, 1000, 1000]) for total 3000 → 3 rows.
  2. Sum mismatch → InstallmentError, no rows created.
  3. pay_installment #1 → invoice status = PARTIALLY_PAID, JE posted.
  4. pay_installment #2 + #3 → invoice status = PAID.
  5. Cannot re-pay an already-PAID installment.
  6. refresh_installment_overdue_flags: past-due PENDING → OVERDUE.
  7. Creating a second plan on an invoice that already has one → refused.
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
            "SELECT id FROM companies WHERE name LIKE '__IP_%__'"))]
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
            "DELETE FROM users WHERE email LIKE 'ip-%@x.test'"))
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id NOT IN "
                    "(SELECT id FROM companies)"))
        conn.execute(text(
            "DELETE FROM journal_lines WHERE entry_id NOT IN "
            "(SELECT id FROM journal_entries)"))
        conn.execute(text(
            "DELETE FROM invoice_installments WHERE invoice_id NOT IN "
            "(SELECT id FROM invoices)"))
        conn.execute(text(
            "DELETE FROM installment_reminder_sent WHERE installment_id NOT IN "
            "(SELECT id FROM invoice_installments)"))


def _bootstrap():
    """Company + customer + payment method + one invoice for 3000."""
    from app.models import (
        Company, Customer, PaymentMethod, User, UserStatus,
        Invoice, InvoiceItem, InvoiceStatus,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from app.services.invoicing import post_invoice_to_ledger
    from app.services.numbering import next_number
    from werkzeug.security import generate_password_hash
    c = Company(name="__IP_CO__", base_currency="EGP",
                 subdomain="ip-co",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email="ip-owner@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="ip-owner", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    cust = Customer(company_id=c.id, name="عميل تقسيط")
    db.session.add(cust)
    from app.services.ledger import get_account_by_code
    cash_acct = get_account_by_code(c.id, "1010")
    pm = PaymentMethod.query.filter_by(company_id=c.id).first()
    if not pm:
        pm = PaymentMethod(company_id=c.id, name="Cash",
                            name_ar="نقدي",
                            account_id=cash_acct.id, is_active=True,
                            is_default=True)
        db.session.add(pm)
    db.session.flush()
    # Invoice for exactly 3000 (avoid VAT complication → set tax_rate=0).
    inv = Invoice(
        company_id=c.id, customer_id=cust.id,
        number=next_number(c.id, "INVOICE"),
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=90),
        currency="EGP", tax_rate=Decimal("0.00"),
        status=InvoiceStatus.DRAFT,
    )
    inv.items.append(InvoiceItem(
        description="عقد صيانة", quantity=1, unit_price=3000))
    db.session.add(inv); db.session.flush()
    inv.recalc()
    post_invoice_to_ledger(inv, created_by=u.id)
    db.session.commit()
    return c, cust, pm, u, inv


@check("1. create_installment_plan(3x1000) → 3 rows")
def _():
    from app.services.installments import create_installment_plan
    _teardown()
    c, cust, pm, u, inv = _bootstrap()
    _STATE["c"] = c; _STATE["cust"] = cust
    _STATE["pm"] = pm; _STATE["u"] = u; _STATE["inv"] = inv

    rows = create_installment_plan(inv, [
        {"amount": "1000", "due_date": (date.today() + timedelta(days=30)).isoformat()},
        {"amount": "1000", "due_date": (date.today() + timedelta(days=60)).isoformat()},
        {"amount": "1000", "due_date": (date.today() + timedelta(days=90)).isoformat()},
    ], actor_id=u.id)
    assert len(rows) == 3
    assert all(r.status == "PENDING" for r in rows)
    assert sorted([r.sequence_no for r in rows]) == [1, 2, 3]
    assert sum(r.amount for r in rows) == Decimal("3000")
    return "3 installments, all PENDING"


@check("2. Sum mismatch → InstallmentError")
def _():
    from app.services.installments import (
        create_installment_plan, InstallmentError,
    )
    from app.models import Invoice, InvoiceItem, InvoiceStatus
    from app.services.invoicing import post_invoice_to_ledger
    from app.services.numbering import next_number
    inv2 = Invoice(
        company_id=_STATE["c"].id, customer_id=_STATE["cust"].id,
        number=next_number(_STATE["c"].id, "INVOICE"),
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="EGP", tax_rate=Decimal("0.00"),
        status=InvoiceStatus.DRAFT,
    )
    inv2.items.append(InvoiceItem(description="x", quantity=1, unit_price=1000))
    db.session.add(inv2); db.session.flush()
    inv2.recalc()
    post_invoice_to_ledger(inv2, created_by=_STATE["u"].id)
    db.session.commit()

    raised = False
    try:
        create_installment_plan(inv2, [
            {"amount": "500", "due_date": (date.today() + timedelta(days=15)).isoformat()},
            {"amount": "600", "due_date": (date.today() + timedelta(days=30)).isoformat()},  # sums to 1100 ≠ 1000
        ])
    except InstallmentError:
        raised = True
    assert raised, "sum mismatch accepted"
    assert len(inv2.installments) == 0
    return "1100 ≠ 1000 refused"


@check("3. Pay #1 → invoice=PARTIALLY_PAID, JE posted")
def _():
    from app.services.installments import pay_installment
    from app.models import Invoice, InvoiceStatus
    inv = _STATE["inv"]
    installment_1 = inv.installments[0]
    pay_installment(installment_1, payment_method=_STATE["pm"],
                     actor_id=_STATE["u"].id)
    db.session.refresh(inv)
    assert installment_1.status == "PAID"
    assert installment_1.paid_payment_id is not None
    assert inv.status == InvoiceStatus.PARTIALLY_PAID, \
        f"status={inv.status}"
    assert float(inv.paid_amount) == 1000.0
    return "PARTIALLY_PAID, 1000 collected"


@check("4. Pay #2 + #3 → invoice=PAID")
def _():
    from app.services.installments import pay_installment
    from app.models import InvoiceStatus
    inv = _STATE["inv"]
    for i in inv.installments:
        if i.status == "PENDING":
            pay_installment(i, payment_method=_STATE["pm"],
                             actor_id=_STATE["u"].id)
    db.session.refresh(inv)
    assert inv.status == InvoiceStatus.PAID, f"status={inv.status}"
    assert float(inv.paid_amount) == 3000.0
    return "PAID, 3000 collected"


@check("5. Cannot re-pay a PAID installment")
def _():
    from app.services.installments import (
        pay_installment, InstallmentError,
    )
    inv = _STATE["inv"]
    installment_1 = inv.installments[0]
    raised = False
    try:
        pay_installment(installment_1,
                         payment_method=_STATE["pm"],
                         actor_id=_STATE["u"].id)
    except InstallmentError:
        raised = True
    assert raised, "double payment allowed"
    return "double-pay refused"


@check("6. refresh_installment_overdue_flags flips past-due")
def _():
    from app.services.installments import (
        create_installment_plan, refresh_installment_overdue_flags,
    )
    from app.models import Invoice, InvoiceItem, InvoiceStatus
    from app.services.invoicing import post_invoice_to_ledger
    from app.services.numbering import next_number
    inv3 = Invoice(
        company_id=_STATE["c"].id, customer_id=_STATE["cust"].id,
        number=next_number(_STATE["c"].id, "INVOICE"),
        issue_date=date.today() - timedelta(days=60),
        due_date=date.today() + timedelta(days=30),
        currency="EGP", tax_rate=Decimal("0.00"),
        status=InvoiceStatus.DRAFT,
    )
    inv3.items.append(InvoiceItem(description="y", quantity=1, unit_price=500))
    db.session.add(inv3); db.session.flush()
    inv3.recalc()
    post_invoice_to_ledger(inv3, created_by=_STATE["u"].id)
    db.session.commit()
    create_installment_plan(inv3, [
        {"amount": "250", "due_date": (date.today() - timedelta(days=5)).isoformat()},   # past-due
        {"amount": "250", "due_date": (date.today() + timedelta(days=15)).isoformat()},  # future
    ])
    n = refresh_installment_overdue_flags(company_id=_STATE["c"].id)
    assert n == 1, f"expected 1 flip, got {n}"
    return "1 past-due flipped to OVERDUE"


@check("7. Second plan on same invoice → refused")
def _():
    from app.services.installments import (
        create_installment_plan, InstallmentError,
    )
    inv = _STATE["inv"]   # already has a plan from check 1
    raised = False
    try:
        create_installment_plan(inv, [
            {"amount": "1500", "due_date": (date.today() + timedelta(days=10)).isoformat()},
            {"amount": "1500", "due_date": (date.today() + timedelta(days=40)).isoformat()},
        ])
    except InstallmentError:
        raised = True
    assert raised, "duplicate plan allowed"
    return "second plan refused"


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
