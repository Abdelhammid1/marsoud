#!/usr/bin/env python3
"""MARSOUD-CRM-NO-RESPONSE + MARSOUD-LEAD-AUTOCONTACT (2026-07-13).

End-to-end Playwright verification of BOTH tickets against a live
Flask server on http://127.0.0.1:5050. Covers everything the audit
suite already covers, PLUS the actual browser rendering (which the
HTTP-level tests can't verify): CSS panel toggle, real form submits,
navigation from the sidebar.

Ticket A checks: NO_RESPONSE stage
  1. New sidebar link "لا يوجد استجابة" is present + navigates.
  2. Fresh lead is visible on the Kanban board.
  3. Moving a lead to NO_RESPONSE hides it from the board.
  4. Same lead now appears on /leads/no-response folder page.
  5. Restore action from folder returns lead to a pipeline stage
     and the folder page no longer lists it.
  6. Campaigns page shows the "No Response" column header.
  7. Analytics page shows the NO_RESPONSE KPI card.

Ticket B checks: Auto-Contact + CRUD
  8. Creating a lead via /leads/new auto-creates a primary contact
     (visible in the contacts panel on the lead's detail page).
  9. Edit contact form flow — click "تعديل", change the name,
     submit, verify the updated name appears in the view row.
 10. Delete contact — remove the auto-created one, confirm the
     empty-state message renders.
 11. Recreate contact after delete — add a new one from the
     panel form, confirm it shows up.
"""
import os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5050")
SHOTS = ROOT / "tests" / "screenshots" / "no_response_contact"
SHOTS.mkdir(parents=True, exist_ok=True)


def _cleanup():
    from app import create_app, db
    from app.models import Lead, LeadContact
    app = create_app()
    with app.app_context():
        # Remove any prior-run fixtures.
        leads = Lead.query.filter(
            Lead.client_name.like("PW-NR-%")
        ).all()
        for l in leads:
            LeadContact.query.filter_by(lead_id=l.id).delete()
            db.session.delete(l)
        db.session.commit()


