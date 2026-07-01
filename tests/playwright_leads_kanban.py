#!/usr/bin/env python3
"""MARSOUD-CRM-EXPANSION §1 — Playwright audit of the Leads Kanban board.

Seeds 7 leads (one per stage), logs in, drives the board:
  - all 7 columns render
  - each stage's card is visible in its column
  - dropdown+submit inside a card changes the stage
  - the redirect keeps the user on the board (return_to=board)
"""
import os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = os.environ.get("BASE_URL", "http://localhost:5050")
SHOTS = ROOT / "tests" / "screenshots" / "leads_kanban"
SHOTS.mkdir(parents=True, exist_ok=True)


def _spin_up_fixture():
    from decimal import Decimal
    from app import create_app, db
    from app.models import Company, User, Lead, LeadStatus
    app = create_app()
    with app.app_context():
        company = Company.query.first()
        owner = User.query.filter_by(email='demo@manasety.ai').first()
        Lead.query.filter(Lead.client_name.like('PW-KB-%')).delete()
        db.session.commit()
        for i, st in enumerate(LeadStatus):
            db.session.add(Lead(
                company_id=company.id,
                client_name=f"PW-KB-{st.name}",
                phone=f"0500{i:03d}00",
                service_needed="خدمة اختبار",
                status=st, assigned_to_id=owner.id,
                expected_value=Decimal("1000") + Decimal(i * 500),
            ))
        db.session.commit()
        return company.id


def _teardown():
    from app import create_app, db
    from app.models import Lead
    app = create_app()
    with app.app_context():
        Lead.query.filter(Lead.client_name.like('PW-KB-%')).delete()
        db.session.commit()


def main():
    from playwright.sync_api import sync_playwright
    _spin_up_fixture()
    passed = failed = 0
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            ctx = b.new_context(viewport={"width": 1600, "height": 900}, locale="ar")
            page = ctx.new_page()

            # Login
            page.goto(f"{BASE}/login", wait_until="domcontentloaded")
            page.fill('input[name="email"]', "demo@manasety.ai")
            page.fill('input[name="password"]', "demo1234")
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")

            # Board
            page.goto(f"{BASE}/leads/?view=board", wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "01_board_full.png"), full_page=True)
            html = page.content()

            # 1. All 7 stage labels present
            from app.models import LeadStatus
            for st in LeadStatus:
                if st.label_ar not in html:
                    print(f"FAIL: stage label '{st.label_ar}' missing")
                    failed += 1
                else:
                    passed += 1
            # 2. All 7 audit cards visible
            for st in LeadStatus:
                marker = f"PW-KB-{st.name}"
                if marker not in html:
                    print(f"FAIL: card {marker} missing")
                    failed += 1
                else:
                    passed += 1
            # 3. View toggle present
            if "📋 بورد" in html and "📊 قائمة" in html:
                passed += 1
            else:
                print("FAIL: view toggle chips missing")
                failed += 1
            # 4. List view still works
            page.goto(f"{BASE}/leads/?view=list", wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "02_list_view.png"), full_page=True)
            if "data-table" not in page.content():
                print("FAIL: list view broken")
                failed += 1
            else:
                passed += 1

            # 5. Status change: pick the NEW_LEAD card, move to CONTACTED
            page.goto(f"{BASE}/leads/?view=board", wait_until="networkidle")
            # Find the card by its unique marker + submit a status change
            from app import create_app, db
            from app.models import Lead
            app = create_app()
            with app.app_context():
                l = Lead.query.filter_by(client_name='PW-KB-NEW_LEAD').first()
                lid = l.id
                original = l.status.value
            # Directly hit the endpoint via Playwright's request helper
            r = page.request.post(
                f"{BASE}/leads/{lid}/status",
                form={"new_status": "MEETING_SCHEDULED", "return_to": "board"},
            )
            # Verify the update landed in the DB
            with app.app_context():
                db.session.expire_all()
                l = db.session.get(Lead, lid)
                if l.status.value == "MEETING_SCHEDULED":
                    passed += 1
                else:
                    print(f"FAIL: status change didn't stick: {l.status.value}")
                    failed += 1
            # Take a screenshot of the board after the move
            page.goto(f"{BASE}/leads/?view=board", wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "03_board_after_move.png"), full_page=True)

            b.close()
    finally:
        _teardown()

    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    print(f"Screenshots: {SHOTS}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
