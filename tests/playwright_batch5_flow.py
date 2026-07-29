#!/usr/bin/env python3
"""MARSOUD-BATCH-5-E2E (Abdelhamid 2026-07-29).

Real-browser walk of the 3 UI surfaces Batch 5 shipped:

  Ticket 4 + 7 (/choose-plan):
    - Coupon input field renders + placeholder is Arabic.
    - Frequency toggle renders + MONTHLY/YEARLY radios present.
    - Submitting with a valid coupon + YEARLY sets both on
      the company row.

  Ticket 5 (/calendar/):
    - "+ إضافة حدث" button opens the modal.
    - Modal has all 5 fields (title, starts_at, ends_at, location,
      reminder, description).
    - Submitting persists a CalendarEvent row.
    - The event shows up on the timeline with the 📌 icon.

  Ticket 7 (/admin/saas):
    - Page renders as super-admin.
    - Company row shows plan + frequency + outstanding invoice.
    - "💰 تم الدفع" button visible on outstanding invoice.

Runs against a live dev server (default localhost:5050). Set
BASE_URL to override.
"""
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = os.environ.get("BASE_URL", "http://localhost:5050")
SHOTS = ROOT / "tests" / "screenshots" / "batch5_flow"
SHOTS.mkdir(parents=True, exist_ok=True)


TEST_EMAIL = "pw-b5-owner@test.local"
SUPER_EMAIL = "pw-b5-super@test.local"
TEST_SUBDOMAIN = "pw-b5-owner"


def _teardown():
    """Wipe fixture rows so each run is clean."""
    from app import create_app, db
    from sqlalchemy import text, inspect
    from app.services.manasty import manasty_id
    app = create_app()
    with app.app_context():
        insp = inspect(db.engine)
        with db.engine.begin() as conn:
            cids = [r[0] for r in conn.execute(text(
                "SELECT id FROM companies WHERE subdomain = :s"),
                {"s": TEST_SUBDOMAIN})]
            for cid in cids:
                conn.execute(text(
                    "UPDATE companies SET saas_customer_id = NULL, "
                    "applied_coupon_id = NULL WHERE id = :c"),
                    {"c": cid})
                conn.execute(text(
                    "DELETE FROM user_companies WHERE company_id = :c"),
                    {"c": cid})
                for tbl in reversed(db.metadata.sorted_tables):
                    cols = {col["name"]
                            for col in insp.get_columns(tbl.name)}
                    if "company_id" in cols:
                        conn.execute(text(
                            f"DELETE FROM {tbl.name} "
                            f"WHERE company_id = :c"), {"c": cid})
                conn.execute(text(
                    "DELETE FROM companies WHERE id = :c"),
                    {"c": cid})
            # SaaS invoices in Manasty's books tied to us.
            mid = manasty_id()
            conn.execute(text(
                "DELETE FROM invoice_items WHERE invoice_id IN "
                "(SELECT id FROM invoices WHERE company_id = :m "
                "AND source = 'SAAS_BILLING' AND notes LIKE '%PW E2E B5%')"),
                {"m": mid})
            conn.execute(text(
                "DELETE FROM invoices WHERE company_id = :m "
                "AND source = 'SAAS_BILLING' AND notes LIKE '%PW E2E B5%'"),
                {"m": mid})
            conn.execute(text(
                "DELETE FROM customers WHERE company_id = :m "
                "AND name = :n"), {"m": mid, "n": "PW E2E B5 Company"})
            conn.execute(text(
                "DELETE FROM users WHERE email IN (:e1, :e2)"),
                {"e1": TEST_EMAIL, "e2": SUPER_EMAIL})
            conn.execute(text(
                "DELETE FROM coupons WHERE code = 'PWB5-20'"))
        from app.services.bot_guard import register_rate_reset
        register_rate_reset()


