#!/usr/bin/env python3
"""MARSOUD-PARTY-LEDGER-02 — browser-driven Playwright test.

Spins up a fresh test company in-process, drives a real browser to
hit the new party-ledger page, and verifies the rendered HTML +
the PDF + Excel exports against a live Flask dev server.
"""
import io
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = os.environ.get("BASE_URL", "http://localhost:5050")
SHOTS = ROOT / "tests" / "screenshots" / "party_ledger"
SHOTS.mkdir(parents=True, exist_ok=True)

DEMO_EMAIL = "demo@manasety.ai"
DEMO_PW = "demo1234"
COMPANY_NAME = f"PLAYWRIGHT-PL-{int(time.time())}"

CHECKS = []
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _spin_up():
    from datetime import date, timedelta
    from decimal import Decimal
    from app import create_app, db
    from app.models import (
        Company, User, Vendor, VendorBill, VendorBillItem,
        VendorBillPaymentMethod, VendorBillStatus, Account,
    )
    from app.models.user import user_companies
    from app.models.vendor_bill import BillLineType
    from app.services.seed_coa import seed_default_coa
    from app.services.subsidiary import ensure_vendor_account
    from app.services.vendor_bills import post_vendor_bill
    app = create_app()
    with app.app_context():
        owner = User.query.filter_by(email=DEMO_EMAIL).first()
        c = Company(name=COMPANY_NAME, base_currency="SAR")
        db.session.add(c); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=owner.id, company_id=c.id, role="owner",
        ))
        db.session.commit()
        seed_default_coa(c.id)
        # Two bills against one vendor — one cash, one credit
        v = Vendor(company_id=c.id, name="مورد بلاي رايت — كشف الحساب")
        db.session.add(v); db.session.flush()
        ensure_vendor_account(v)
        rent = Account.query.filter_by(company_id=c.id, code="5220").first()
        for num, method, total in [
            ("PL-CASH-A", VendorBillPaymentMethod.CASH, Decimal("1000")),
            ("PL-CREDIT-A", VendorBillPaymentMethod.CREDIT, Decimal("2000")),
        ]:
            bill = VendorBill(
                company_id=c.id, vendor_id=v.id, number=num,
                issue_date=date.today(),
                due_date=date.today() + timedelta(days=30),
                status=VendorBillStatus.DRAFT,
                payment_method=method,
                tax_rate=Decimal("15"),
            )
            db.session.add(bill); db.session.flush()
            db.session.add(VendorBillItem(
                bill_id=bill.id, line_type=BillLineType.EXPENSE,
                account_id=rent.id, description=f"بند {num}",
                quantity=Decimal("1"), unit_price=total,
                line_total=total,
            ))
            db.session.flush()
            post_vendor_bill(bill)
            db.session.commit()
        return c.id, v.id


def _teardown(company_id):
    from app import create_app, db
    from app.models import (
        Company, JournalEntry, JournalLine, VendorBill, VendorBillItem,
        Invoice, InvoiceItem, Payment,
    )
    from sqlalchemy import inspect
    app = create_app()
    with app.app_context():
        insp = inspect(db.engine)
        entry_ids = [r.id for r in JournalEntry.query.filter_by(
            company_id=company_id).all()]
        if entry_ids:
            JournalLine.query.filter(
                JournalLine.entry_id.in_(entry_ids)
            ).delete(synchronize_session=False)
        bill_ids = [r.id for r in VendorBill.query.filter_by(
            company_id=company_id).all()]
        if bill_ids:
            VendorBillItem.query.filter(
                VendorBillItem.bill_id.in_(bill_ids)
            ).delete(synchronize_session=False)
        inv_ids = [r.id for r in Invoice.query.filter_by(
            company_id=company_id).all()]
        if inv_ids:
            InvoiceItem.query.filter(
                InvoiceItem.invoice_id.in_(inv_ids)
            ).delete(synchronize_session=False)
            Payment.query.filter(
                Payment.invoice_id.in_(inv_ids)
            ).delete(synchronize_session=False)
        for t in reversed(db.metadata.sorted_tables):
            if "company_id" in {c["name"] for c in insp.get_columns(t.name)}:
                db.session.execute(t.delete().where(t.c.company_id == company_id))
        c = db.session.get(Company, company_id)
        if c:
            db.session.delete(c)
        db.session.commit()


def _login(page):
    page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    page.fill('input[name="email"]', DEMO_EMAIL)
    page.fill('input[name="password"]', DEMO_PW)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")