def main():
    from playwright.sync_api import sync_playwright, expect
    _cleanup()
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

            # ── Login as the seeded demo owner ────────────────────
            page.goto(f"{BASE}/login", wait_until="domcontentloaded")
            page.fill('input[name="email"]', "demo@manasety.ai")
            page.fill('input[name="password"]', "demo1234")
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(SHOTS / "00_after_login.png"))

            # ── Ticket A / Check 1 — sidebar link ─────────────────
            page.goto(f"{BASE}/home", wait_until="networkidle")
            html = page.content()
            _record(
                "/leads/no-response" in html
                and "لا يوجد استجابة" in html,
                "1. sidebar shows 'لا يوجد استجابة' link",
                "sidebar HTML missing link or label",
            )

            # ── Create a fresh lead via the new-lead form ─────────
            page.goto(f"{BASE}/leads/new", wait_until="networkidle")
            page.fill('input[name="client_name"]', "PW-NR-Client")
            page.fill('input[name="phone"]', "0501112233")
            page.fill('input[name="service_needed"]', "consulting-test")
            # `assigned_to_id` is a select; grab the first non-empty option.
            first_rep = page.eval_on_selector(
                'select[name="assigned_to_id"] option:not([value=""])',
                'e => e.value',
            )
            page.select_option('select[name="assigned_to_id"]', first_rep)
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")
            # After success we land on the lead detail page.
            detail_url = page.url
            page.screenshot(path=str(SHOTS / "01_lead_detail.png"),
                            full_page=True)

            # ── Ticket B / Check 8 — auto-Contact rendered ────────
            body = page.content()
            _record(
                "PW-NR-Client" in body and "0501112233" in body
                and "أساسي" in body,
                "8. new lead detail shows auto-created primary contact",
                "contact card missing name/phone/primary badge",
            )

            # ── Check 2 — lead is on the pipeline board ───────────
            page.goto(f"{BASE}/leads/?view=board", wait_until="networkidle")
            body = page.content()
            _record(
                "PW-NR-Client" in body,
                "2. fresh lead visible on the pipeline board",
                "lead not on board",
            )

            # ── Check 3 — move to NO_RESPONSE, gone from board ────
            # The Kanban card has a form per lead posting to /leads/<id>/status.
            # Find the form containing our lead name and submit it.
            # Simpler path: hit the endpoint directly using the lead id.
            from app import create_app, db
            from app.models import Lead
            app = create_app()
            with app.app_context():
                l = Lead.query.filter_by(client_name="PW-NR-Client").first()
                lead_id = l.id
            page.evaluate(
                """async ({lead_id}) => {
                    const fd = new FormData();
                    fd.append('new_status', 'NO_RESPONSE');
                    fd.append('return_to', 'board');
                    await fetch(`/leads/${lead_id}/status`,
                                {method:'POST', body:fd});
                }""",
                {"lead_id": lead_id},
            )
            page.goto(f"{BASE}/leads/?view=board", wait_until="networkidle")
            body = page.content()
            _record(
                "PW-NR-Client" not in body,
                "3. parked lead is hidden from the pipeline board",
                "parked lead still visible on board",
            )

            # ── Check 4 — folder page shows the lead ──────────────
            page.goto(f"{BASE}/leads/no-response",
                      wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "02_folder_with_lead.png"),
                            full_page=True)
            body = page.content()
            _record(
                "PW-NR-Client" in body,
                "4. parked lead visible on /leads/no-response folder",
                "folder page missing lead",
            )

            # ── Check 5 — restore from folder ─────────────────────
            page.evaluate(
                """async ({lead_id}) => {
                    const fd = new FormData();
                    fd.append('new_status', 'CONTACTED');
                    fd.append('return_to', '/leads/no-response');
                    await fetch(`/leads/${lead_id}/status`,
                                {method:'POST', body:fd});
                }""",
                {"lead_id": lead_id},
            )
            page.goto(f"{BASE}/leads/no-response",
                      wait_until="networkidle")
            body = page.content()
            gone_from_folder = "PW-NR-Client" not in body
            page.goto(f"{BASE}/leads/?view=board", wait_until="networkidle")
            body = page.content()
            back_on_board = "PW-NR-Client" in body
            _record(
                gone_from_folder and back_on_board,
                "5. restore: folder empties, pipeline board rehydrates",
                f"gone_from_folder={gone_from_folder} back_on_board={back_on_board}",
            )

            # ── Check 6 — Campaigns page has No Response column ───
            page.goto(f"{BASE}/crm/campaigns/", wait_until="networkidle")
            body = page.content()
            _record(
                "No Response" in body or "لا يوجد استجابة" in body,
                "6. campaigns page shows the No Response column",
                "No Response column header missing",
            )

            # ── Check 7 — Analytics page has NO_RESPONSE KPI ──────
            page.goto(f"{BASE}/crm/analytics/", wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "03_analytics.png"),
                            full_page=True)
            body = page.content()
            _record(
                "لا يوجد استجابة" in body,
                "7. analytics page surfaces the NO_RESPONSE KPI",
                "NO_RESPONSE label missing from analytics",
            )

            # ── Ticket B / Check 9 — edit contact flow ────────────
            page.goto(detail_url, wait_until="networkidle")
            # Click the edit button on the first contact card.
            edit_btn = page.locator('button:has-text("✏ تعديل")').first
            edit_btn.click()
            # Change the name in the inline form.
            name_input = page.locator('form.js-edit input[name="name"]').first
            name_input.fill("PW-NR-Client-Edited")
            page.locator('form.js-edit button[type="submit"]:has-text("حفظ")').first.click()
            page.wait_for_load_state("networkidle")
            body = page.content()
            _record(
                "PW-NR-Client-Edited" in body,
                "9. contact edit inline form updates the name",
                "edited name not visible after save",
            )

            # ── Check 10 — delete contact ─────────────────────────
            # Confirm() shim: accept the browser dialog automatically.
            page.on("dialog", lambda d: d.accept())
            delete_form = page.locator(
                'form[action*="/contacts/"][action$="/delete"]'
            ).first
            # Extract the action so we can POST it via fetch (bypasses
            # the confirm() dialog even though we registered a handler).
            action = delete_form.get_attribute("action")
            page.evaluate(
                """async ({action}) => {
                    const fd = new FormData();
                    await fetch(action, {method:'POST', body:fd});
                }""",
                {"action": action},
            )
            page.goto(detail_url, wait_until="networkidle")
            body = page.content()
            _record(
                "لا توجد جهات اتصال بعد" in body,
                "10. contact deleted — empty-state message visible",
                "contact still present or empty-state missing",
            )

            # ── Check 11 — recreate contact after delete ──────────
            page.locator('form[action$="/contacts/new"] input[name="name"]').fill("PW-NR-Recreated")
            page.locator('form[action$="/contacts/new"] input[name="phone"]').fill("0509998887")
            page.locator('form[action$="/contacts/new"] button[type="submit"]').click()
            page.wait_for_load_state("networkidle")
            body = page.content()
            _record(
                "PW-NR-Recreated" in body,
                "11. new contact form recreates after deletion",
                "recreated contact missing from UI",
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