def _seed():
    """Create an owner + a coupon so /choose-plan has stuff to
    exercise. Returns (user_id, company_id, coupon_id)."""
    from app import create_app, db
    from app.models import (
        Company, User, UserStatus, Plan, Coupon, DISCOUNT_PERCENT,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    app = create_app()
    with app.app_context():
        plan = Plan.query.filter_by(is_active=True).first()
        now = datetime.utcnow()
        c = Company(name="PW E2E B5 Company", base_currency="EGP",
                     subdomain=TEST_SUBDOMAIN,
                     subscription_started_at=now,
                     subscription_expires_at=now + timedelta(days=14),
                     intended_plan_id=None)
        db.session.add(c); db.session.flush()
        seed_default_coa(c.id)
        u = User(email=TEST_EMAIL,
                 password_hash=generate_password_hash(
                     "TestPass123!", method="pbkdf2:sha256"),
                 full_name="PW E2E B5 Owner", is_active=True,
                 status=UserStatus.ACTIVE.value,
                 email_verified_at=now,
                 terms_version="TEST")
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=c.id, role="owner"))

        # Super-admin for /admin/saas.
        su = User(email=SUPER_EMAIL,
                  password_hash=generate_password_hash(
                      "SuperPass1!", method="pbkdf2:sha256"),
                  full_name="PW B5 Super", is_active=True,
                  status=UserStatus.ACTIVE.value,
                  email_verified_at=now,
                  is_superadmin=True,
                  terms_version="TEST")
        db.session.add(su); db.session.flush()

        co = Coupon(code="PWB5-20", discount_type=DISCOUNT_PERCENT,
                     discount_value=Decimal("20"), active=True)
        db.session.add(co); db.session.commit()
        return u.id, c.id, co.id, su.id, plan.id if plan else None


