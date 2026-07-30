#!/usr/bin/env python3
"""MARSOUD-SAAS-EMAIL-01 (Abdelhamid 2026-07-29).

Batch 6 Ticket 1 audit. On prod, /choose-plan created the SaaS
invoice but no email fired — root cause: ensure_saas_customer
hardcoded email=None + create_first_invoice never called the
notification sender.

Checks:
  1. ensure_saas_customer copies owner's email onto the mirror
     Customer row.
  2. ensure_saas_customer keeps the mirror email in sync when
     the owner's email changes between calls.
  3. create_first_invoice invokes send_invoice_notification with
     the created invoice.
  4. mark_saas_invoice_paid invokes send_invoice_notification on
     the NEXT invoice it creates (renewal cycle).
  5. Email failure inside _try_email doesn't roll back the DB
     transaction — the invoice + subscription state stays intact.
"""
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

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
    from app.services.manasty import manasty_id
    db.session.rollback()
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__SBE_%__'"))]
        for cid in cids:
            conn.execute(text(
                "UPDATE companies SET saas_customer_id = NULL, "
                "applied_coupon_id = NULL WHERE id = :c"),
                {"c": cid})
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
        mid = manasty_id()
        conn.execute(text(
            "DELETE FROM invoice_items WHERE invoice_id IN "
            "(SELECT id FROM invoices WHERE company_id = :m "
            "AND source = 'SAAS_BILLING' AND notes LIKE '%__SBE_%')"),
            {"m": mid})
        conn.execute(text(
            "DELETE FROM invoices WHERE company_id = :m "
            "AND source = 'SAAS_BILLING' AND notes LIKE '%__SBE_%'"),
            {"m": mid})
        conn.execute(text(
            "DELETE FROM customers WHERE company_id = :m "
            "AND name LIKE '__SBE_%__'"), {"m": mid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'sbe-%@x.test'"))
        conn.execute(text(
            "DELETE FROM plans WHERE code LIKE 'sbe-%'"))


def _bootstrap(owner_email="sbe-owner@x.test"):
    from app.models import Company, User, UserStatus, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash

    plan = Plan(code=f"sbe-{owner_email}",
                 name="SBE", name_ar="اختبار",
                 is_active=True,
                 price_monthly=Decimal("500"),
                 price_yearly=None)
    db.session.add(plan); db.session.flush()

    now = datetime.utcnow()
    c = Company(name=f"__SBE_{plan.id}__", base_currency="EGP",
                 subdomain=f"sbe-{plan.id}",
                 subscription_started_at=now,
                 subscription_expires_at=now + timedelta(days=14),
                 subscription_frequency="MONTHLY",
                 intended_plan_id=plan.id)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    u = User(email=owner_email,
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="sbe-owner", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=now,
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    return c, u, plan


@check("1. ensure_saas_customer copies owner email to mirror Customer")
def _():
    from app.services import saas_billing as _sb
    _teardown()
    c, u, plan = _bootstrap(owner_email="sbe-check1@x.test")
    cust = _sb.ensure_saas_customer(c)
    db.session.commit()
    assert cust.email == "sbe-check1@x.test", \
        f"mirror email={cust.email!r}, want owner email"
    return f"customer #{cust.id}.email={cust.email}"


@check("2. Mirror Customer email stays in sync when owner changes email")
def _():
    from app.services import saas_billing as _sb
    from app.models import User
    _teardown()
    c, u, plan = _bootstrap(owner_email="sbe-check2a@x.test")
    cust = _sb.ensure_saas_customer(c)
    db.session.commit()
    # Owner changes email.
    fresh_u = db.session.get(User, u.id)
    fresh_u.email = "sbe-check2b@x.test"
    db.session.commit()
    # Re-fetch mirror.
    cust2 = _sb.ensure_saas_customer(c)
    db.session.commit()
    assert cust2.id == cust.id, "mirror recreated instead of updated"
    assert cust2.email == "sbe-check2b@x.test", \
        f"mirror email stale: {cust2.email}"
    return f"mirror updated to {cust2.email}"


@check("3. create_first_invoice calls send_invoice_notification once")
def _():
    from app.services import saas_billing as _sb
    _teardown()
    c, u, plan = _bootstrap(owner_email="sbe-check3@x.test")
    with patch(
        "app.services.invoicing.send_invoice_notification"
    ) as mock_send:
        inv = _sb.create_first_invoice(c)
        db.session.commit()
    assert inv is not None
    assert mock_send.called, "send_invoice_notification not called"
    # Called once with the created invoice as the arg.
    assert mock_send.call_count == 1, \
        f"called {mock_send.call_count} times"
    call_arg = mock_send.call_args.args[0]
    assert call_arg.id == inv.id, \
        f"emailed wrong invoice: id={call_arg.id}, want {inv.id}"
    return f"email fired for invoice #{inv.id}"


@check("4. process_saas_next_invoices emails the NEXT invoice on due date")
def _():
    from app.services import saas_billing as _sb
    from app.models import User, UserStatus
    from werkzeug.security import generate_password_hash
    from datetime import date
    _teardown()
    c, u, plan = _bootstrap(owner_email="sbe-check4@x.test")
    first = _sb.create_first_invoice(c)
    admin = User(email="sbe-admin@x.test",
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name="sbe-admin", is_active=True,
                  status=UserStatus.ACTIVE.value)
    db.session.add(admin); db.session.commit()
    _sb.mark_saas_invoice_paid(first, admin.id)
    # MARSOUD-SAAS-DEFERRED-INVOICE-01 (Batch 8 Ticket 2) — the
    # NEXT invoice is now created + emailed by the cron sweep,
    # not at payment time. Simulate the cron day.
    c.next_billing_date = date.today()
    db.session.commit()
    with patch(
        "app.services.invoicing.send_invoice_notification"
    ) as mock_send:
        _sb.process_saas_next_invoices()
    assert mock_send.called, "no email fired by cron for next invoice"
    return f"cron fired email {mock_send.call_count}× for renewal"


@check("5. Email failure inside _try_email doesn't break invoice creation")
def _():
    from app.services import saas_billing as _sb
    from app.models import Invoice
    _teardown()
    c, u, plan = _bootstrap(owner_email="sbe-check5@x.test")
    with patch(
        "app.services.invoicing.send_invoice_notification",
        side_effect=RuntimeError("SMTP down"),
    ):
        inv = _sb.create_first_invoice(c)
        db.session.commit()
    assert inv is not None
    # Confirm the invoice actually persisted to the DB.
    assert db.session.get(Invoice, inv.id) is not None
    return "invoice persisted despite SMTP failure"


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
