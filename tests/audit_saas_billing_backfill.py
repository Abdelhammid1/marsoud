#!/usr/bin/env python3
"""MARSOUD-SAAS-BILLING-BACKFILL-01 (Abdelhamid 2026-07-29).

Batch 6 Ticket 2 audit. Two parts:

  Part A — unified payment path: record_payment() on a SaaS
  invoice from ANY screen (regular /invoices/pay) produces the
  same effect as clicking 'تم الدفع' on /admin/saas (renewal +
  next invoice creation).

  Part B — flask saas-backfill CLI: creates a first SaaS invoice
  for OLD companies that have a chosen plan but no invoice yet.
  Idempotent.

Checks:
  1. record_payment on a SaaS invoice → subscription renews +
     next invoice created (Part A).
  2. record_payment on a non-SaaS invoice → subscription
     UNCHANGED (Part A regression — no side effects on regular
     bills).
  3. record_payment on a PARTIALLY-paid SaaS invoice → renewal
     does NOT fire until full payment (Part A idempotency edge).
  4. Backfill: company with plan + no invoice → invoice created.
  5. Backfill: company without a plan → skipped entirely.
  6. Backfill: company with existing outstanding invoice → skip
     + no duplicate.
  7. Backfill: company without subscription_frequency → forced to
     MONTHLY.
"""
import os
import sys
from datetime import datetime, timedelta
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
    from app.services.manasty import manasty_id
    db.session.rollback()
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__SBB_%__'"))]
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
            "AND notes LIKE '%__SBB_%')"), {"m": mid})
        conn.execute(text(
            "DELETE FROM invoices WHERE company_id = :m "
            "AND notes LIKE '%__SBB_%'"), {"m": mid})
        conn.execute(text(
            "DELETE FROM customers WHERE company_id = :m "
            "AND name LIKE '__SBB_%__'"), {"m": mid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'sbb-%@x.test'"))
        conn.execute(text(
            "DELETE FROM plans WHERE code LIKE 'sbb-%'"))


def _bootstrap(suffix, freq="MONTHLY", with_plan=True, expired=False):
    from app.models import (
        Company, User, UserStatus, Plan,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash

    plan = None
    if with_plan:
        plan = Plan(code=f"sbb-{suffix}",
                     name=f"SBB-{suffix}", name_ar="اختبار",
                     is_active=True,
                     price_monthly=Decimal("500"))
        db.session.add(plan); db.session.flush()

    now = datetime.utcnow()
    exp = (now - timedelta(days=5)) if expired \
        else (now + timedelta(days=14))
    c = Company(name=f"__SBB_{suffix}__", base_currency="EGP",
                 subdomain=f"sbb-{suffix.lower()}",
                 subscription_started_at=now,
                 subscription_expires_at=exp,
                 subscription_frequency=freq,
                 intended_plan_id=plan.id if plan else None)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    u = User(email=f"sbb-{suffix.lower()}@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name=f"sbb-{suffix}", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=now)
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    return c, u, plan


# ─── Part A: unified payment path ────────────────────────────────
@check("1. record_payment on SaaS invoice → subscription renews + next invoice")
def _():
    from app.services import saas_billing as _sb
    from app.services.invoicing import record_payment
    from app.models import Invoice, InvoiceStatus, User, UserStatus
    from werkzeug.security import generate_password_hash
    _teardown()
    c, u, plan = _bootstrap("A")
    inv = _sb.create_first_invoice(c)
    db.session.commit()
    prev_expiry = c.subscription_expires_at
    admin = User(email="sbb-admin-a@x.test",
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name="sbb-admin", is_active=True,
                  status=UserStatus.ACTIVE.value)
    db.session.add(admin); db.session.commit()
    # Use record_payment DIRECTLY (simulating the regular invoice
    # payment form flow, not mark_saas_invoice_paid).
    record_payment(
        invoice=inv, amount=float(inv.total),
        method="cash",
        created_by=admin.id, notify=False,
    )
    db.session.expire_all()
    fresh_inv = db.session.get(Invoice, inv.id)
    from app.models import Company
    tenant = db.session.get(Company, c.id)
    assert fresh_inv.status == InvoiceStatus.PAID, \
        f"invoice not PAID: {fresh_inv.status}"
    assert tenant.subscription_expires_at > prev_expiry, \
        "subscription not renewed via record_payment path"
    next_inv = (Invoice.query
                  .filter(Invoice.customer_id == tenant.saas_customer_id,
                            Invoice.id != inv.id,
                            Invoice.source == "SAAS_BILLING")
                  .first())
    assert next_inv is not None, \
        "no NEXT invoice created via record_payment path"
    return f"paid #{inv.id} via record_payment → renewed + next #{next_inv.id}"


@check("2. record_payment on NON-SaaS invoice → subscription UNTOUCHED")
def _():
    from app.services.invoicing import record_payment
    from app.models import (
        Invoice, InvoiceStatus, User, UserStatus, Customer,
    )
    from werkzeug.security import generate_password_hash
    from datetime import date
    _teardown()
    c, u, plan = _bootstrap("B")
    prev_expiry = c.subscription_expires_at
    # Regular customer + regular invoice inside the tenant's OWN
    # books (not Manasty).
    cust = Customer(company_id=c.id, name="Regular Customer")
    db.session.add(cust); db.session.flush()
    from app.services.subsidiary import ensure_customer_account
    ensure_customer_account(cust)
    inv = Invoice(company_id=c.id, customer_id=cust.id,
                   number="REG-0001",
                   issue_date=date.today(),
                   due_date=date.today() + timedelta(days=30),
                   currency="EGP", tax_rate=0,
                   status=InvoiceStatus.SENT,
                   source="MANUAL")
    db.session.add(inv); db.session.flush()
    from app.models.invoice import InvoiceItem
    db.session.add(InvoiceItem(
        invoice_id=inv.id, company_id=c.id,
        description="Regular sale", quantity=1, unit_price=100))
    inv.recalc(); db.session.commit()
    admin = User(email="sbb-admin-b@x.test",
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name="sbb-admin", is_active=True,
                  status=UserStatus.ACTIVE.value)
    db.session.add(admin); db.session.commit()
    record_payment(inv, amount=float(inv.total),
                    method="cash", created_by=admin.id, notify=False)
    db.session.expire_all()
    from app.models import Company
    tenant = db.session.get(Company, c.id)
    assert tenant.subscription_expires_at == prev_expiry, \
        "subscription changed for non-SaaS payment (leak!)"
    return "non-SaaS payment → subscription untouched"


@check("3. Partially-paid SaaS invoice → renewal does NOT fire yet")
def _():
    from app.services import saas_billing as _sb
    from app.services.invoicing import record_payment
    from app.models import Invoice, InvoiceStatus, User, UserStatus, Company
    from werkzeug.security import generate_password_hash
    _teardown()
    c, u, plan = _bootstrap("C")
    inv = _sb.create_first_invoice(c)
    db.session.commit()
    prev_expiry = c.subscription_expires_at
    admin = User(email="sbb-admin-c@x.test",
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name="sbb-admin", is_active=True,
                  status=UserStatus.ACTIVE.value)
    db.session.add(admin); db.session.commit()
    # Pay HALF the invoice.
    half = float(inv.total) / 2
    record_payment(inv, amount=half, method="cash",
                    created_by=admin.id, notify=False)
    db.session.expire_all()
    tenant = db.session.get(Company, c.id)
    assert tenant.subscription_expires_at == prev_expiry, \
        "subscription renewed on partial payment (bug)"
    # No next invoice yet.
    next_invs = (Invoice.query
                    .filter(Invoice.customer_id == tenant.saas_customer_id,
                              Invoice.id != inv.id,
                              Invoice.source == "SAAS_BILLING")
                    .all())
    assert not next_invs, \
        f"next invoice created on partial payment: {next_invs}"
    return "partial payment → renewal correctly skipped"


# ─── Part B: backfill CLI ───────────────────────────────────────
def _run_backfill():
    """Invoke the backfill CLI command in-process."""
    from click.testing import CliRunner
    from app.cli import saas_backfill_command
    return CliRunner().invoke(saas_backfill_command, [])


@check("4. Backfill creates invoice for company with plan + no invoice")
def _():
    from app.models import Invoice, InvoiceStatus
    _teardown()
    c, u, plan = _bootstrap("D")
    result = _run_backfill()
    assert result.exit_code == 0, \
        f"CLI failed: exit={result.exit_code}, output={result.output}"
    inv = Invoice.query.filter_by(
        customer_id=c.saas_customer_id if c.saas_customer_id else -1,
        source="SAAS_BILLING",
    ).first() if c.saas_customer_id else None
    db.session.expire_all()
    from app.models import Company
    fresh_c = db.session.get(Company, c.id)
    inv = Invoice.query.filter_by(
        customer_id=fresh_c.saas_customer_id,
        source="SAAS_BILLING").first() if fresh_c.saas_customer_id else None
    assert inv is not None, "no invoice created by backfill"
    return f"created invoice #{inv.number}"


@check("5. Backfill SKIPS companies without a plan")
def _():
    from app.models import Invoice, Company
    _teardown()
    # Company WITH plan (should get an invoice).
    c_ok, _, _ = _bootstrap("E1")
    # Company WITHOUT plan (should be skipped).
    c_skip, _, _ = _bootstrap("E2", with_plan=False)
    assert c_skip.intended_plan_id is None
    _run_backfill()
    db.session.expire_all()
    fresh_skip = db.session.get(Company, c_skip.id)
    assert fresh_skip.saas_customer_id is None, \
        "backfill touched a company without a plan (bug)"
    return "no-plan company left alone"


@check("6. Backfill is idempotent: rerun doesn't spawn duplicates")
def _():
    from app.models import Invoice, Company
    _teardown()
    c, u, plan = _bootstrap("F")
    _run_backfill()
    db.session.expire_all()
    c1 = db.session.get(Company, c.id)
    count1 = Invoice.query.filter_by(
        customer_id=c1.saas_customer_id,
        source="SAAS_BILLING").count()
    # Re-run.
    _run_backfill()
    db.session.expire_all()
    c2 = db.session.get(Company, c.id)
    count2 = Invoice.query.filter_by(
        customer_id=c2.saas_customer_id,
        source="SAAS_BILLING").count()
    assert count1 == count2, \
        f"duplicate invoice created: {count1} → {count2}"
    assert count1 == 1, f"expected 1 invoice, got {count1}"
    return f"stable at {count1} invoice across 2 runs"


@check("7. Backfill forces MONTHLY when subscription_frequency is NULL")
def _():
    from app.models import Company
    from sqlalchemy import text
    _teardown()
    c, u, plan = _bootstrap("G")
    # Force NULL freq post-bootstrap (bootstrap defaults to MONTHLY).
    with db.engine.begin() as conn:
        conn.execute(text(
            "UPDATE companies SET subscription_frequency = NULL "
            "WHERE id = :i"), {"i": c.id})
    db.session.expire_all()
    _run_backfill()
    db.session.expire_all()
    fresh = db.session.get(Company, c.id)
    assert fresh.subscription_frequency == "MONTHLY", \
        f"freq not forced: got {fresh.subscription_frequency}"
    return "MONTHLY forced on the NULL company"


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