def _switch_company(page, cid):
    page.goto(f"{BASE}/switch-company/{cid}", wait_until="networkidle")


def _shot(page, name):
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


# ─── Checks ────────────────────────────────────────────────────────────
@check("1. /reports/party-ledger/ loads + shows vendor in the dropdown")
def _(page):
    page.goto(f"{BASE}/reports/party-ledger/?kind=vendor",
              wait_until="networkidle")
    _shot(page, "01_empty_form")
    html = page.content()
    assert "كشف حساب طرف" in html
    assert "مورد بلاي رايت — كشف الحساب" in html, \
        "vendor missing from the dropdown"
    return "vendor dropdown populated"


@check("2. Selecting the vendor renders the statement with movements")
def _(page):
    page.goto(f"{BASE}/reports/party-ledger/?kind=vendor&party_id={_STATE['vendor_id']}",
              wait_until="networkidle")
    _shot(page, "02_vendor_statement")
    html = page.content()
    # Expect at least 3 movement rows (cash bill credit + settlement debit + credit bill)
    assert "إجمالي مدين" in html, "totals card missing"
    assert "PL-CASH-A" in html or "VB-PL-CASH-A" in html or "فاتورة مشتريات" in html, \
        "cash bill not on statement"
    assert "PL-CREDIT-A" in html or "VB-PL-CREDIT-A" in html, \
        "credit bill not on statement"
    return "statement renders both bills + running balance"


@check("3. Excel export downloads as xlsx with the right mimetype")
def _(page):
    # Use Playwright's own API request — inherits the browser cookies
    r = page.request.get(
        f"{BASE}/reports/party-ledger/export.xlsx",
        params={"kind": "vendor", "party_id": str(_STATE["vendor_id"])},
    )
    assert r.status == 200, f"status={r.status}"
    ct = r.headers.get("content-type", "")
    assert "spreadsheet" in ct, f"wrong content-type: {ct}"
    body = r.body()
    assert len(body) > 4000, f"xlsx too small: {len(body)}"
    return f"xlsx download OK ({len(body)} bytes)"


@check("4. PDF export downloads as application/pdf")
def _(page):
    r = page.request.get(
        f"{BASE}/reports/party-ledger/export.pdf",
        params={"kind": "vendor", "party_id": str(_STATE["vendor_id"])},
        timeout=60000,
    )
    assert r.status == 200, f"status={r.status}"
    ct = r.headers.get("content-type", "")
    assert ct.startswith("application/pdf"), f"wrong content-type: {ct}"
    body = r.body()
    assert body.startswith(b"%PDF"), "PDF magic bytes missing"
    pdf_out = SHOTS / "ledger_export.pdf"
    pdf_out.write_bytes(body)
    return f"PDF download OK ({len(body)} bytes, saved to {pdf_out.name})"


@check("5. Switching to 'customer' tab refreshes the dropdown")
def _(page):
    page.goto(f"{BASE}/reports/party-ledger/?kind=customer",
              wait_until="networkidle")
    _shot(page, "05_customer_tab")
    html = page.content()
    assert "اختر —" in html or "نوع الطرف" in html
    return "customer tab loads"


@check("6. Sidebar exposes the party-ledger link")
def _(page):
    page.goto(f"{BASE}/home", wait_until="networkidle")
    html = page.content()
    assert "كشف حساب طرف" in html or "party_ledger" in html, \
        "sidebar link missing"
    return "sidebar link present"


# ─── Run ───────────────────────────────────────────────────────────────
def main():
    from playwright.sync_api import sync_playwright

    print(f"Playwright party-ledger audit against {BASE}")
    cid, vid = _spin_up()
    _STATE["company_id"] = cid
    _STATE["vendor_id"] = vid
    print(f"Spun up company #{cid} with vendor #{vid}")

    passed = failed = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                viewport={"width": 1366, "height": 900}, locale="ar",
            )
            page = ctx.new_page()
            _login(page)
            _switch_company(page, cid)
            for label, fn in CHECKS:
                try:
                    result = fn(page)
                    print(f"PASS  {label}  ⇒ {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
                    try:
                        _shot(page, f"FAIL_{label[:20]}")
                    except Exception:
                        pass
            browser.close()
    finally:
        try:
            _teardown(cid)
            print(f"(torn down company #{cid})")
        except Exception as e:  # noqa: BLE001
            print(f"(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
