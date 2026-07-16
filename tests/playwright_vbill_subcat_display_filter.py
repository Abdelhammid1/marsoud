#!/usr/bin/env python3
"""MARSOUD-VBILL-SUBCAT-DISPLAY-FILTER end-to-end verify.

Seeds two vendors + two sub-cats + 3 bills (2 Claude, 1 Google) and
drives the /vendor-bills/ filter panel in Chromium against a live
Flask server at http://127.0.0.1:5050.
"""
import os, sys
from pathlib import Path
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5050")
SHOTS = ROOT / "tests" / "screenshots" / "vbill_subcat_display_filter"
SHOTS.mkdir(parents=True, exist_ok=True)


def _spin_up():
    from app import create_app, db
    from app.models import (
        Company, User, Vendor, VendorBill, VendorBillItem,
        VendorBillStatus, VendorBillPaymentMethod, BillLineType,
        VendorSubCategory, Account,
    )
    from app.services.numbering import next_number
    app = create_app()
    with app.app_context():
        company = Company.query.first()
        owner = User.query.filter_by(email='demo@manasety.ai').first()
        VendorBillItem.query.filter(
            VendorBillItem.description.like('PW-VSD-%')).delete()
        VendorBill.query.filter(
            VendorBill.number.like('PW-VSD-%')).delete()
        VendorSubCategory.query.filter(
            VendorSubCategory.name.like('PW-VSD-%')).delete()
        Vendor.query.filter(Vendor.name.like('PW-VSD-%')).delete()
        db.session.commit()

        v_claude = Vendor(company_id=company.id, name='PW-VSD-Claude',
                           is_active=True)
        v_google = Vendor(company_id=company.id, name='PW-VSD-Google',
                           is_active=True)
        db.session.add_all([v_claude, v_google]); db.session.flush()
        sc_ab = VendorSubCategory(
            company_id=company.id, vendor_id=v_claude.id,
            name='PW-VSD-Abdelhamid', is_active=True,
            created_by_id=owner.id)
        sc_ws = VendorSubCategory(
            company_id=company.id, vendor_id=v_google.id,
            name='PW-VSD-Workspace', is_active=True,
            created_by_id=owner.id)
        db.session.add_all([sc_ab, sc_ws]); db.session.flush()

        exp = Account.query.filter_by(
            company_id=company.id, code='5210').first()

        def _bill(v, price, sc_id, tag):
            b = VendorBill(
                company_id=company.id,
                number=next_number(company.id, 'VENDOR_BILL'),
                vendor_id=v.id,
                issue_date=date.today(),
                due_date=date.today() + timedelta(days=30),
                payment_method=VendorBillPaymentMethod.CASH,
                currency='SAR', tax_rate=0,
                status=VendorBillStatus.POSTED,
            )
            db.session.add(b); db.session.flush()
            db.session.add(VendorBillItem(
                bill_id=b.id, description=tag,
                line_type=BillLineType.EXPENSE, account_id=exp.id,
                quantity=1, unit_price=price, line_total=price,
                sub_category_id=sc_id,
            ))
            b.recalc()

        _bill(v_claude, 100, sc_ab.id, 'PW-VSD-line-claude-tagged')
        _bill(v_claude, 200, None, 'PW-VSD-line-claude-untagged')
        _bill(v_google, 500, sc_ws.id, 'PW-VSD-line-google-tagged')
        db.session.commit()
        return {
            'claude_id': v_claude.id, 'google_id': v_google.id,
            'sc_ab_id': sc_ab.id, 'sc_ws_id': sc_ws.id,
        }


def _cleanup():
    from app import create_app, db
    from app.models import (
        Vendor, VendorBill, VendorBillItem, VendorSubCategory,
    )
    from sqlalchemy import text
    app = create_app()
    with app.app_context():
        VendorBillItem.query.filter(
            VendorBillItem.description.like('PW-VSD-%')).delete()
        VendorBill.query.filter(
            VendorBill.number.like('PW-VSD-%')).delete()
        VendorSubCategory.query.filter(
            VendorSubCategory.name.like('PW-VSD-%')).delete()
        Vendor.query.filter(Vendor.name.like('PW-VSD-%')).delete()
        db.session.commit()
        with db.engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM vendor_bill_items WHERE bill_id NOT IN "
                "(SELECT id FROM vendor_bills)"))


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

            page.goto(f"{BASE}/vendor-bills/", wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "01_unfiltered.png"),
                            full_page=True)
            body = page.content()
            _record(
                'name="vendor"' in body and 'name="sub_category"' in body,
                "1. filter panel has vendor + sub_category selects",
                "one or both selects missing",
            )
            _record(
                "PW-VSD-line-claude-tagged" in body
                and "PW-VSD-line-google-tagged" in body,
                "2. all 3 seeded bills visible unfiltered",
                "seeded bills not all visible",
            )
            _record(
                "🏷" in body and "PW-VSD-Abdelhamid" in body,
                "3. sub-category pill visible in list",
                "pill emoji or name missing",
            )

            # Filter by Claude.
            page.select_option(
                'select[name="vendor"]', str(seed['claude_id']))
            page.locator(
                'button[type="submit"]:has-text("تطبيق")'
            ).first.click()
            page.wait_for_load_state("networkidle")
            body = page.content()
            _record(
                "PW-VSD-line-claude-tagged" in body
                and "PW-VSD-line-google-tagged" not in body,
                "4. vendor filter narrows to Claude only",
                "vendor filter didn't narrow",
            )

            # Add sub_category = Abdelhamid on top of Claude.
            page.select_option(
                'select[name="sub_category"]', str(seed['sc_ab_id']))
            page.locator(
                'button[type="submit"]:has-text("تطبيق")'
            ).first.click()
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(SHOTS / "02_filtered.png"),
                            full_page=True)
            body = page.content()
            _record(
                "PW-VSD-line-claude-tagged" in body
                and "PW-VSD-line-claude-untagged" not in body,
                "5. sub-cat filter narrows to tagged Claude bill only",
                "sub-cat filter didn't narrow",
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
