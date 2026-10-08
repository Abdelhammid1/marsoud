#!/usr/bin/env python3
"""MARSOUD-SAAS-ZERO-INVOICE-GUARD-01 + MARSOUD-SAAS-EMAIL-VALIDATOR-01 —
the SaaS billing cron must survive both a free-plan tenant (zero-total
invoice) and a tenant with a malformed email.

Before this ticket, `process_saas_next_invoices` failed for tenant 47
every day with:
  * `LedgerError: القيد لا يمكن أن يكون بقيمة صفر` because
    `_create_next_cycle_invoice` happily built an invoice with total=0
    and then `post_journal` refused the JE at
    `app/services/ledger.py:53`.
  * `smtplib.SMTPRecipientsRefused` because tenant 47's owner email
    was malformed and `ensure_saas_customer` propagated it verbatim
    to the saas Customer mirror.

Four checks:
  1. Zero-price plan -> Invoice is PAID with total 0 and NO
     JournalEntry was written for it.
  2. Non-zero plan -> normal path: Invoice + 1 JournalEntry (the
     accrual) land as before.
  3. `_is_valid_email` behaves on realistic input (positive / negative
     cases, incl. the kind of garbage tenant 47 had).
  4. `ensure_saas_customer` with a malformed owner email leaves the
     mirror's `email` NULL instead of propagating the garbage.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__SAAS_BILLING_ZERO_EMAIL_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import Company, Customer, Plan, User, Invoice, JournalEntry
    from app.models.user import user_companies
    from sqlalchemy import text

    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    for name in (COMPANY_NAME, COMPANY_NAME + "_OWNER_HOST"):
        for c in Company.query.filter_by(name=name).all():
            _teardown_company_hard(c.id)
    for email in ("__saas_zero_owner__@audit.local",
                   "__saas_bad_owner__@audit.local"):
        for u in User.query.filter_by(email=email).all():
            db.session.execute(user_companies.delete().where(
                user_companies.c.user_id == u.id))
            db.session.delete(u)
    db.session.execute(text("DELETE FROM plans WHERE code LIKE '__SAAS_AUD_%'"))
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()

    # Need a "Manasty" host company first so manasty_id() has a target
    # to write the saas Customer row against.  Find the first company
    # (manasty_id() does `Company.query.first().id` or similar); we
    # reuse the test tenant's own company_id for simplicity here —
    # saas_billing's manasty_id() is a package-level choice we can't
    # safely override, so we just confirm the fixture tenants can
    # be created and the two unit paths work; the mirror customer
    # test runs standalone against ensure_saas_customer.
    plan_zero = Plan(code="__SAAS_AUD_ZERO__", name="free",
                      name_ar="free",
                      price_monthly=Decimal("0.00"),
                      price_yearly=Decimal("0.00"),
                      is_active=True)
    plan_paid = Plan(code="__SAAS_AUD_PAID__", name="paid",
                      name_ar="paid",
                      price_monthly=Decimal("49.00"),
                      price_yearly=Decimal("499.00"),
                      is_active=True)
    db.session.add_all([plan_zero, plan_paid]); db.session.flush()
    _STATE["plan_zero_id"] = plan_zero.id
    _STATE["plan_paid_id"] = plan_paid.id
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


# ─── 3. Validator unit tests (no DB) ─────────────────────────────
@check("3. _is_valid_email accepts good, rejects bad")
def _():
    from app.services.saas_billing import _is_valid_email
    good = ["x@y.com", "abc.def+tag@example.co.uk", "a_b@sub.domain.io"]
    bad = ["", "abc", "x@", "@y.com", "x@y", "spaces here@y.com",
            "x@y..com", None, "   ", "x@y.", "x.y.com"]
    for s in good:
        assert _is_valid_email(s), f"good email rejected: {s!r}"
    for s in bad:
        assert not _is_valid_email(s), f"bad email accepted: {s!r}"
    return f"{len(good)} valid + {len(bad)} invalid"


# ─── 1. Zero-price invoice: PAID, no JE ──────────────────────────
@check("1. zero-price plan -> Invoice PAID + no JournalEntry")
def _():
    from app.services.saas_billing import _create_next_cycle_invoice
    from app.models import Company, Customer, Invoice, JournalEntry
    from app.models.invoice import InvoiceStatus
    from app.services.seed_coa import seed_default_coa

    # Need a tenant Company + a saas Customer row (the mirror) to
    # call _create_next_cycle_invoice directly.  We seed both in a
    # fresh company so the test is self-contained; no manasty_id()
    # dance.
    host = Company(name=COMPANY_NAME, base_currency="EGP")
    db.session.add(host); db.session.flush()
    seed_default_coa(host.id)
    cust = Customer(company_id=host.id, name="free-tier tenant",
                      email="free@tenant.local", is_active=True)
    db.session.add(cust); db.session.flush()

    tenant = Company(
        name=COMPANY_NAME + "_OWNER_HOST",
        base_currency="EGP",
        intended_plan_id=_STATE["plan_zero_id"],
        saas_customer_id=cust.id,
        subscription_frequency="MONTHLY",
        subscription_expires_at=(
            __import__("datetime").datetime.now()
            + timedelta(days=30)
        ),
    )
    db.session.add(tenant); db.session.flush()
    _STATE["zero_tenant_id"] = tenant.id
    _STATE["zero_host_id"] = host.id

    inv = _create_next_cycle_invoice(tenant)
    assert inv is not None, "zero-price should still create an Invoice row"
    assert inv.status == InvoiceStatus.PAID, \
        f"expected PAID, got {inv.status}"
    assert float(inv.total or 0) < 0.005, \
        f"total should be 0, got {inv.total}"
    assert float(inv.paid_amount or 0) < 0.005, \
        f"paid_amount should be 0, got {inv.paid_amount}"

    je_rows = JournalEntry.query.filter_by(
        source_type="invoice", source_id=inv.id).all()
    assert not je_rows, \
        f"zero-price invoice must NOT post a JE, found {len(je_rows)}"
    return f"inv #{inv.id} PAID + 0 JEs"


# ─── 2. Non-zero plan still posts the JE ─────────────────────────
@check("2. non-zero plan still posts the full accrual JE")
def _():
    from app.services.saas_billing import _create_next_cycle_invoice
    from app.models import Company, JournalEntry
    from app.models.invoice import InvoiceStatus

    tenant = db.session.get(Company, _STATE["zero_tenant_id"])
    tenant.intended_plan_id = _STATE["plan_paid_id"]
    db.session.flush()

    inv = _create_next_cycle_invoice(tenant)
    assert inv is not None
    assert inv.status == InvoiceStatus.SENT, \
        f"expected SENT, got {inv.status}"
    assert float(inv.total) > 0, f"total should be > 0, got {inv.total}"

    je_rows = JournalEntry.query.filter_by(
        source_type="invoice", source_id=inv.id).all()
    assert je_rows, "non-zero invoice must post the accrual JE"
    return f"inv #{inv.id} SENT + {len(je_rows)} JE"


# ─── 4. ensure_saas_customer rejects a bad owner email ───────────
@check("4. ensure_saas_customer drops malformed owner email to NULL")
def _():
    from app.services.saas_billing import ensure_saas_customer, _is_valid_email
    # Build a synthetic company with a bad-email owner — we poke the
    # internal `_tenant_owner_email` by hand because adding a real
    # auth User chain is irrelevant to the invariant we're pinning.
    import app.services.saas_billing as saas_mod
    original = saas_mod._tenant_owner_email
    saas_mod._tenant_owner_email = lambda _c: "Bad recipient address syntax"
    original_phone = saas_mod._tenant_owner_phone
    saas_mod._tenant_owner_phone = lambda _c: None
    try:
        from app.models import Company
        host = db.session.get(Company, _STATE["zero_host_id"])
        tenant = Company(name=COMPANY_NAME + "_BADMAIL",
                           base_currency="EGP")
        db.session.add(tenant); db.session.flush()
        # Point tenant at the host as the saas-customer-manasty-id
        # surrogate — ensure_saas_customer calls manasty_id() to pick
        # the mirror's company_id.  We don't need to override that
        # for THIS assertion — just confirm no bad email lands.
        cust = ensure_saas_customer(tenant)
        assert cust.email is None, \
            f"bad owner email should drop to NULL, got {cust.email!r}"
        # Clean up the stray tenant immediately so teardown doesn't
        # inherit it.
        db.session.delete(cust)
        db.session.delete(tenant)
        db.session.commit()
    finally:
        saas_mod._tenant_owner_email = original
        saas_mod._tenant_owner_phone = original_phone
    return "mirror email left NULL"


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
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                for name in (COMPANY_NAME, COMPANY_NAME + "_OWNER_HOST",
                              COMPANY_NAME + "_BADMAIL"):
                    from app.models import Company
                    for c in Company.query.filter_by(name=name).all():
                        _teardown_company_hard(c.id)
                from sqlalchemy import text
                db.session.execute(text(
                    "DELETE FROM plans WHERE code LIKE '__SAAS_AUD_%'"))
                db.session.commit()
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
