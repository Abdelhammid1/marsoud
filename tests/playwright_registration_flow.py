#!/usr/bin/env python3
"""MARSOUD-REG-FLOW-E2E (Abdelhamid 2026-07-25).

Real-browser walk of the FULL new-user journey:
  1. GET /register — form renders + honeypot invisible + agree_terms
     checkbox visible.
  2. Fill form + submit — server accepts.
  3. Server should redirect to /verify-pending (email verification).
  4. Grab the verify token from the DB (SMTP isn't wired), hit the
     verify link.
  5. Land on /choose-plan (sidebar hidden — that layout stays
     locked until a plan is picked).
  6. Click a plan card + click "تأكيد الاختيار" — must POST the plan
     form, NOT the header logout form. Redirect to dashboard.
  7. Land on /home (dashboard.index) with the sidebar visible.

Runs against a live dev server (default localhost:5050). Set
BASE_URL to override.
"""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = os.environ.get("BASE_URL", "http://localhost:5050")
SHOTS = ROOT / "tests" / "screenshots" / "registration_flow"
SHOTS.mkdir(parents=True, exist_ok=True)


TEST_EMAIL = "pw-reg-e2e@test.local"
TEST_SUBDOMAIN = "pw-reg-e2e"


def _teardown():
    """Wipe any leftover fixture rows so the run is clean."""
    from app import create_app, db
    from sqlalchemy import text, inspect
    app = create_app()
    with app.app_context():
        insp = inspect(db.engine)
        with db.engine.begin() as conn:
            cids = [r[0] for r in conn.execute(text(
                "SELECT id FROM companies WHERE subdomain = :s"),
                {"s": TEST_SUBDOMAIN})]
            for cid in cids:
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
            conn.execute(text(
                "DELETE FROM users WHERE email = :e"),
                {"e": TEST_EMAIL})
        from app.services.bot_guard import register_rate_reset
        register_rate_reset()


def _get_verify_url_for(email):
    """SMTP isn't wired in dev, so grab the verify token straight
    from the service that mints it and build the URL."""
    from app import create_app
    from app.models import User
    from app.services.permissions import generate_verify_email_token
    app = create_app()
    with app.app_context():
        u = User.query.filter_by(email=email).first()
        if not u:
            return None
        token = generate_verify_email_token(u.id)
    return f"{BASE}/verify/{token}"


