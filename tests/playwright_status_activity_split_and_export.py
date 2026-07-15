#!/usr/bin/env python3
"""End-to-end verification for the 2 tickets from 2026-07-15:

  Ticket A: separate Lead Status from Activities (+outcome, +suggest)
  Ticket B: grouped Excel export by campaign

Drives both in a real browser against http://127.0.0.1:5050.
"""
import os, sys
from pathlib import Path
from datetime import date, datetime, timedelta
from io import BytesIO

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5050")
SHOTS = ROOT / "tests" / "screenshots" / "status_activity_split"
SHOTS.mkdir(parents=True, exist_ok=True)


def _spin_up():
    from app import create_app, db
    from app.models import (
        Company, User, Lead, LeadStatus, LeadActivity, Campaign,
    )
    app = create_app()
    with app.app_context():
        company = Company.query.first()
        owner = User.query.filter_by(email='demo@manasety.ai').first()
        # Wipe prior fixtures.
        LeadActivity.query.filter(
            LeadActivity.subject.like('PW-SAS-%')).delete()
        Lead.query.filter(Lead.client_name.like('PW-SAS-%')).delete()
        Campaign.query.filter(Campaign.name.like('PW-SAS-%')).delete()
        db.session.commit()

        camp = Campaign(company_id=company.id, name='PW-SAS-Camp',
                        active=True, created_by_id=owner.id)
        db.session.add(camp); db.session.flush()

        lead = Lead(
            company_id=company.id, client_name='PW-SAS-Lead',
            phone='0500000000', service_needed='pw-test',
            assigned_to_id=owner.id, created_by_id=owner.id,
            status=LeadStatus.NEW_LEAD, campaign_id=camp.id,
        )
        # A second lead with NO campaign for the export test.
        lead2 = Lead(
            company_id=company.id, client_name='PW-SAS-NoCamp',
            phone='0500000001', service_needed='pw-test',
            assigned_to_id=owner.id, created_by_id=owner.id,
            status=LeadStatus.CONTACTED, campaign_id=None,
        )
        db.session.add_all([lead, lead2]); db.session.commit()
        return {'company_id': company.id, 'owner_id': owner.id,
                 'lead_id': lead.id, 'campaign': camp.name}


def _cleanup():
    from app import create_app, db
    from app.models import Lead, LeadActivity, Campaign
    app = create_app()
    with app.app_context():
        LeadActivity.query.filter(
            LeadActivity.subject.like('PW-SAS-%')).delete()
        Lead.query.filter(Lead.client_name.like('PW-SAS-%')).delete()
        Campaign.query.filter(Campaign.name.like('PW-SAS-%')).delete()
        db.session.commit()


def main():
    from playwright.sync_api import sync_playwright
    seed = _spin_up()
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

            page.goto(f"{BASE}/login", wait_until="domcontentloaded")
            page.fill('input[name="email"]', "demo@manasety.ai")
            page.fill('input[name="password"]', "demo1234")
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")

            # ── Ticket A / Check 1 — activity form has outcome dropdown
            page.goto(f"{BASE}/leads/{seed['lead_id']}",
                       wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "01_lead_detail.png"),
                            full_page=True)
            html = page.content()
            _record(
                'name="outcome"' in html,
                "1. activity form has an Outcome dropdown",
                "outcome select missing",
            )

            # ── Check 2 — Outcome list has WhatsApp etc. options
            _record(
                'value="WHATSAPP"' in html
                and 'value="VISIT"' in html
                and 'value="FILE_SENT"' in html
                and 'value="QUOTE_SENT"' in html
                and 'value="CONTRACT_SIGNED"' in html,
                "2. all 5 new activity types present in the type select",
                "new activity types missing from type select",
            )

            # ── Check 3 — Submit MEETING + outcome "تم الاجتماع", verify
            # suggestion appears + status hasn't changed yet
            page.select_option('select[name="type"]', "MEETING")
            page.wait_for_timeout(200)
            page.select_option('select[name="outcome"]', "تم الاجتماع")
            page.fill('input[name="subject"]', "PW-SAS-Meeting")
            page.locator(
                'button[type="submit"]:has-text("سجّل نشاط")'
            ).first.click()
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(SHOTS / "02_after_meeting.png"),
                            full_page=True)
            body = page.content()
            # Suggestion panel should be visible.
            _record(
                "هل ترغب في تحديث حالة العميل" in body
                and "اجتماع مجدول" in body,
                "3. status-change suggestion prompt appears after activity",
                "no suggestion prompt visible",
            )

            # Verify status did NOT change yet (still NEW_LEAD).
            from app import create_app, db
            from app.models import Lead, LeadStatus
            app = create_app()
            with app.app_context():
                l = db.session.get(Lead, seed['lead_id'])
                status_before_apply = l.status
            _record(
                status_before_apply == LeadStatus.NEW_LEAD,
                "4. status did NOT auto-change (still NEW_LEAD)",
                f"unexpected status: {status_before_apply}",
            )

            # ── Check 5 — Click "نعم" to apply the suggestion
            page.locator(
                'button:has-text("نعم، حدّث الحالة")'
            ).first.click()
            page.wait_for_load_state("networkidle")
            with app.app_context():
                l = db.session.get(Lead, seed['lead_id'])
                status_after_apply = l.status
            _record(
                status_after_apply == LeadStatus.MEETING_SCHEDULED,
                "5. clicking 'نعم' flips status to MEETING_SCHEDULED",
                f"status: {status_after_apply}",
            )

            # ── Check 6 — outcome pill visible in the timeline
            page.goto(f"{BASE}/leads/{seed['lead_id']}",
                       wait_until="networkidle")
            body = page.content()
            _record(
                "🎯" in body and "تم الاجتماع" in body,
                "6. outcome pill 🎯 visible in the activity timeline",
                "outcome pill missing",
            )

            # ── Ticket B — Grouped export by campaign
            page.goto(f"{BASE}/leads/", wait_until="networkidle")
            html = page.content()
            _record(
                "تصدير حسب الحملة" in html
                and "group_by=campaign" in html,
                "7. leads page has the 'تصدير حسب الحملة' button",
                "grouped export button missing",
            )

            # Download the file (via HTTP since Playwright's download
            # requires an event listener). The endpoint should return
            # a valid xlsx.
            r = page.evaluate("""async () => {
                const resp = await fetch('/leads/export/excel?group_by=campaign');
                if (!resp.ok) return {ok:false, status: resp.status};
                const blob = await resp.blob();
                return {ok:true, size: blob.size};
            }""")
            _record(
                r.get("ok") and r.get("size", 0) > 1000,
                "8. HTTP /leads/export/excel?group_by=campaign returns xlsx",
                f"got {r!r}",
            )

            b.close()
    finally:
        _cleanup()
        print()
        print(f"────  {passed} passed, {failed} failed  ────")
        if fails:
            for f in fails:
                print(f"  · {f}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
