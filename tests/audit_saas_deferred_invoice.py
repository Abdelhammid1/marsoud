#!/usr/bin/env python3
"""MARSOUD-SAAS-DEFERRED-INVOICE-01 (Abdelhamid 2026-07-30).

Batch 8 Ticket 2. `_saas_post_payment()` used to create the next-
cycle invoice + post its JE at payment time — with an issue_date
months in the future. That leaked the row into /invoices/ and AR
aging long before its real date. Fix: DEFER creation to a daily
cron sweep. Payment now just stashes `Company.next_billing_date`;
the cron picks it up on the actual day.

Checks:
  1. Pay a SaaS invoice → NO next invoice row appears immediately.
  2. Company.next_billing_date is set to the expected offset
     (3 days before expiry for MONTHLY, 30 days for YEARLY).
  3. Cron BEFORE the due date → creates nothing, next_billing_date
     stays set.
  4. Cron ON the due date → creates the invoice + posts JE +
     clears next_billing_date.
  5. Cron twice on the same day → no duplicate (second pass
     finds next_billing_date NULL).
  6. First invoice (create_first_invoice) is UNCHANGED — still
     created immediately.
  7. Subscription renewal + coupon clearing STILL happen at
     payment time (only invoice creation is deferred).
  8. Any exception on one tenant doesn't stop the sweep.
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
    from app.services.manasty import manasty_id
    db.session.rollback()
    db.session.expunge_all()
    db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__SDI_%__'"))]
        for cid in cids:
            conn.execute(text(
                "UPDATE companies SET saas_customer_id = NULL, "
                "applied_coupon_id = NULL, "
                "next_billing_date = NULL WHERE id = :c"),
                {"c": cid})
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
        mid = manasty_id()
        conn.execute(text(
            "DELETE FROM invoice_items WHERE invoice_id IN "
            "(SELECT id FROM invoices WHERE company_id = :m "
            "AND source = 'SAAS_BILLING' AND notes LIKE '%__SDI_%')"),
            {"m": mid})
        conn.execute(text(
            "DELETE FROM invoices WHERE company_id = :m "
            "AND source = 'SAAS_BILLING' AND notes LIKE '%__SDI_%'"),
            {"m": mid})
        conn.execute(text(
            "DELETE FROM customers WHERE company_id = :m "
            "AND name LIKE '__SDI_%__'"), {"m": mid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'sdi-%@x.test'"))
        conn.execute(text(
            "DELETE FROM plans WHERE code LIKE 'sdi-%'"))


def _bootstrap(suffix, freq="MONTHLY"):
    from app.models import Company, User, UserStatus, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    plan = Plan(code=f"sdi-{suffix}", name=f"SDI-{suffix}",
                 name_ar="اختبار", is_active=True,
                 price_monthly=Decimal("500"),
                 price_yearly=Decimal("5500"))
    db.session.add(plan); db.session.flush()
    now = datetime.utcnow()
    c = Company(name=f"__SDI_{suffix}__", base_currency="EGP",
                 subdomain=f"sdi-{suffix.lower()}",
                 subscription_started_at=now,
                 subscription_expires_at=now + timedelta(days=14),
                 subscription_frequency=freq,
                 intended_plan_id=plan.id)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email=f"sdi-{suffix.lower()}@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name=f"sdi-{suffix}", is_active=True,
             status=UserStatus.ACTIVE.value)
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    return c, u, plan


def _count_saas_invoices_for_tenant(tenant_id):
    """Count non-first SaaS invoices for a tenant (the 'next
    cycle' invoices)."""
    from sqlalchemy import text
    from app.services.manasty import manasty_id
    row = db.session.execute(text(
        "SELECT COUNT(*) FROM invoices i "
        "JOIN customers c ON c.id = i.customer_id "
        "WHERE c.company_id = :m AND i.source = 'SAAS_BILLING' "
        "AND i.customer_id IN "
        "(SELECT saas_customer_id FROM companies WHERE id = :t)"),
        {"m": manasty_id(), "t": tenant_id}).scalar()
    return int(row or 0)


@check("1. Payment does NOT create a next invoice immediately")
def _():
    from app.services import saas_billing as _sb
    from app.services.invoicing import record_payment
    from app.models import User, UserStatus
    from werkzeug.security import generate_password_hash
    _teardown()
    c, u, plan = _bootstrap("A")
    first = _sb.create_first_invoice(c)
    db.session.commit()
    admin = User(email="sdi-admin-a@x.test",
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name="sdi-admin", is_active=True,
                  status=UserStatus.ACTIVE.value)
    db.session.add(admin); db.session.commit()
    before = _count_saas_invoices_for_tenant(c.id)
    record_payment(invoice=first, amount=float(first.total),
                    method="cash", created_by=admin.id, notify=False)
    db.session.expire_all()
    after = _count_saas_invoices_for_tenant(c.id)
    # Only the ORIGINAL first invoice — no new next-cycle one.
    assert after == before, \
        f"invoice count grew: {before} → {after}"
    return f"no new invoice created ({before} invoice(s) unchanged)"


@check("2. next_billing_date set at payment time (MONTHLY offset = 3d)")
def _():
    from app.services import saas_billing as _sb
    from app.services.invoicing import record_payment
    from app.models import Company, User, UserStatus
    from werkzeug.security import generate_password_hash
    _teardown()
    c, u, plan = _bootstrap("B", freq="MONTHLY")
    first = _sb.create_first_invoice(c)
    db.session.commit()
    admin = User(email="sdi-admin-b@x.test",
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name="sdi-admin", is_active=True,
                  status=UserStatus.ACTIVE.value)
    db.session.add(admin); db.session.commit()
    record_payment(invoice=first, amount=float(first.total),
                    method="cash", created_by=admin.id, notify=False)
    db.session.expire_all()
    tenant = db.session.get(Company, c.id)
    assert tenant.next_billing_date is not None, \
        "next_billing_date not set"
    delta = (tenant.subscription_expires_at.date()
              - tenant.next_billing_date).days
    assert delta == 3, f"MONTHLY offset = {delta}, want 3"
    return f"next_billing_date offset = {delta}d before expiry"


@check("3. Cron BEFORE due date → creates nothing, date preserved")
def _():
    from app.services import saas_billing as _sb
    from app.models import Company
    _teardown()
    c, u, plan = _bootstrap("C")
    # Directly set a future next_billing_date.
    c.next_billing_date = date.today() + timedelta(days=5)
    c.saas_customer_id = _sb.ensure_saas_customer(c).id
    db.session.commit()
    before = _count_saas_invoices_for_tenant(c.id)
    result = _sb.process_saas_next_invoices()
    db.session.expire_all()
    tenant = db.session.get(Company, c.id)
    assert result["scanned"] == 0, \
        f"scanned={result['scanned']}, want 0 (date not due)"
    assert tenant.next_billing_date is not None, \
        "date cleared before due"
    after = _count_saas_invoices_for_tenant(c.id)
    assert after == before, "invoice created before due"
    return f"future-dated tenant left alone"


@check("4. Cron ON due date → creates invoice + posts JE + clears date")
def _():
    from app.services import saas_billing as _sb
    from app.models import Company, Invoice
    from sqlalchemy import text
    _teardown()
    c, u, plan = _bootstrap("D")
    c.next_billing_date = date.today()
    c.saas_customer_id = _sb.ensure_saas_customer(c).id
    db.session.commit()
    before = _count_saas_invoices_for_tenant(c.id)
    result = _sb.process_saas_next_invoices()
    db.session.expire_all()
    tenant = db.session.get(Company, c.id)
    after = _count_saas_invoices_for_tenant(c.id)
    assert result["created"] == 1, \
        f"created={result['created']}, want 1"
    assert after == before + 1, \
        f"invoice count: {before} → {after}"
    assert tenant.next_billing_date is None, \
        "next_billing_date not cleared"
    # JE was posted for the new invoice.
    latest_inv = (Invoice.query
                    .filter(Invoice.customer_id == tenant.saas_customer_id,
                              Invoice.source == "SAAS_BILLING")
                    .order_by(Invoice.id.desc())
                    .first())
    je_count = db.session.execute(text(
        "SELECT COUNT(*) FROM journal_entries "
        "WHERE source_type = 'invoice' AND source_id = :i"),
        {"i": latest_inv.id}).scalar()
    assert je_count == 1, f"JE count = {je_count}, want 1"
    return f"invoice #{latest_inv.number} created + JE posted"


@check("5. Cron twice same day → no duplicate")
def _():
    from app.services import saas_billing as _sb
    from app.models import Company
    _teardown()
    c, u, plan = _bootstrap("E")
    c.next_billing_date = date.today()
    c.saas_customer_id = _sb.ensure_saas_customer(c).id
    db.session.commit()
    _sb.process_saas_next_invoices()
    first_count = _count_saas_invoices_for_tenant(c.id)
    _sb.process_saas_next_invoices()
    db.session.expire_all()
    second_count = _count_saas_invoices_for_tenant(c.id)
    assert first_count == second_count, \
        f"duplicate created: {first_count} → {second_count}"
    return f"stable at {first_count} across 2 runs"


@check("6. First invoice (create_first_invoice) UNCHANGED — still immediate")
def _():
    from app.services import saas_billing as _sb
    from app.models import Company
    _teardown()
    c, u, plan = _bootstrap("F")
    before = _count_saas_invoices_for_tenant(c.id)
    inv = _sb.create_first_invoice(c)
    db.session.commit()
    after = _count_saas_invoices_for_tenant(c.id)
    assert inv is not None
    assert after == before + 1, \
        f"first invoice count: {before} → {after}"
    tenant = db.session.get(Company, c.id)
    # next_billing_date should NOT be set by create_first_invoice
    # (only by payment).
    assert tenant.next_billing_date is None, \
        f"create_first_invoice leaked next_billing_date: "\
        f"{tenant.next_billing_date}"
    return "create_first_invoice still creates immediately"


@check("7. Subscription renewal STILL happens at payment time")
def _():
    from app.services import saas_billing as _sb
    from app.services.invoicing import record_payment
    from app.models import Company, User, UserStatus
    from werkzeug.security import generate_password_hash
    _teardown()
    c, u, plan = _bootstrap("G")
    first = _sb.create_first_invoice(c)
    db.session.commit()
    prev_expiry = c.subscription_expires_at
    admin = User(email="sdi-admin-g@x.test",
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name="sdi-admin", is_active=True,
                  status=UserStatus.ACTIVE.value)
    db.session.add(admin); db.session.commit()
    record_payment(invoice=first, amount=float(first.total),
                    method="cash", created_by=admin.id, notify=False)
    db.session.expire_all()
    tenant = db.session.get(Company, c.id)
    assert tenant.subscription_expires_at > prev_expiry, \
        "subscription not renewed at payment time (regression)"
    return "subscription renewed at payment time (as before)"


@check("8. Sweep tolerates per-tenant errors (continues to next)")
def _():
    from app.services import saas_billing as _sb
    _teardown()
    c1, u1, _ = _bootstrap("H1")
    c2, u2, _ = _bootstrap("H2")
    c1.next_billing_date = date.today()
    c2.next_billing_date = date.today()
    c1.saas_customer_id = _sb.ensure_saas_customer(c1).id
    c2.saas_customer_id = _sb.ensure_saas_customer(c2).id
    # Break c1 by clearing its intended_plan_id → create returns None,
    # sweep should still process c2 successfully.
    c1.intended_plan_id = None
    db.session.commit()
    result = _sb.process_saas_next_invoices()
    assert result["scanned"] == 2
    # c2 should have created; c1 not.
    c1_count = _count_saas_invoices_for_tenant(c1.id)
    c2_count = _count_saas_invoices_for_tenant(c2.id)
    assert c2_count == 1, f"c2 count = {c2_count}"
    return f"c1 skipped (no plan), c2 processed"


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