def main():
    from playwright.sync_api import sync_playwright
    _teardown()

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
            page = ctx.new_page()

            # ─── STEP 1: /register renders ──────────────────
            page.goto(f"{BASE}/register", wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "01_register_form.png"))
            has_form = page.locator(
                "form input[name='email']").count() > 0
            check("1. /register form loads with email input",
                  has_form)
            # Honeypot input exists in the DOM but is NOT visible.
            honeypot = page.locator("input[name='website']")
            hp_count = honeypot.count()
            hp_visible = honeypot.is_visible() if hp_count else False
            check("2. Honeypot field present in DOM",
                  hp_count > 0)
            check("3. Honeypot field NOT visible to humans",
                  hp_count > 0 and not hp_visible,
                  detail=f"visible={hp_visible}")

            # ─── STEP 2: fill + submit ──────────────────────
            page.fill("input[name='full_name']", "PW E2E User")
            page.fill("input[name='email']", TEST_EMAIL)
            page.fill("input[name='company_name']", "PW E2E Company")
            page.fill("input[name='subdomain']", TEST_SUBDOMAIN)
            page.fill("input[name='password']", "Str0ngP@ss1!")
            # tick agree_terms if present.
            terms = page.locator("input[name='agree_terms']")
            if terms.count():
                terms.check()
            # DO NOT touch the honeypot.
            page.screenshot(path=str(SHOTS / "02_filled.png"))

            with page.expect_navigation(wait_until="networkidle",
                                          timeout=15000):
                page.click("button[type='submit']")
            landed = page.url
            page.screenshot(path=str(SHOTS / "03_after_submit.png"))
            check("4. Submit redirects (not stuck on /register)",
                  "/register" not in landed,
                  detail=f"→ {landed}")

            # ─── STEP 3: user + company in DB ───────────────
            from app import create_app, db
            from app.models import User, Company, UserStatus
            app = create_app()
            with app.app_context():
                u = User.query.filter_by(email=TEST_EMAIL).first()
                co = Company.query.filter_by(
                    subdomain=TEST_SUBDOMAIN).first()
            check("5. User row created", u is not None)
            check("6. Company row created", co is not None)
            check("7. User starts as PENDING_VERIFICATION",
                  u is not None and
                  u.status == UserStatus.PENDING_VERIFICATION.value,
                  detail=(u.status if u else "no user"))

            # ─── STEP 4: verify email ───────────────────────
            verify_url = _get_verify_url_for(TEST_EMAIL)
            check("8. Verify URL mintable", verify_url is not None)
            if verify_url:
                page.goto(verify_url, wait_until="networkidle")
                page.screenshot(path=str(SHOTS / "04_after_verify.png"))
                after_verify = page.url
                check("9. Verify lands on /choose-plan",
                      "/choose-plan" in after_verify,
                      detail=f"→ {after_verify}")

                # DB flip: status == ACTIVE
                with app.app_context():
                    u2 = User.query.filter_by(email=TEST_EMAIL).first()
                check("10. User flipped to ACTIVE after verify",
                      u2 and u2.status == UserStatus.ACTIVE.value,
                      detail=(u2.status if u2 else "no user"))

            # ─── STEP 5: sidebar hidden on /choose-plan ─────
            sidebar_present = page.locator("#sidebar").count() > 0
            check("11. Sidebar hidden on /choose-plan",
                  not sidebar_present,
                  detail=f"#sidebar count={sidebar_present}")
            # Confirm button lives inside the plan form (structural
            # regression check).
            plan_form = page.locator("form#choose-plan-form")
            check("12. Plan form has id='choose-plan-form'",
                  plan_form.count() > 0)
            submit_in_form = page.locator(
                "form#choose-plan-form button[type='submit']"
            ).count() > 0
            check("13. Submit button lives INSIDE the plan form",
                  submit_in_form)

            # ─── STEP 6: pick a plan + submit ───────────────
            first_plan_input = page.locator(
                "form#choose-plan-form input[name='plan_id']").first
            if first_plan_input.count():
                first_plan_input.check()
                page.screenshot(path=str(SHOTS / "05_plan_selected.png"))
                with page.expect_navigation(wait_until="networkidle",
                                              timeout=15000):
                    page.locator(
                        "form#choose-plan-form button[type='submit']"
                    ).click()
                after_pick = page.url
                page.screenshot(path=str(SHOTS / "06_after_pick.png"))
                check("14. Plan submit redirects OFF /choose-plan",
                      "/choose-plan" not in after_pick,
                      detail=f"→ {after_pick}")
                check("15. Plan submit does NOT log the user out",
                      "/login" not in after_pick,
                      detail=f"→ {after_pick}")

                # DB: intended_plan_id set.
                with app.app_context():
                    co2 = Company.query.filter_by(
                        subdomain=TEST_SUBDOMAIN).first()
                check("16. intended_plan_id persisted",
                      co2 and co2.intended_plan_id is not None,
                      detail=(f"plan_id={co2.intended_plan_id}"
                              if co2 else "no company"))

                # STEP 7: sidebar back
                sidebar_after = page.locator("#sidebar").count() > 0
                check("17. Sidebar re-appears after plan chosen",
                      sidebar_after,
                      detail=f"#sidebar count={sidebar_after}")
            else:
                check("14+15+16+17. Plan submit chain SKIPPED "
                      "(no plans in DB — run seed-plans first)",
                      False,
                      detail="no plan cards rendered")

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
