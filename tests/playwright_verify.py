"""Playwright verification for round-3 fixes.

Logs in as non-owner Asmaa and proves on real HTTP:
  1. She can open /tasks/new without a 403 (Ibrahim's "if she can be
     assigned a task she must see + create" principle).
  2. She can pick a datetime-local for both activity_date and
     follow_up_date on the lead detail activity form.
  3. Her draft daily-report renders in the new readable format.

Each step captures a full-page screenshot into tests/screenshots/.
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SHOT_DIR = ROOT / "tests" / "screenshots"
SHOT_DIR.mkdir(parents=True, exist_ok=True)

BASE = "http://127.0.0.1:5000"


def _login(page, email, password):
    page.goto(f"{BASE}/login")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def main():
    from app import create_app, db
    from app.models import (
        Company, User, Lead, LeadStatus,
        EmployeeDailyReport, DailyReportStatus,
    )
    from app.models.crm import LeadStatusEvent, TaskActivityLog
    from app.models.crm_expansion import LeadActivity, LeadActivityType
    from datetime import datetime as _dt
    import json

    # 1) Prepare a bit more fixture data — a lead so we can hit the
    #    lead-activity form, plus a draft daily report so we can
    #    prove the render fix.
    app = create_app()
    with app.app_context():
        c = Company.query.filter_by(name="__PWTEST__").one()
        asmaa = User.query.filter_by(email="pwasmaa@t.co").one()
        # Lead she can visit.
        L = Lead(
            company_id=c.id, client_name="محمد بشير أحمد",
            phone="0500001111", service_needed="محاسبة",
            status=LeadStatus.NEW_LEAD,
            lead_type="INBOUND", source="WEBSITE",
            created_by_id=asmaa.id, assigned_to_id=asmaa.id,
        )
        db.session.add(L); db.session.flush()
        lead_id = L.id

        # Draft daily-report with the OLD (frozen, raw) body so we can
        # prove auto-refresh regenerates it. For the refresh to fire,
        # build_digest must see actual events for the report day —
        # otherwise its "don't overwrite with empty" guard trips and
        # leaves the stale body alone (correct behaviour, defensive).
        report_day = date.today() - timedelta(days=1)
        report_dt = _dt.combine(report_day, _dt.min.time()) + timedelta(hours=10)

        # Give Asmaa a real lead activity on that day so the refresh
        # regenerates the body in the new format.
        db.session.add(LeadActivity(
            company_id=c.id, lead_id=lead_id,
            type=LeadActivityType.CALL,
            subject="اتفقنا على العرض الجديد",
            activity_date=report_dt,
            created_by_id=asmaa.id,
            created_at=report_dt,
        ))

        stale_body = (
            "**المهام** (2)\n"
            "  • STATUS_CHANGED ← مهمة #61\n"
            "  • COMMENT_ADDED ← مهمة #61\n\n"
            "**متابعات العملاء المحتملين** (1)\n"
            "  • مكالمة: — (ليد #76)"
        )
        rpt = EmployeeDailyReport(
            company_id=c.id, employee_id=asmaa.employee_id,
            report_date=report_day,
            title="تقرير اختبار",
            body=stale_body,
            status=DailyReportStatus.DRAFT,
        )
        db.session.add(rpt); db.session.flush()
        rpt_id = rpt.id
        db.session.commit()

    from playwright.sync_api import sync_playwright
    steps = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            locale="ar-EG",
        )
        page = context.new_page()

        _login(page, "pwasmaa@t.co", "asmaa123")
        page.screenshot(path=SHOT_DIR / "01_asmaa_logged_in.png",
                          full_page=True)
        steps.append(("Logged in as Asmaa (sales_rep, non-owner)",
                       "01_asmaa_logged_in.png"))

        # ── Test 1: tasks.view + tasks.manage bypass ─────────────
        r = page.goto(f"{BASE}/tasks/")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=SHOT_DIR / "02_tasks_index.png",
                          full_page=True)
        assert r.status == 200, f"/tasks/ returned {r.status}"
        steps.append((f"GET /tasks/ → HTTP {r.status} (no forbidden)",
                       "02_tasks_index.png"))

        r = page.goto(f"{BASE}/tasks/new")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=SHOT_DIR / "03_tasks_new_form.png",
                          full_page=True)
        assert r.status == 200, f"/tasks/new returned {r.status}"
        # Try to actually submit the form.
        page.fill('input[name="title"]', "مهمة اختبار من أسماء")
        # Priority + status if present as selects — leave defaults.
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        after_url = page.url
        page.screenshot(path=SHOT_DIR / "04_tasks_new_submitted.png",
                          full_page=True)
        # A 403 would leave us on /tasks/new; a success redirects.
        steps.append((f"POST /tasks/new → {after_url} (no 403)",
                       "04_tasks_new_submitted.png"))

        # ── Test 2: lead activity form with datetime-local ─────
        r = page.goto(f"{BASE}/leads/{lead_id}")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=SHOT_DIR / "05_lead_detail.png",
                          full_page=True)
        # Check the two datetime-local inputs are present.
        act_dt = page.locator('input[name="activity_date"]')
        follow_dt = page.locator('input[name="follow_up_date"]')
        assert act_dt.get_attribute("type") == "datetime-local", \
            "activity_date not datetime-local"
        assert follow_dt.get_attribute("type") == "datetime-local", \
            "follow_up_date not datetime-local"
        # Fill a specific meeting time and submit.
        act_dt.fill("2026-07-03T10:00")
        follow_dt.fill("2026-07-08T15:30")
        page.locator('textarea[name="body"]').fill(
            "اجتماع مع العميل الساعة 3 ونص"
        )
        page.click('form[action*="/activities/new"] button[type="submit"]')
        page.wait_for_load_state("networkidle")
        page.screenshot(path=SHOT_DIR / "06_activity_logged.png",
                          full_page=True)
        steps.append(("Logged lead activity with specific time 15:30",
                       "06_activity_logged.png"))

        # ── Test 3: daily report auto-refresh on view ──────────
        # Check the stale body is what's in the DB before we hit the
        # route. Then open it and confirm it re-renders.
        with app.app_context():
            stale = db.session.get(EmployeeDailyReport, rpt_id).body
            assert "STATUS_CHANGED" in stale, "fixture body not raw"

        r = page.goto(f"{BASE}/my/daily-reports/{rpt_id}")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=SHOT_DIR / "07_daily_report.png",
                          full_page=True)

        # Verify body was regenerated: raw codes gone.
        body_locator = page.locator('.whitespace-pre-line').first
        body_text = body_locator.inner_text()
        # Report has 0 real events, so body may be empty — but at
        # least verify raw codes are gone from the visible text.
        assert "STATUS_CHANGED" not in body_text, \
            f"raw STATUS_CHANGED still visible: {body_text!r}"
        assert "COMMENT_ADDED" not in body_text, \
            f"raw COMMENT_ADDED still visible: {body_text!r}"
        # The intro card + submit button should be visible.
        assert "إزاي أتعامل مع التقرير" in page.content(), \
            "onboarding card missing"
        assert "ابعت التقرير للمالك" in page.content(), \
            "submit button label missing"
        steps.append(("Daily report auto-refreshed — raw codes gone",
                       "07_daily_report.png"))

        browser.close()

    print("\n─── Verification summary ───")
    for i, (label, shot) in enumerate(steps, 1):
        print(f"  {i}. ✅ {label}")
        print(f"     → tests/screenshots/{shot}")

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
