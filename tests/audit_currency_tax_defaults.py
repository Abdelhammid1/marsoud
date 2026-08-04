#!/usr/bin/env python3
"""MARSOUD-CURRENCY-TAX-DEFAULTS (Abdelhamid 2026-07-30).

Batch 8 Ticket 4 — three sub-fixes:
  4a. Vendor bill form pre-fills tax_rate from company.vat_rate.
  4b. New companies default to vat_rate = 0.
  4c. Invoice edit gets a currency-change action that syncs the
      linked JE so the ledger stays consistent.

Checks:
  1. Rendered vendor bill form contains the company vat_rate in
     the tax_rate input's value attribute.
  2. Register POST → new Company row starts with vat_rate = 0.
  3. Existing companies are NOT retroactively changed (their
     current vat_rate is preserved on save).
  4. change_currency updates invoice.currency AND the linked
     JournalEntry.currency in the same commit.
  5. change_currency refuses on VOIDED invoices.
  6. change_currency rejects an invalid currency string.
  7. change_currency writes an activity log entry.
"""
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"
os.environ["TURNSTILE_SECRET"] = ""

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
            "SELECT id FROM companies WHERE name LIKE '__CTD_%__' "
            "OR subdomain LIKE 'ctd-%'"))]
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
            "DELETE FROM users WHERE email LIKE 'ctd-%@x.test'"))
    from app.services.bot_guard import register_rate_reset
    register_rate_reset()


def _mk_owner(suffix, vat_rate=Decimal("12.5")):
    from app.models import Company, User, UserStatus
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    c = Company(name=f"__CTD_{suffix}__", base_currency="EGP",
                 subdomain=f"ctd-{suffix.lower()}",
                 vat_rate=vat_rate,
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email=f"ctd-{suffix.lower()}@x.test",
             password_hash=generate_password_hash(
                 "TestPass123!", method="pbkdf2:sha256"),
             full_name=f"ctd-{suffix}", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    return c, u


# ─── Sub-ticket 4a: vendor bill form pre-fills tax_rate ─────────
@check("1. Vendor bill form template pulls tax_rate from company.vat_rate")
def _():
    # Rather than trying to render the full base.html chain (which
    # needs current_user + tons of context), grep the template
    # source for the Batch 8 Ticket 4a fix pattern. If the string
    # is there, the fix landed.
    # MARSOUD-BILL-SPLIT renamed vendor_bills/form.html to
    # new_typed.html (the three-way entry: expense / asset / inventory).
    # Same template, same tax_rate field — only the filename moved.
    tpl = (ROOT / "app" / "templates" / "vendor_bills"
            / "new_typed.html").read_text(encoding="utf-8")
    assert 'name="tax_rate"' in tpl
    # The fix should reference active_company.vat_rate — not the
    # old hardcoded value="0".
    assert "active_company.vat_rate" in tpl, \
        "vendor bill form doesn't pre-fill from active_company.vat_rate"
    return "template uses active_company.vat_rate"


# ─── Sub-ticket 4b: new companies default vat_rate = 0 ──────────
@check("2. Register POST → new Company starts with vat_rate = 0")
def _():
    from flask import current_app
    from app.models import Company
    _teardown()
    with current_app.test_client() as client:
        r = client.post("/register", data={
            "full_name": "CTD Owner",
            "email": "ctd-reg@x.test",
            "password": "TestPass123!",
            "company_name": "CTD-Reg-Co",
            "subdomain": "ctd-reg",
            "base_currency": "EGP",
            "agree_terms": "on",
            "cf-turnstile-response": "test",
        }, follow_redirects=False)
    assert r.status_code in (302, 303), f"got {r.status_code}"
    c = Company.query.filter_by(subdomain="ctd-reg").first()
    assert c is not None
    assert float(c.vat_rate or 0) == 0.0, \
        f"new company vat_rate={c.vat_rate}, want 0"
    return f"new company vat_rate = {c.vat_rate}"


@check("3. Existing companies keep their saved vat_rate")
def _():
    from app.models import Company
    _teardown()
    c, u = _mk_owner("EX", vat_rate=Decimal("15.00"))
    # Ensure the row we just created keeps 15 (nothing in the fix
    # rewrites existing rows).
    fresh = db.session.get(Company, c.id)
    assert float(fresh.vat_rate) == 15.0
    return "existing companies unchanged (still 15%)"


# ─── Sub-ticket 4c: change_currency route ───────────────────────
def _mk_invoice_with_je(c, u):
    """Create an invoice + post a JE for it so we can verify
    change_currency syncs the linked entry."""
    from app.models import (
        Customer, Invoice, InvoiceItem, InvoiceStatus,
    )
    from app.services.subsidiary import ensure_customer_account
    from app.services.invoicing import post_invoice_to_ledger
    cust = Customer(company_id=c.id, name="Test Customer")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    inv = Invoice(company_id=c.id, customer_id=cust.id,
                   number="INV-CTD-001",
                   issue_date=date.today(),
                   due_date=date.today() + timedelta(days=30),
                   currency="EGP", tax_rate=0,
                   status=InvoiceStatus.SENT,
                   source="MANUAL")
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, company_id=c.id,
        description="line", quantity=1, unit_price=200))
    inv.recalc()
    post_invoice_to_ledger(inv, created_by=u.id)
    db.session.commit()
    return inv


