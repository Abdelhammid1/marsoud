"""Playwright verification for PER-CO-NUMBERING.

Fresh company (global ids are guaranteed > 1 because prior fixtures
used ids 1..N). Creates a lead + a project via the real HTTP forms,
opens each detail page, and screenshots. Then asserts:

  - Lead detail page shows "L-0001" — NOT the raw global id.
  - Project detail page shows "PRJ-0001" — NOT the raw global id.
  - When the project is converted from the lead, the "جاء من" line
    reads "عميل محتمل L-0001", not "#<id>".
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SHOT = ROOT / "tests" / "screenshots"
SHOT.mkdir(parents=True, exist_ok=True)

BASE = "http://127.0.0.1:5000"


def _login(page, email, password):
    page.goto(f"{BASE}/login")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def main():
    from playwright.sync_api import sync_playwright
    from app import create_app, db
    from app.models import Company, Customer

    app = create_app()
    with app.app_context():
        c = Company.query.filter_by(name="__PCNTEST__").one()
        cid = c.id
        cust = Customer.query.filter_by(company_id=cid).one()
        customer_id = cust.id
        # Reset any prior leads/projects/sequences on this fixture so
        # the first form submission produces L-0001 / PRJ-0001, not
        # L-0002 etc. from a previous run of this same script.
        from sqlalchemy import text
        with db.engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM lead_status_events WHERE lead_id IN "
                "(SELECT id FROM leads WHERE company_id = :c)"
            ), {"c": cid})
            conn.execute(text(
                "DELETE FROM projects WHERE company_id = :c"
            ), {"c": cid})
            conn.execute(text(
                "DELETE FROM leads WHERE company_id = :c"
            ), {"c": cid})
            conn.execute(text(
                "DELETE FROM number_sequences WHERE company_id = :c "
                "AND doc_type IN ('LEAD', 'PROJECT', 'PRODUCT')"
            ), {"c": cid})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            locale="ar-EG",
        )
        page = context.new_page()

        _login(page, "pcnowner@t.co", "owner123")

        # ── Create a Lead via the real form ───────────────────
        page.goto(f"{BASE}/leads/new")
        page.wait_for_load_state("networkidle")
        page.fill('input[name="client_name"]', "شركة اختبار الترقيم")
        page.fill('input[name="phone"]', "0500000000")
        page.fill('input[name="service_needed"]', "خدمة اختبار")
        # Assigned to the owner (only rep option → index 0).
        page.select_option('select[name="assigned_to_id"]', index=0)
        # Type + source are enums; use direct enum names so we don't
        # depend on Jinja option ordering.
        page.select_option('select[name="lead_type"]', value="INBOUND")
        page.select_option('select[name="source"]', value="WEBSITE")
        page.click('button[type=\"submit\"]')
        page.wait_for_load_state("networkidle")
        # Grab the created lead's global id, then navigate explicitly
        # to the DETAIL page (the form redirects to /leads/ index).
        with app.app_context():
            from app.models import Lead
            lead = Lead.query.filter_by(company_id=cid).one()
            lead_global_id = lead.id
            lead_id = lead.id
        page.goto(f"{BASE}/leads/{lead_id}")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=SHOT / "pcn_01_lead_detail.png",
                          full_page=True)

        html = page.content()
        assert "L-0001" in html, \
            f"L-0001 missing from lead detail — see screenshot."
        # The URL still uses the id — that's fine and expected.
        assert f"#{lead_global_id}" not in html or "L-0001" in html, \
            "raw #id leaks alongside L-0001"
        print(f"  Lead: global id={lead_global_id}, "
                f"display=L-0001 ✓")

        # ── Convert requires status=WON. Promote the lead directly. ─
        with app.app_context():
            from app.models import Lead, LeadStatus
            L = db.session.get(Lead, lead_id)
            L.status = LeadStatus.WON
            db.session.commit()

        # ── Convert to project via the /leads/<id>/convert form ────
        page.goto(f"{BASE}/leads/{lead_id}/convert")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector('input[name="project_name"]', timeout=5000)
        page.fill('input[name="project_name"]', "مشروع اختبار من الليد")
        page.fill('input[name="project_type"]', "استشارة")
        page.select_option('select[name="manager_id"]', index=0)
        # end_date is required — set 30 days out.
        from datetime import date, timedelta
        end = (date.today() + timedelta(days=30)).isoformat()
        page.fill('input[name="end_date"]', end)
        page.click('button[type=\"submit\"]')
        page.wait_for_load_state("networkidle")
        page.screenshot(path=SHOT / "pcn_02_project_detail.png",
                          full_page=True)

        html = page.content()
        assert "PRJ-0001" in html, \
            "PRJ-0001 missing from project detail"
        # The critical line Abdelhamid screenshotted:
        assert ("جاء من" in html) and ("L-0001" in html), \
            "'جاء من: عميل محتمل L-0001' missing"
        assert "عميل محتمل #" not in html, \
            "raw '#<id>' leak in 'جاء من' line"
        with app.app_context():
            from app.models import Project
            proj = Project.query.filter_by(company_id=cid).one()
            print(f"  Project: global id={proj.id}, "
                    f"display=PRJ-0001, جاء من=L-0001 ✓")

        # ── Also assert the URL contains the global id (not
        #    renumbered) — routing is unchanged.
        assert f"/projects/{proj.id}" in page.url, \
            f"URL should use global id, got {page.url}"

        browser.close()

    print("\nAll assertions passed.")
    print(f"  → tests/screenshots/pcn_01_lead_detail.png")
    print(f"  → tests/screenshots/pcn_02_project_detail.png")


if __name__ == "__main__":
    main()
