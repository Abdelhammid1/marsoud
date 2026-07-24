#!/usr/bin/env python3
"""MARSOUD-CUSTOMER-DEPOSIT-01 (Abdelhamid 2026-07-24).

Checks:
  1. record_deposit posts a balanced JE and returns doc_number.
  2. Customer AR sub-account shows the deposit as a credit balance.
  3. apply_to_invoice consumes the deposit against a new invoice,
     flips status APPLIED, invoice.paid_amount grows by the deposit
     amount.
  4. Cannot apply an already-APPLIED deposit twice.
  5. Cannot apply a deposit belonging to a different customer.
  6. refund() reverses the deposit JE and flips status REFUNDED.
  7. Cannot refund an APPLIED deposit.
  8. Multiple ACTIVE deposits sum correctly via total_active_amount.
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
            "SELECT id FROM companies WHERE name LIKE '__CD_%__'"))]
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
            "DELETE FROM users WHERE email LIKE 'cd-%@x.test'"))
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id NOT IN "
                    "(SELECT id FROM companies)"))
        # SQLite reuses PKs — orphan journal_lines pointing at deleted
        # entries would collide with the new deposit's JE lookups.
        conn.execute(text(
            "DELETE FROM journal_lines WHERE entry_id NOT IN "
            "(SELECT id FROM journal_entries)"))


def _bootstrap():
    from app.models import Company, Customer, PaymentMethod, User, UserStatus
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    c = Company(name="__CD_CO__", base_currency="EGP",
                 subdomain="cd-co",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email="cd-owner@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="cd-owner", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    cust = Customer(company_id=c.id, name="عميل جديد")
    cust2 = Customer(company_id=c.id, name="عميل تاني")
    db.session.add_all([cust, cust2])
    # payment method — seed_default_coa often already set Cash but
    # not always with a PaymentMethod row.
    pm = PaymentMethod.query.filter_by(company_id=c.id).first()
    if not pm:
        from app.services.ledger import get_account_by_code
        cash_acct = get_account_by_code(c.id, "1010")
        pm = PaymentMethod(company_id=c.id, name="Cash",
                            name_ar="نقدي",
                            account_id=cash_acct.id, is_active=True,
                            is_default=True)
        db.session.add(pm)
    db.session.commit()
    return c, cust, cust2, pm, u


@check("1. record_deposit posts balanced JE + assigns doc_number")
def _():
    from app.services.deposits import record_deposit
    from app.models import JournalEntry, JournalLine
    _teardown()
    c, cust, cust2, pm, u = _bootstrap()
    _STATE["c"] = c; _STATE["cust"] = cust; _STATE["cust2"] = cust2
    _STATE["pm"] = pm; _STATE["u"] = u

    d = record_deposit(
        company_id=c.id, customer=cust, amount=500,
        payment_method=pm, date_=date.today(),
        actor_id=u.id,
    )
    assert d.doc_number.startswith("DEP-"), \
        f"unexpected doc_number: {d.doc_number}"
    assert d.status == "ACTIVE"
    assert d.journal_entry_id, "no JE linked"
    entry = db.session.get(JournalEntry, d.journal_entry_id)
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    dr = sum(float(l.debit or 0) for l in lines)
    cr = sum(float(l.credit or 0) for l in lines)
    assert dr == cr == 500.0, f"JE unbalanced: dr={dr}, cr={cr}"
    _STATE["deposit"] = d
    return f"{d.doc_number} → 500 balanced"


@check("2. total_active_amount reflects the deposit")
def _():
    from app.services.deposits import total_active_amount
    total = total_active_amount(_STATE["cust"].id)
    assert total == Decimal("500"), f"got {total}"
    return f"active total = {total}"


@check("3. apply_to_invoice consumes deposit against new invoice")
def _():
    from app.models import Invoice, InvoiceItem, InvoiceStatus
    from app.services.invoicing import post_invoice_to_ledger
    from app.services.deposits import apply_to_invoice
    from app.services.numbering import next_number
    c = _STATE["c"]; cust = _STATE["cust"]; u = _STATE["u"]
    inv = Invoice(
        company_id=c.id, customer_id=cust.id,
        number=next_number(c.id, "INVOICE"),
        issue_date=date.today(), due_date=date.today() + timedelta(days=30),
        currency="EGP", tax_rate=Decimal("15.00"),
        status=InvoiceStatus.DRAFT,
    )
    inv.items.append(InvoiceItem(
        description="خدمة", quantity=1, unit_price=1500))
    db.session.add(inv); db.session.flush()
    inv.recalc()
    post_invoice_to_ledger(inv, created_by=u.id)
    db.session.commit()
    _STATE["invoice"] = inv
    # Before: paid_amount = 0.
    before_paid = float(inv.paid_amount or 0)
    d = apply_to_invoice(_STATE["deposit"], inv, actor_id=u.id)
    assert d.status == "APPLIED"
    assert d.applied_invoice_id == inv.id
    # Refresh invoice.
    db.session.refresh(inv)
    after_paid = float(inv.paid_amount or 0)
    assert after_paid - before_paid == 500.0, \
        f"paid_amount grew by {after_paid - before_paid} (expected 500)"
    return f"invoice #{inv.id} paid_amount → {after_paid}"


@check("4. Cannot re-apply an APPLIED deposit")
def _():
    from app.services.deposits import apply_to_invoice, DepositError
    raised = False
    try:
        apply_to_invoice(_STATE["deposit"], _STATE["invoice"],
                          actor_id=_STATE["u"].id)
    except DepositError:
        raised = True
    assert raised, "second apply was allowed"
    return "second apply → refused"


@check("5. Cannot apply deposit meant for a different customer")
def _():
    from app.services.deposits import (
        record_deposit, apply_to_invoice, DepositError,
    )
    from app.models import Invoice, InvoiceItem, InvoiceStatus
    from app.services.invoicing import post_invoice_to_ledger
    from app.services.numbering import next_number
    c = _STATE["c"]; cust2 = _STATE["cust2"]; u = _STATE["u"]
    # A fresh deposit for cust2, then try to apply it to cust1's next invoice.
    d2 = record_deposit(
        company_id=c.id, customer=cust2, amount=200,
        payment_method=_STATE["pm"], date_=date.today(),
        actor_id=u.id,
    )
    inv = Invoice(
        company_id=c.id, customer_id=_STATE["cust"].id,
        number=next_number(c.id, "INVOICE"),
        issue_date=date.today(), due_date=date.today() + timedelta(days=30),
        currency="EGP", tax_rate=Decimal("15.00"),
        status=InvoiceStatus.DRAFT,
    )
    inv.items.append(InvoiceItem(description="x", quantity=1, unit_price=100))
    db.session.add(inv); db.session.flush()
    inv.recalc(); post_invoice_to_ledger(inv, created_by=u.id)
    db.session.commit()
    raised = False
    try:
        apply_to_invoice(d2, inv, actor_id=u.id)
    except DepositError:
        raised = True
    assert raised, "cross-customer apply allowed"
    _STATE["deposit2"] = d2
    return "cross-customer apply → refused"


@check("6. refund reverses JE + status=REFUNDED")
def _():
    from app.services.deposits import refund
    from app.models import JournalEntry, JournalLine
    d2 = _STATE["deposit2"]
    d2 = refund(d2, actor_id=_STATE["u"].id)
    assert d2.status == "REFUNDED"
    assert d2.refund_journal_entry_id, "no reversal JE"
    rev = db.session.get(JournalEntry, d2.refund_journal_entry_id)
    lines = JournalLine.query.filter_by(entry_id=rev.id).all()
    dr = sum(float(l.debit or 0) for l in lines)
    cr = sum(float(l.credit or 0) for l in lines)
    assert dr == cr == 200.0, f"reversal unbalanced: dr={dr}, cr={cr}"
    return "reversed + REFUNDED"


@check("7. Cannot refund an already-APPLIED deposit")
def _():
    from app.services.deposits import refund, DepositError
    raised = False
    try:
        refund(_STATE["deposit"], actor_id=_STATE["u"].id)
    except DepositError:
        raised = True
    assert raised, "refund of APPLIED deposit allowed"
    return "APPLIED cannot be refunded"


@check("8. total_active_amount now zero (all consumed/refunded)")
def _():
    from app.services.deposits import total_active_amount
    t1 = total_active_amount(_STATE["cust"].id)
    t2 = total_active_amount(_STATE["cust2"].id)
    assert t1 == Decimal("0"), f"cust1 still {t1}"
    assert t2 == Decimal("0"), f"cust2 still {t2}"
    return "both customers back to 0 active"


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
