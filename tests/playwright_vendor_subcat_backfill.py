#!/usr/bin/env python3
"""MARSOUD-VENDOR-SUBCAT-BACKFILL end-to-end verify.

Seeds a vendor + 2 sub-cats + a POSTED bill with 2 uncategorized
lines, then opens /vendors/<id>/bill-items/categorize in a real
browser and drives the bulk-save workflow.
"""
import os, sys
from pathlib import Path
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5050")
SHOTS = ROOT / "tests" / "screenshots" / "vendor_subcat_backfill"
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
        Vendor.query.filter(Vendor.name.like('PW-BF-%')).delete()
        VendorSubCategory.query.filter(
            VendorSubCategory.name.like('PW-BF-%')).delete()
        VendorBillItem.query.filter(
            VendorBillItem.description.like('PW-BF-%')).delete()
        VendorBill.query.filter(
            VendorBill.number.like('PW-BF-%')).delete()
        db.session.commit()

        v = Vendor(company_id=company.id, name='PW-BF-Claude',
                    is_active=True)
        db.session.add(v); db.session.flush()
        sc_ab = VendorSubCategory(
            company_id=company.id, vendor_id=v.id, name='PW-BF-Abdelhamid',
            is_active=True, created_by_id=owner.id,
        )
        sc_rf = VendorSubCategory(
            company_id=company.id, vendor_id=v.id, name='PW-BF-Rofida',
            is_active=True, created_by_id=owner.id,
        )
        db.session.add_all([sc_ab, sc_rf]); db.session.flush()

        exp_acc = Account.query.filter_by(
            company_id=company.id, code="5210").first()
        b = VendorBill(
            company_id=company.id,
            number=next_number(company.id, "VENDOR_BILL"),
            vendor_id=v.id,
            issue_date=date.today(),
            due_date=date.today() + timedelta(days=30),
            payment_method=VendorBillPaymentMethod.CASH,
            currency="SAR",
            status=VendorBillStatus.POSTED,
        )
        db.session.add(b); db.session.flush()
        it1 = VendorBillItem(
            bill_id=b.id, description="PW-BF-line-1",
            line_type=BillLineType.EXPENSE, account_id=exp_acc.id,
            quantity=1, unit_price=150, line_total=150,
        )
        it2 = VendorBillItem(
            bill_id=b.id, description="PW-BF-line-2",
            line_type=BillLineType.EXPENSE, account_id=exp_acc.id,
            quantity=1, unit_price=250, line_total=250,
        )
        db.session.add_all([it1, it2])
        db.session.commit()
        return {'vendor_id': v.id, 'sc_ab_id': sc_ab.id,
                 'sc_rf_id': sc_rf.id,
                 'it1_id': it1.id, 'it2_id': it2.id}


def _cleanup():
    from app import create_app, db
    from app.models import (
        Vendor, VendorBill, VendorBillItem, VendorSubCategory,
    )
    from sqlalchemy import text
    app = create_app()
    with app.app_context():
        VendorBillItem.query.filter(
            VendorBillItem.description.like('PW-BF-%')).delete()
        VendorBill.query.filter(
            VendorBill.number.like('PW-BF-%')).delete()
        VendorSubCategory.query.filter(
            VendorSubCategory.name.like('PW-BF-%')).delete()
        Vendor.query.filter(Vendor.name.like('PW-BF-%')).delete()
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

            # Open the categorize page.
            page.goto(
                f"{BASE}/vendors/{seed['vendor_id']}/bill-items/categorize",
                wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "01_page_open.png"),
                            full_page=True)
            html = page.content()
            _record(
                "PW-BF-line-1" in html and "PW-BF-line-2" in html,
                "1. bulk categorize page lists both old bill lines",
                "lines missing",
            )
            _record(
                "PW-BF-Abdelhamid" in html and "PW-BF-Rofida" in html,
                "2. sub-cat options populate the dropdowns",
                "sub-cats missing from selects",
            )

            # Assign line 1 → Abdelhamid, line 2 → Rofida, save-all.
            page.select_option(
                f'select[name="item_subcat_{seed["it1_id"]}"]',
                str(seed["sc_ab_id"]))
            page.select_option(
                f'select[name="item_subcat_{seed["it2_id"]}"]',
                str(seed["sc_rf_id"]))
            page.locator(
                'button[type="submit"]:has-text("حفظ الكل")'
            ).first.click()
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(SHOTS / "02_after_save.png"),
                            full_page=True)

            # Reload + verify persistence.
            from app import create_app, db
            from app.models import VendorBillItem
            app = create_app()
            with app.app_context():
                it1 = db.session.get(VendorBillItem, seed['it1_id'])
                it2 = db.session.get(VendorBillItem, seed['it2_id'])
                _record(
                    it1.sub_category_id == seed['sc_ab_id']
                    and it2.sub_category_id == seed['sc_rf_id'],
                    "3. save-all persisted both line assignments",
                    f"it1={it1.sub_category_id}, it2={it2.sub_category_id}",
                )

            # Filter=uncategorized should now show 0 lines.
            page.goto(
                f"{BASE}/vendors/{seed['vendor_id']}/bill-items/categorize?filter=uncategorized",
                wait_until="networkidle")
            body = page.content()
            _record(
                "PW-BF-line-1" not in body and "PW-BF-line-2" not in body,
                "4. filter=uncategorized hides now-tagged lines",
                "tagged lines still visible under uncategorized filter",
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
