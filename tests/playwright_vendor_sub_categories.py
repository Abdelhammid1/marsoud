#!/usr/bin/env python3
"""MARSOUD-VENDOR-SUBCAT (Abdelhamid 2026-07-14) end-to-end verify.

Real-browser check that:
  1. Sub-category management page renders + can add a category.
  2. Vendor bill form shows a Sub-Category dropdown that populates
     from the JSON API when a vendor is selected.
  3. Submitting the form saves sub_category_id on the bill line.
  4. Report page renders the (vendor, sub-category) totals.

Server assumed running on http://127.0.0.1:5050.
"""
import os, sys
from pathlib import Path
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5050")
SHOTS = ROOT / "tests" / "screenshots" / "vendor_sub_categories"
SHOTS.mkdir(parents=True, exist_ok=True)


def _spin_up():
    from app import create_app, db
    from app.models import (
        Company, User, Vendor, VendorSubCategory, VendorBill,
        VendorBillItem,
    )
    app = create_app()
    with app.app_context():
        company = Company.query.first()
        owner = User.query.filter_by(email='demo@manasety.ai').first()
        Vendor.query.filter(Vendor.name.like('PW-VSC-%')).delete()
        VendorSubCategory.query.filter(
            VendorSubCategory.name.like('PW-VSC-%')).delete()
        VendorBillItem.query.filter(
            VendorBillItem.description.like('PW-VSC-%')).delete()
        VendorBill.query.filter(
            VendorBill.number.like('PW-VSC-%')).delete()
        db.session.commit()
        v = Vendor(
            company_id=company.id, name='PW-VSC-Claude',
            email='pw@claude.test', is_active=True,
        )
        db.session.add(v); db.session.commit()
        return {'company_id': company.id, 'owner_id': owner.id,
                 'vendor_id': v.id}


def _cleanup():
    from app import create_app, db
    from app.models import (
        Vendor, VendorSubCategory, VendorBill, VendorBillItem,
    )
    from sqlalchemy import text
    app = create_app()
    with app.app_context():
        VendorBillItem.query.filter(
            VendorBillItem.description.like('PW-VSC-%')).delete()
        VendorBill.query.filter(
            VendorBill.number.like('PW-VSC-%')).delete()
        VendorSubCategory.query.filter(
            VendorSubCategory.name.like('PW-VSC-%')).delete()
        Vendor.query.filter(Vendor.name.like('PW-VSC-%')).delete()
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
            page.on("dialog", lambda d: d.accept())

            # Login.
            page.goto(f"{BASE}/login", wait_until="domcontentloaded")
            page.fill('input[name="email"]', "demo@manasety.ai")
            page.fill('input[name="password"]', "demo1234")
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")

            # ── Check 1: management page + add category ────────────
            page.goto(
                f"{BASE}/vendors/{seed['vendor_id']}/sub-categories",
                wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "01_management.png"),
                            full_page=True)
            page.fill('input[name="name"]', "PW-VSC-Abdelhamid")
            page.locator(
                'button[type="submit"]:has-text("إضافة")'
            ).first.click()
            page.wait_for_load_state("networkidle")
            body = page.content()
            _record(
                "PW-VSC-Abdelhamid" in body,
                "1. new sub-category appears in the management list",
                "new category not visible after add",
            )

            # Add a second one so the dropdown has multiple options.
            page.fill('input[name="name"]', "PW-VSC-Rofida")
            page.locator(
                'button[type="submit"]:has-text("إضافة")'
            ).first.click()
            page.wait_for_load_state("networkidle")

            # ── Check 2: bill form shows sub-cat dropdown ──────────
            page.goto(f"{BASE}/vendor-bills/new",
                       wait_until="networkidle")
            page.select_option('select[name="vendor_id"]',
                                str(seed['vendor_id']))
            # Wait for the JSON fetch to settle.
            page.wait_for_timeout(500)
            page.screenshot(path=str(SHOTS / "02_bill_form.png"),
                            full_page=True)
            html = page.content()
            _record(
                "PW-VSC-Abdelhamid" in html and "PW-VSC-Rofida" in html,
                "2. bill form loads both sub-categories in dropdown",
                "sub-categories missing from form html",
            )

            # ── Check 3: submitting the bill saves sub_category_id ─
            # Fill a line with the description + pick Abdelhamid.
            page.fill(
                'input[name="item_description[]"]',
                "PW-VSC-Line1")
            page.fill('input[name="item_quantity[]"]', "1")
            page.fill('input[name="item_unit_price[]"]', "1234")
            # Pick the first available expense account.
            first_acc = page.eval_on_selector(
                'select[name="item_account_id[]"] option:not([value=""])',
                'e => e.value')
            page.select_option(
                'select[name="item_account_id[]"]', first_acc)
            # Pick Abdelhamid from the sub-cat picker.
            page.select_option(
                'select[name="item_sub_category_id[]"]',
                label="PW-VSC-Abdelhamid")
            # Save (the button label is "حفظ + تسجيل").
            page.locator('button[type="submit"]').first.click()
            page.wait_for_load_state("networkidle")

            from app import create_app, db
            from app.models import VendorBill, VendorBillItem
            app = create_app()
            with app.app_context():
                bill = VendorBill.query.filter_by(
                    vendor_id=seed['vendor_id']
                ).order_by(VendorBill.id.desc()).first()
                assert bill is not None, "no bill created"
                line = VendorBillItem.query.filter_by(
                    bill_id=bill.id,
                    description="PW-VSC-Line1",
                ).first()
                if line is None:
                    _record(False, "3. bill line saved with sub-cat",
                            "line row not found in DB")
                else:
                    from app.models import VendorSubCategory
                    sc = db.session.get(
                        VendorSubCategory, line.sub_category_id)
                    _record(
                        sc is not None and sc.name == "PW-VSC-Abdelhamid",
                        "3. bill line saved with the correct sub-category",
                        f"got sub_category_id={line.sub_category_id}",
                    )

            # ── Check 4: report page shows the total ───────────────
            page.goto(f"{BASE}/reports/vendor-sub-categories",
                       wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "03_report.png"),
                            full_page=True)
            body = page.content()
            _record(
                "PW-VSC-Claude" in body
                and "PW-VSC-Abdelhamid" in body
                and "1,234" in body,
                "4. report shows vendor + sub-category + total",
                "report missing expected vendor/subcat/total tokens",
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
