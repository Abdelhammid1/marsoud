#!/usr/bin/env python3
"""MARSOUD-INSTALLMENT-PLAN-01 pt 2 (Abdelhamid 2026-07-25).

Verify per-installment reminders (mirror of invoice reminders,
gated by company.reminders config).

Checks:
  1. Installment due in 7 days + days_before=[7] → 1 email sent.
  2. Same-day re-run → 0 emails (dedupe row exists).
  3. Installment overdue by 3 days + overdue_days=[3] → 1 email.
  4. Cancelled invoice → its installments are NOT reminded.
  5. Company reminders disabled → nothing sent.
  6. Installment already PAID → no reminder.
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
_EMAIL_LOG = []


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
            "SELECT id FROM companies WHERE name LIKE '__IR_%__'"))]
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
            "DELETE FROM users WHERE email LIKE 'ir-%@x.test'"))
        conn.execute(text(
            "DELETE FROM installment_reminder_sent WHERE installment_id "
            "NOT IN (SELECT id FROM invoice_installments)"))
        # SQLite reuses PKs — orphan installments from deleted
        # invoices would show up on the new (id-reused) invoice via
        # a plain query, so wipe them.
        conn.execute(text(
            "DELETE FROM invoice_installments WHERE invoice_id "
            "NOT IN (SELECT id FROM invoices)"))
    _EMAIL_LOG.clear()


def _stub_email():
    """Replace send_email to capture calls without hitting SMTP."""
    from app.services import email as _email_mod
    from app.services import reminders as _rem_mod
    def capture(to, subject, html_body, **kw):
        _EMAIL_LOG.append((to, subject))
        return True
    _email_mod.send_email = capture
    _rem_mod.send_email = capture


def _bootstrap(reminders_enabled=True, days_before=None, overdue_days=None):
    from app.models import (
        Company, Customer, User, UserStatus, Invoice, InvoiceItem,
        InvoiceStatus, PaymentMethod,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from app.services.invoicing import post_invoice_to_ledger
    from app.services.numbering import next_number
    from app.services.installments import create_installment_plan
    from werkzeug.security import generate_password_hash
    import json

    c = Company(name="__IR_CO__", base_currency="EGP",
                 subdomain="ir-co",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    # reminders config lives on Company.reminder_config as JSON.
    cfg = {"enabled": reminders_enabled}
    if days_before is not None:
        cfg["days_before"] = days_before
    if overdue_days is not None:
        cfg["overdue_days"] = overdue_days
    c.reminder_config = json.dumps(cfg)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email="ir-owner@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="ir", is_active=True,
             status=UserStatus.ACTIVE.value)
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    cust = Customer(company_id=c.id, name="عميل",
                     email="cust-ir@x.test")
    db.session.add(cust); db.session.commit()
    return c, cust, u


def _mk_invoice_with_installments(company, customer, actor,
                                     amounts_and_due_days):
    """Build a POSTED invoice with the given plan. amounts_and_due_days
    is a list of (amount, +/-days-from-today)."""
    from app.models import Invoice, InvoiceItem, InvoiceStatus
    from app.services.invoicing import post_invoice_to_ledger
    from app.services.numbering import next_number
    from app.services.installments import create_installment_plan
    total = sum(a for a, _ in amounts_and_due_days)
    inv = Invoice(
        company_id=company.id, customer_id=customer.id,
        number=next_number(company.id, "INVOICE"),
        issue_date=date.today() - timedelta(days=30),
        due_date=date.today() + timedelta(days=90),
        currency="EGP", tax_rate=Decimal("0.00"),
        status=InvoiceStatus.DRAFT,
        send_reminders=True,
    )
    inv.items.append(InvoiceItem(description="x", quantity=1,
                                   unit_price=total))
    db.session.add(inv); db.session.flush()
    inv.recalc()
    post_invoice_to_ledger(inv, created_by=actor.id)
    db.session.commit()
    create_installment_plan(inv, [
        {"amount": str(amt),
         "due_date": (date.today() + timedelta(days=d)).isoformat()}
        for amt, d in amounts_and_due_days
    ])
    return inv


@check("1. Due in 7 days + days_before=[7] → email sent")
def _():
    from app.services.reminders import process_installment_reminders
    _teardown(); _stub_email()
    c, cust, u = _bootstrap(days_before=[7], overdue_days=[])
    _STATE["c"] = c; _STATE["u"] = u; _STATE["cust"] = cust
    inv = _mk_invoice_with_installments(c, cust, u, [
        (500, 7),   # due exactly 7 days out → matches
        (500, 30),  # due 30 days out → doesn't match
    ])
    _EMAIL_LOG.clear()
    result = process_installment_reminders()
    assert result["before"] == 1, f"expected 1, got {result}"
    assert len(_EMAIL_LOG) == 1, f"emails: {_EMAIL_LOG}"
    assert "خلال 7" in _EMAIL_LOG[0][1] or "7 أيام" in _EMAIL_LOG[0][1]
    _STATE["inv"] = inv
    return "1 email fired for -7 installment"


@check("2. Same-day re-run → 0 emails (dedupe)")
def _():
    from app.services.reminders import process_installment_reminders
    _EMAIL_LOG.clear()
    result = process_installment_reminders()
    assert result["before"] == 0, f"duplicate fired: {result}"
    assert len(_EMAIL_LOG) == 0
    return "dedupe holds"


@check("3. Overdue by 3 + overdue_days=[3] → email sent")
def _():
    from app.services.reminders import process_installment_reminders
    _teardown(); _stub_email()
    c, cust, u = _bootstrap(days_before=[], overdue_days=[3])
    _STATE["c"] = c; _STATE["u"] = u; _STATE["cust"] = cust
    _mk_invoice_with_installments(c, cust, u, [
        (500, -3),   # due 3 days ago → matches overdue_days=3
        (500, 30),
    ])
    _EMAIL_LOG.clear()
    result = process_installment_reminders()
    assert result["overdue"] == 1, f"expected 1, got {result}"
    assert len(_EMAIL_LOG) == 1
    assert "متأخر منذ 3" in _EMAIL_LOG[0][1]
    return "1 overdue email"


@check("4. Cancelled invoice → installments NOT reminded")
def _():
    from app.services.reminders import process_installment_reminders
    from app.models import Invoice, InvoiceStatus
    _teardown(); _stub_email()
    c, cust, u = _bootstrap(days_before=[7], overdue_days=[])
    inv = _mk_invoice_with_installments(c, cust, u, [
        (500, 7), (500, 30),
    ])
    inv.status = InvoiceStatus.CANCELLED
    db.session.commit()
    _EMAIL_LOG.clear()
    result = process_installment_reminders()
    assert result["before"] == 0, f"cancelled invoice fired: {result}"
    assert len(_EMAIL_LOG) == 0
    return "cancelled invoice skipped"


@check("5. Company reminders disabled → nothing sent")
def _():
    from app.services.reminders import process_installment_reminders
    _teardown(); _stub_email()
    c, cust, u = _bootstrap(reminders_enabled=False,
                              days_before=[7], overdue_days=[])
    _mk_invoice_with_installments(c, cust, u, [
        (500, 7), (500, 30),
    ])
    _EMAIL_LOG.clear()
    result = process_installment_reminders()
    assert result["before"] == 0
    assert result["skipped"] >= 1
    assert len(_EMAIL_LOG) == 0
    return "config off → silence"


@check("6. PAID installment → no reminder")
def _():
    from app.services.reminders import process_installment_reminders
    from app.models import PaymentMethod, Account
    from app.services.installments import pay_installment
    _teardown(); _stub_email()
    c, cust, u = _bootstrap(days_before=[7], overdue_days=[])
    # Reuse the seed_coa payment method if it created one; else insert
    # with a unique name to avoid clashing with the seed.
    pm = PaymentMethod.query.filter_by(
        company_id=c.id, is_active=True).first()
    if not pm:
        any_asset = Account.query.filter_by(
            company_id=c.id, is_postable=True).first()
        pm = PaymentMethod(company_id=c.id, name="IR-Cash",
                            name_ar="نقدي",
                            account_id=any_asset.id, is_active=True,
                            is_default=True)
        db.session.add(pm); db.session.commit()
    inv = _mk_invoice_with_installments(c, cust, u, [
        (500, 7), (500, 30),
    ])
    # Pay the -7 installment; it should now be excluded from
    # reminders (PAID != PENDING/OVERDUE).
    pay_installment(inv.installments[0], payment_method=pm,
                     actor_id=u.id)
    _EMAIL_LOG.clear()
    result = process_installment_reminders()
    assert result["before"] == 0, \
        f"PAID installment got a reminder: {result}"
    assert len(_EMAIL_LOG) == 0
    return "PAID skipped"


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
