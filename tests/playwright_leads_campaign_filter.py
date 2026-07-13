#!/usr/bin/env python3
"""MARSOUD-LEADS-CAMPAIGN-FILTER (Abdelhamid 2026-07-13).

Real-browser verification that:
  1. The /leads/ filter panel has a Campaign <select>.
  2. Picking a campaign + hitting "تطبيق" narrows the board.
  3. Only the leads on that campaign are visible after the reload.
  4. The Export Excel link carries the campaign filter to the export URL.

Server assumed running on http://127.0.0.1:5050.
"""
import os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5050")
SHOTS = ROOT / "tests" / "screenshots" / "leads_campaign_filter"
SHOTS.mkdir(parents=True, exist_ok=True)


def _spin_up_fixture():
    from app import create_app, db
    from app.models import Company, User, Lead, LeadStatus, Campaign
    app = create_app()
    with app.app_context():
        company = Company.query.first()
        owner = User.query.filter_by(email='demo@manasety.ai').first()
        # Clean prior-run leftovers.
        Lead.query.filter(Lead.client_name.like('PW-CAMP-%')).delete()
        Campaign.query.filter(Campaign.name.like('PW-CAMP-%')).delete()
        db.session.commit()

        camp_a = Campaign(
            company_id=company.id, name='PW-CAMP-Alpha',
            active=True, created_by_id=owner.id)
        camp_b = Campaign(
            company_id=company.id, name='PW-CAMP-Beta',
            active=True, created_by_id=owner.id)
        db.session.add_all([camp_a, camp_b]); db.session.flush()

        def _lead(name, camp_id):
            db.session.add(Lead(
                company_id=company.id, client_name=name,
                phone='0500000000', service_needed='pw-test',
                assigned_to_id=owner.id, created_by_id=owner.id,
                status=LeadStatus.NEW_LEAD, campaign_id=camp_id,
            ))
        _lead('PW-CAMP-Client-Alpha1', camp_a.id)
        _lead('PW-CAMP-Client-Alpha2', camp_a.id)
        _lead('PW-CAMP-Client-Beta1', camp_b.id)
        db.session.commit()
        return company.id, camp_a.id, camp_b.id


def _cleanup():
    from app import create_app, db
    from app.models import Lead, Campaign
    app = create_app()
    with app.app_context():
        Lead.query.filter(Lead.client_name.like('PW-CAMP-%')).delete()
        Campaign.query.filter(Campaign.name.like('PW-CAMP-%')).delete()
        db.session.commit()


def main():
    from playwright.sync_api import sync_playwright
    cid, camp_a, camp_b = _spin_up_fixture()
    passed = failed = 0
    fails = []

    def _record(ok, label, details=""):
        nonlocal passed, failed
        if ok:
            print(f"PASS  {label}")
            passed += 1
        else:
            print(f"FAIL  {label}  ⇒ {details}")
            failed += 1
            fails.append(f"{label}: {details}")

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            ctx = b.new_context(
                viewport={"width": 1600, "height": 1000}, locale="ar",
            )
            page = ctx.new_page()

            # Login as the demo owner.
            page.goto(f"{BASE}/login", wait_until="domcontentloaded")
            page.fill('input[name="email"]', "demo@manasety.ai")
            page.fill('input[name="password"]', "demo1234")
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")

            # Open the leads board.
            page.goto(f"{BASE}/leads/", wait_until="networkidle")

            # Check 1: campaign select in the filter panel.
            filter_open = page.locator("#filter-panel").first
            filter_open.click()  # <details> → open
            camp_select = page.locator('select[name="campaign"]').first
            _record(
                camp_select.count() > 0,
                "1. filter panel has select[name='campaign']",
                "select not found",
            )
            html = page.content()
            _record(
                "PW-CAMP-Alpha" in html and "PW-CAMP-Beta" in html,
                "2. dropdown lists both fixture campaigns",
                "campaign labels missing from dropdown",
            )

            # Check 3: pick Alpha, submit, verify board.
            camp_select.select_option(str(camp_a))
            page.locator('button[type="submit"]:has-text("تطبيق")').first.click()
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(SHOTS / "01_filtered.png"),
                            full_page=True)
            html = page.content()
            _record(
                "PW-CAMP-Client-Alpha1" in html
                and "PW-CAMP-Client-Alpha2" in html,
                "3. board shows both Alpha leads after filter",
                "Alpha leads missing from filtered board",
            )
            _record(
                "PW-CAMP-Client-Beta1" not in html,
                "4. board hides Beta leads when campaign=Alpha",
                "Beta lead leaked through the filter",
            )

            # Check 5: Export Excel link carries the campaign query arg.
            export_href = page.get_attribute(
                'a:has-text("تصدير Excel")', 'href')
            _record(
                export_href is not None
                and f"campaign={camp_a}" in export_href,
                "5. Export Excel link carries campaign={id}",
                f"got href={export_href!r}",
            )

            b.close()
    finally:
        _cleanup()
        print()
        print(f"────  {passed} passed, {failed} failed  ────")
        if fails:
            for line in fails:
                print(f"  · {line}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
