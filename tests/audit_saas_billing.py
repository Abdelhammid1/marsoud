#!/usr/bin/env python3
"""MARSOUD-SAAS-BILLING-01 (Abdelhamid 2026-07-29).

Batch 5 Ticket 7 audit. End-to-end SaaS billing loop:
  · Signup + plan pick → auto-create Manasty-side invoice.
  · Coupon-on-choose-plan → applied to first invoice, redeemed
    after payment succeeds.
  · Admin marks paid → payment posted + subscription renewed +
    next invoice created.
  · Plan price changes don't auto-apply — price_lock overrides.
  · Cross-tenant safety: tenant's invoice lives in Manasty's
    books, not in the tenant's own ledger.
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
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text, inspect
    from app.services.manasty import manasty_id
    # Kill any outstanding session state so the raw-engine
    # transaction can grab the DB lock cleanly.
    db.session.rollback()
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        # Clean tenant fixtures.
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__SB_%__'"))]
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
        # Clean Manasty-side fixtures we created (customers +
        # invoices).
        mid = manasty_id()
        conn.execute(text(
            "DELETE FROM invoice_items WHERE invoice_id IN "
            "(SELECT id FROM invoices WHERE company_id = :m "
            "AND source = 'SAAS_BILLING' AND notes LIKE '%__SB_%')"),
            {"m": mid})
        conn.execute(text(
            "DELETE FROM invoices WHERE company_id = :m "
            "AND source = 'SAAS_BILLING' AND notes LIKE '%__SB_%'"),
            {"m": mid})
        conn.execute(text(
            "DELETE FROM customers WHERE company_id = :m "
            "AND name LIKE '__SB_%__'"), {"m": mid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'sb-%@x.test'"))
        conn.execute(text(
            "DELETE FROM coupons WHERE code LIKE 'SB-%'"))
        conn.execute(text(
            "DELETE FROM plans WHERE code LIKE 'sb-%'"))


def _bootstrap(freq="MONTHLY", price_monthly=Decimal("1000"),
                 price_yearly=None, coupon=False):
    """Set up a tenant company + owner. Returns (company, user, plan)."""
    from app.models import (
        Company, User, UserStatus, Plan,
        Coupon, DISCOUNT_PERCENT,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash

    plan = Plan(code=f"sb-{freq.lower()}-{price_monthly}",
                 name=f"SB-{freq}", name_ar="اختبار",
                 is_active=True,
                 price_monthly=price_monthly,
                 price_yearly=price_yearly)
    db.session.add(plan); db.session.flush()

    now = datetime.utcnow()
    c = Company(name=f"__SB_{freq}_{plan.id}__",
                 base_currency="EGP",
                 subdomain=f"sb-{plan.id}",
                 subscription_started_at=now,
                 subscription_expires_at=now + timedelta(days=14),
                 subscription_frequency=freq,
                 intended_plan_id=plan.id)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    u = User(email=f"sb-{plan.id}@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="sb-owner", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))

    if coupon:
        co = Coupon(code=f"SB-CO-{plan.id}",
                     discount_type=DISCOUNT_PERCENT,
                     discount_value=Decimal("20"),
                     active=True)
        db.session.add(co); db.session.flush()
        c.applied_coupon_id = co.id

    db.session.commit()
    return c, u, plan


@check("1. Monthly plan → first invoice created in Manasty's books")
def _():
    from app.services import saas_billing as _sb
    from app.services.manasty import manasty_id
    _teardown()
    c, u, plan = _bootstrap(freq="MONTHLY",
                             price_monthly=Decimal("1500"))
    inv = _sb.create_first_invoice(c)
    assert inv is not None, "no invoice created"
    assert inv.company_id == manasty_id(), \
        f"invoice landed in {inv.company_id}, expected Manasty"
    assert inv.customer_id == c.saas_customer_id
    # Total = subtotal (no coupon, no tax).
    assert Decimal(str(inv.total)) == Decimal("1500"), \
        f"total={inv.total}"
    assert inv.due_date == c.subscription_expires_at.date()
    return f"invoice #{inv.number} = {inv.total} EGP due {inv.due_date}"


@check("2. Yearly plan → uses price_yearly")
def _():
    from app.services import saas_billing as _sb
    _teardown()
    c, u, plan = _bootstrap(freq="YEARLY",
                             price_monthly=Decimal("1500"),
                             price_yearly=Decimal("15000"))
    inv = _sb.create_first_invoice(c)
    assert Decimal(str(inv.total)) == Decimal("15000")
    return f"yearly invoice = {inv.total}"


@check("3. Idempotent: second call returns the same outstanding invoice")
def _():
    from app.services import saas_billing as _sb
    _teardown()
    c, u, plan = _bootstrap(price_monthly=Decimal("1200"))
    inv1 = _sb.create_first_invoice(c)
    inv2 = _sb.create_first_invoice(c)
    assert inv1.id == inv2.id, \
        f"duplicate invoice created: {inv1.id} vs {inv2.id}"
    return f"same invoice #{inv1.id} returned on re-call"


@check("4. Coupon-on-choose-plan applies as invoice discount")
def _():
    from app.services import saas_billing as _sb
    _teardown()
    c, u, plan = _bootstrap(price_monthly=Decimal("1000"),
                             coupon=True)
    inv = _sb.create_first_invoice(c)
    # 20% off 1000 = 200 discount → total = 800.
    assert Decimal(str(inv.total)) == Decimal("800"), \
        f"expected 800, got {inv.total}"
    return f"invoice with 20% coupon = {inv.total}"


@check("5. price_lock overrides plan price on subsequent invoice creation")
def _():
    from app.services import saas_billing as _sb
    _teardown()
    c, u, plan = _bootstrap(price_monthly=Decimal("2000"))
    c.price_lock = Decimal("800")
    db.session.commit()
    inv = _sb.create_first_invoice(c)
    assert Decimal(str(inv.total)) == Decimal("800"), \
        f"price_lock ignored: got {inv.total}"
    return f"price_lock honored: {inv.total} (not plan 2000)"


@check("6. Mark paid → payment recorded + subscription renewed + next invoice created")
def _():
    from app.services import saas_billing as _sb
    from app.models import Invoice, InvoiceStatus, User, UserStatus
    from werkzeug.security import generate_password_hash
    _teardown()
    c, u, plan = _bootstrap(freq="MONTHLY",
                             price_monthly=Decimal("500"))
    inv = _sb.create_first_invoice(c)
    inv_id = inv.id
    prev_expiry = c.subscription_expires_at
    # Create a fake super-admin user for the payment
    admin = User(email="sb-admin-x@x.test",
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name="sb-admin", is_active=True,
                  status=UserStatus.ACTIVE.value)
    db.session.add(admin); db.session.commit()
    tenant = _sb.mark_saas_invoice_paid(inv, admin.id)
    db.session.expire_all()
    inv = db.session.get(Invoice, inv_id)
    assert inv.status == InvoiceStatus.PAID, \
        f"invoice status = {inv.status}"
    # Subscription extended by 30 days.
    assert tenant.subscription_expires_at > prev_expiry, \
        "subscription not renewed"
    diff_days = (tenant.subscription_expires_at - prev_expiry).days
    assert 25 <= diff_days <= 35, \
        f"renewal off: {diff_days} days added (expected ~30)"
    # NEXT invoice was created.
    next_inv = Invoice.query.filter(
        Invoice.customer_id == c.saas_customer_id,
        Invoice.status.in_([InvoiceStatus.DRAFT, InvoiceStatus.SENT]),
    ).first()
    assert next_inv is not None, "next invoice was not created"
    return f"paid {inv.id}, renewed +{diff_days}d, next invoice #{next_inv.id}"


@check("7. Cross-tenant: SaaS invoice lives in Manasty, NOT in tenant's own books")
def _():
    from app.services import saas_billing as _sb
    from app.services.manasty import manasty_id
    from app.models import Invoice
    _teardown()
    c, u, plan = _bootstrap(price_monthly=Decimal("999"))
    inv = _sb.create_first_invoice(c)
    # Search tenant's books for this invoice — should NOT be there.
    tenant_invs = Invoice.query.filter_by(
        company_id=c.id, source="SAAS_BILLING").all()
    assert tenant_invs == [], \
        f"tenant's books leaked SaaS invoice: {tenant_invs}"
    manasty_invs = Invoice.query.filter_by(
        company_id=manasty_id(), id=inv.id).all()
    assert len(manasty_invs) == 1
    return "isolation confirmed: invoice in Manasty only"


@check("8. Coupon redeemed after payment; applied_coupon_id cleared")
def _():
    from app.services import saas_billing as _sb
    from app.models import (
        User, UserStatus, CouponRedemption, Company, Coupon,
    )
    from werkzeug.security import generate_password_hash
    _teardown()
    c, u, plan = _bootstrap(price_monthly=Decimal("1000"),
                             coupon=True)
    coupon_id = c.applied_coupon_id
    inv = _sb.create_first_invoice(c)
    admin = User(email="sb-admin-y@x.test",
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name="sb-admin", is_active=True,
                  status=UserStatus.ACTIVE.value)
    db.session.add(admin); db.session.commit()
    _sb.mark_saas_invoice_paid(inv, admin.id)
    db.session.expire_all()
    tenant = db.session.get(Company, c.id)
    assert tenant.applied_coupon_id is None, \
        f"applied_coupon_id not cleared: {tenant.applied_coupon_id}"
    reds = CouponRedemption.query.filter_by(
        coupon_id=coupon_id, company_id=tenant.id).all()
    assert len(reds) == 1, f"expected 1 redemption, got {len(reds)}"
    return "coupon redeemed + applied_coupon_id cleared"


@check("9. next_billing_date: monthly=3d before, yearly=30d before")
def _():
    from app.services import saas_billing as _sb
    from app.models import Company
    exp = datetime.utcnow() + timedelta(days=60)
    monthly_c = Company(subscription_expires_at=exp,
                          subscription_frequency="MONTHLY")
    yearly_c = Company(subscription_expires_at=exp,
                         subscription_frequency="YEARLY")
    m_date = _sb.next_billing_date(monthly_c)
    y_date = _sb.next_billing_date(yearly_c)
    m_diff = (exp.date() - m_date).days
    y_diff = (exp.date() - y_date).days
    assert m_diff == 3, f"monthly offset = {m_diff}, want 3"
    assert y_diff == 30, f"yearly offset = {y_diff}, want 30"
    return "offsets correct"


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