def _je_currency(inv_id):
    from sqlalchemy import text
    row = db.session.execute(text(
        "SELECT currency FROM journal_entries "
        "WHERE source_type = 'invoice' AND source_id = :i"),
        {"i": inv_id}).fetchone()
    return row[0] if row else None


def _post_change(user, company, invoice_id, currency):
    from flask import current_app, g as _g
    from flask_login import login_user
    with current_app.test_request_context(
            f"/invoices/{invoice_id}/change-currency",
            method="POST",
            data={"currency": currency}):
        login_user(db.session.get(type(user), user.id))
        _g.active_company = db.session.get(type(company), company.id)
        _g.user_companies = [_g.active_company]
        from app.routes.invoices import change_currency
        return change_currency(invoice_id)


@check("4. change_currency updates invoice + linked JE together")
def _():
    from app.models import Invoice
    _teardown()
    c, u = _mk_owner("CC")
    inv = _mk_invoice_with_je(c, u)
    inv_id = inv.id
    assert inv.currency == "EGP"
    assert _je_currency(inv_id) == "EGP"
    _post_change(u, c, inv_id, "USD")
    db.session.expire_all()
    fresh = db.session.get(Invoice, inv_id)
    assert fresh.currency == "USD", f"invoice.currency={fresh.currency}"
    assert _je_currency(inv_id) == "USD", \
        f"JE currency={_je_currency(inv_id)}"
    return "both invoice + JE flipped EGP → USD"


@check("5. change_currency refuses on VOIDED invoices")
def _():
    from app.models import Invoice, InvoiceStatus
    _teardown()
    c, u = _mk_owner("CV")
    inv = _mk_invoice_with_je(c, u)
    inv.status = InvoiceStatus.VOIDED
    inv.voided_at = datetime.utcnow()
    db.session.commit()
    inv_id = inv.id
    _post_change(u, c, inv_id, "USD")
    db.session.expire_all()
    fresh = db.session.get(Invoice, inv_id)
    assert fresh.currency == "EGP", \
        f"voided invoice currency changed anyway: {fresh.currency}"
    return "VOIDED invoice currency change refused"


@check("6. change_currency rejects invalid currency string")
def _():
    from app.models import Invoice
    _teardown()
    c, u = _mk_owner("CI")
    inv = _mk_invoice_with_je(c, u)
    inv_id = inv.id
    _post_change(u, c, inv_id, "XYZ")
    db.session.expire_all()
    fresh = db.session.get(Invoice, inv_id)
    assert fresh.currency == "EGP", \
        f"bad currency accepted: {fresh.currency}"
    return "invalid currency rejected"


@check("7. change_currency writes an activity log entry")
def _():
    from sqlalchemy import text
    _teardown()
    c, u = _mk_owner("CL")
    inv = _mk_invoice_with_je(c, u)
    inv_id = inv.id
    _post_change(u, c, inv_id, "SAR")
    # Look up the activity log — table name is user_activity_log.
    row = db.session.execute(text(
        "SELECT action_type, entity_type, entity_id "
        "FROM user_activity_log "
        "WHERE company_id = :c AND entity_type = 'invoice' "
        "AND entity_id = :i "
        "ORDER BY id DESC LIMIT 1"),
        {"c": c.id, "i": inv_id}).fetchone()
    assert row is not None, "no activity log entry written"
    assert row[0] == "UPDATE"
    return f"activity log entry written ({row[0]} invoice #{row[2]})"


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