def main():
    from playwright.sync_api import sync_playwright
    _teardown()
    u_id, c_id, co_id, su_id, plan_id = _seed()

    passed = failed = 0
    results = []

    def check(label, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            results.append(f"PASS  {label}"
                           + (f"  ⇒ {detail}" if detail else ""))
        else:
            failed += 1
            results.append(f"FAIL  {label}"
                           + (f"  ⇒ {detail}" if detail else ""))

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            ctx = b.new_context(
                viewport={"width": 1400, "height": 900}, locale="ar")

            # ─── OWNER SESSION ───────────────────────────────
            page = ctx.new_page()
            page.goto(f"{BASE}/login", wait_until="networkidle")
            page.fill("input[name='email']", TEST_EMAIL)
            page.fill("input[name='password']", "TestPass123!")
            with page.expect_navigation(wait_until="networkidle",
                                          timeout=15000):
                page.click("button[type='submit']")

            # ─── /choose-plan surface ────────────────────────
            page.goto(f"{BASE}/choose-plan", wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "01_choose_plan.png"))
            has_coupon_field = page.locator(
                "input[name='coupon_code']").count() > 0
            check("T4.1 Coupon input renders on /choose-plan",
                  has_coupon_field)
            coupon_ph = ""
            if has_coupon_field:
                coupon_ph = page.locator(
                    "input[name='coupon_code']").get_attribute(
                        "placeholder") or ""
            check("T4.2 Coupon placeholder is Arabic hint",
                  "WELCOME" in coupon_ph or "مثال" in coupon_ph,
                  detail=f"placeholder='{coupon_ph}'")
            freq_monthly = page.locator(
                "input[name='frequency'][value='MONTHLY']").count() > 0
            freq_yearly = page.locator(
                "input[name='frequency'][value='YEARLY']").count() > 0
            check("T7.1 Monthly frequency radio present",
                  freq_monthly)
            check("T7.2 Yearly frequency radio present",
                  freq_yearly)

            # Submit with valid coupon + YEARLY.
            if plan_id:
                # Radios are `sr-only` (Tailwind peer pattern) — the
                # visible <label> intercepts pointer events. Set the
                # checked state directly via JS since the actual form
                # POST reads the input's value, not clicks.
                page.evaluate(
                    "(pid) => {"
                    "document.querySelector("
                    "  `input[name='plan_id'][value='${pid}']`"
                    ").checked = true;"
                    "document.querySelector("
                    "  `input[name='frequency'][value='YEARLY']`"
                    ").checked = true;"
                    "}", plan_id)
                page.fill("input[name='coupon_code']", "PWB5-20")
                page.screenshot(path=str(SHOTS / "02_choose_plan_filled.png"))
                with page.expect_navigation(wait_until="networkidle",
                                              timeout=15000):
                    page.locator(
                        "form#choose-plan-form "
                        "button[type='submit']").click()
                landed = page.url
                page.screenshot(path=str(SHOTS / "03_after_choose.png"))
                check("T4+T7.3 Choose-plan submits (redirects off /choose-plan)",
                      "/choose-plan" not in landed,
                      detail=f"→ {landed}")

                # DB verify: coupon stashed + frequency set.
                from app import create_app, db
                from app.models import Company
                app = create_app()
                with app.app_context():
                    co2 = db.session.get(Company, c_id)
                    stashed_coupon = co2.applied_coupon_id
                    stashed_freq = co2.subscription_frequency
                    intended = co2.intended_plan_id
                check("T4.3 applied_coupon_id persisted",
                      stashed_coupon == co_id,
                      detail=f"got {stashed_coupon}, want {co_id}")
                check("T7.4 subscription_frequency = YEARLY",
                      stashed_freq == "YEARLY",
                      detail=f"got {stashed_freq}")
                check("T7.5 intended_plan_id persisted",
                      intended == plan_id,
                      detail=f"got {intended}")

                # DB verify: first invoice created in Manasty.
                from app.models import Invoice, InvoiceStatus
                from app.services.manasty import manasty_id
                app = create_app()
                with app.app_context():
                    mid = manasty_id()
                    inv = Invoice.query.filter_by(
                        company_id=mid, customer_id=co2.saas_customer_id
                    ).first() if co2.saas_customer_id else None
                check("T7.6 First SaaS invoice auto-created in Manasty",
                      inv is not None,
                      detail=(f"invoice #{inv.number} = {inv.total}"
                              if inv else "no invoice"))

            # ─── /calendar/ surface ──────────────────────────
            page.goto(f"{BASE}/calendar/", wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "04_calendar_index.png"))
            add_btn = page.locator("button:has-text('إضافة حدث')")
            check("T5.1 '+ إضافة حدث' button visible",
                  add_btn.count() > 0)
            if add_btn.count() > 0:
                add_btn.first.click()
                page.wait_for_timeout(300)
                page.screenshot(path=str(SHOTS / "05_calendar_modal_open.png"))
                # Modal fields.
                for field, label in [
                    ("input[name='title']", "T5.2 title input"),
                    ("input[name='starts_at']", "T5.3 starts_at input"),
                    ("input[name='ends_at']", "T5.4 ends_at input"),
                    ("input[name='location']", "T5.5 location input"),
                    ("input[name='reminder_minutes_before']",
                     "T5.6 reminder input"),
                    ("textarea[name='description']",
                     "T5.7 description textarea"),
                ]:
                    check(f"{label} present in modal",
                          page.locator(field).count() > 0)
                # Fill + submit.
                start = datetime.utcnow() + timedelta(days=2, hours=10)
                page.fill("input[name='title']", "PW-Event-42")
                page.fill("input[name='starts_at']",
                          start.strftime("%Y-%m-%dT%H:%M"))
                page.fill("input[name='location']", "PW-Room")
                with page.expect_navigation(wait_until="networkidle",
                                              timeout=15000):
                    page.locator(
                        "#add-event-modal button[type='submit']").click()
                page.screenshot(path=str(SHOTS / "06_calendar_after_add.png"))
                # Verify DB.
                from app import create_app
                from app.models import CalendarEvent
                app = create_app()
                with app.app_context():
                    ce = CalendarEvent.query.filter_by(
                        company_id=c_id, title="PW-Event-42").first()
                check("T5.8 CalendarEvent row persisted",
                      ce is not None,
                      detail=(f"id={ce.id}" if ce else "no row"))
                # Verify it renders on timeline.
                body = page.content()
                check("T5.9 New event appears on timeline",
                      "PW-Event-42" in body)
                check("T5.10 Manual event uses 📌 icon",
                      "📌" in body)

            # ─── SUPER-ADMIN SESSION → /admin/saas ───────────
            page.goto(f"{BASE}/logout", wait_until="networkidle")
            page.goto(f"{BASE}/login", wait_until="networkidle")
            page.fill("input[name='email']", SUPER_EMAIL)
            page.fill("input[name='password']", "SuperPass1!")
            with page.expect_navigation(wait_until="networkidle",
                                          timeout=15000):
                page.click("button[type='submit']")
            page.goto(f"{BASE}/admin/saas", wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "07_admin_saas.png"))
            body = page.content()
            check("T7.7 /admin/saas page renders for super-admin",
                  page.url.endswith("/admin/saas"),
                  detail=f"→ {page.url}")
            check("T7.8 Company name visible in the table",
                  "PW E2E B5 Company" in body)
            check("T7.9 'تم الدفع' button visible on outstanding invoice",
                  "تم الدفع" in body)
            check("T7.10 Company row shows YEARLY frequency",
                  "سنوي" in body)

            b.close()
    except Exception as e:
        failed += 1
        results.append(f"FAIL  runtime error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        _teardown()

    print("\n".join(results))
    print(f"\n────  {passed} passed, {failed} failed  ────")
    print(f"Screenshots saved to {SHOTS}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
