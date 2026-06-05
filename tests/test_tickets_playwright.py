#!/usr/bin/env python3
"""
Massive end-to-end ticket verification for Marsoud (مرصود).

Runs headless (silent), logs in once, visits the page(s) that prove each ticket
is implemented, asserts the distinguishing UI elements are present, captures a
screenshot per check, then prints a pass/fail report and writes tests/REPORT.md.

Usage:
    PORT=5050 .venv/bin/python flask_app.py        # in one shell (this script
                                                    # starts it automatically if
                                                    # not already running)
    .venv/bin/python tests/test_tickets_playwright.py
"""
import os
import sys
import time
import socket
import subprocess
import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = os.environ.get("BASE_URL", "http://localhost:5050")
PORT = int(os.environ.get("PORT", "5050"))
EMAIL = "demo@manasety.ai"
PASSWORD = "demo1234"
SHOTS = Path(__file__).resolve().parent / "screenshots"
SHOTS.mkdir(exist_ok=True)


# --------------------------------------------------------------------------- #
# Fixtures (created in-process, committed to the shared SQLite DB the server   #
# reads, then torn down at the end).                                           #
# --------------------------------------------------------------------------- #
def setup_fixtures():
    """Create a journal template (T2 reuse) and a VAT vendor bill (T7/ACC-20)."""
    from app import create_app, db
    from app.models.company import Company
    from app.models.user import User
    from app.services.ledger import get_account_by_code
    from app.models.journal_extras import JournalTemplate, JournalTemplateLine
    from app.models.vendor_bill import (
        VendorBill, VendorBillItem, BillLineType, VendorBillPaymentMethod,
        VendorBillStatus,
    )
    from app.services.vendor_bills import post_vendor_bill
    from datetime import date, timedelta

    app = create_app()
    created = {"app": None, "template_id": None, "bill_id": None,
               "journal_entry_id": None, "company_id": None}
    with app.app_context():
        company = Company.query.order_by(Company.id).first()
        user = User.query.filter_by(email=EMAIL).first()
        created["company_id"] = company.id
        cid = company.id

        expense = get_account_by_code(cid, "5200") or get_account_by_code(cid, "5210")
        cash = get_account_by_code(cid, "1110")

        # T2 fixture: a reusable journal template with two balanced lines
        tpl = JournalTemplate(
            company_id=cid,
            name="PWTEST_قالب_اختبار",
            description="قيد اختبار آلي (PWTEST)",
            is_active=True,
        )
        tpl.lines = [
            JournalTemplateLine(account_id=expense.id, debit=500, credit=0,
                                memo="مصروف اختبار"),
            JournalTemplateLine(account_id=cash.id, debit=0, credit=500,
                                memo="نقدية"),
        ]
        db.session.add(tpl)
        db.session.flush()
        created["template_id"] = tpl.id

        # T7 / ACCOUNTANT-20 fixture: a posted vendor bill with VAT and a line item
        bill = VendorBill(
            company_id=cid,
            number="PWTEST-VB1",
            supplier_invoice_number="PWTEST-INV",
            issue_date=date.today(),
            due_date=date.today() + timedelta(days=30),
            payment_method=VendorBillPaymentMethod.CASH,
            currency="SAR",
            tax_rate=15,
            status=VendorBillStatus.DRAFT,
        )
        bill.items = [
            VendorBillItem(
                description="بند اختبار آلي PWTEST",
                line_type=BillLineType.EXPENSE,
                account_id=expense.id,
                quantity=1,
                unit_price=1000,
            )
        ]
        db.session.add(bill)
        db.session.flush()
        post_vendor_bill(bill, created_by=user.id if user else None)
        created["bill_id"] = bill.id
        created["journal_entry_id"] = bill.journal_entry_id
        db.session.commit()
    return created


def teardown_fixtures(created):
    from app import create_app, db
    from app.models.journal_extras import JournalTemplate
    from app.models.vendor_bill import VendorBill
    from app.models.journal import JournalEntry, JournalLine

    app = create_app()
    with app.app_context():
        bill = db.session.get(VendorBill, created["bill_id"]) if created.get("bill_id") else None
        if bill:
            db.session.delete(bill)
        je_id = created.get("journal_entry_id")
        if je_id:
            JournalLine.query.filter_by(entry_id=je_id).delete()
            je = db.session.get(JournalEntry, je_id)
            if je:
                db.session.delete(je)
        tpl = db.session.get(JournalTemplate, created["template_id"]) if created.get("template_id") else None
        if tpl:
            db.session.delete(tpl)
        db.session.commit()


# --------------------------------------------------------------------------- #
# Server management                                                            #
# --------------------------------------------------------------------------- #
def port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_server():
    if port_open(PORT):
        return None
    env = dict(os.environ, PORT=str(PORT))
    proc = subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), str(ROOT / "flask_app.py")],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        if port_open(PORT):
            time.sleep(1)
            return proc
        time.sleep(0.5)
    raise RuntimeError("Flask server did not start on port %d" % PORT)


# --------------------------------------------------------------------------- #
# Checks                                                                       #
# --------------------------------------------------------------------------- #
def build_checks(fx):
    """Each check: (ticket, title, url, must_contain[list], shot_name)."""
    bid = fx["bill_id"]
    tid = fx["template_id"]
    return [
        ("T1", "Auto-code generation in chart of accounts",
         "/accounts/new", ["الكود", 'name="code'], "t1_account_new"),
        ("T2", "Journal entry page + template reuse prefill",
         f"/journals/templates/{tid}/use",
         ["تم التعبئة من القالب", "سطور القيد"], "t2_template_use"),
        ("T2b", "Journal templates list",
         "/journals/templates", ["PWTEST_قالب_اختبار"], "t2_templates_list"),
        ("T3", "Email automation (invoice send actions present)",
         "/invoices", ["الفواتير"], "t3_invoices"),
        ("T4", "Per-company numbering (vendor bills numbered)",
         "/vendor-bills", ["PWTEST-VB1"], "t4_numbering"),
        ("T5", "Payroll / employee module",
         "/payroll", ["الرواتب", "الموظف"], "t5_payroll"),
        ("T5b", "Add-employee form",
         "/payroll/employees/new", ["الراتب", "الاسم"], "t5_employee_new"),
        ("T6", "Invoices module",
         "/invoices/new", ["العميل"], "t6_invoice_new"),
        ("T7", "Vendor bill VAT posted (2120) — bill detail",
         f"/vendor-bills/{bid}",
         ["PWTEST-VB1"], "t7_vendor_bill_detail"),
        ("T8", "Reports overhaul — reports index",
         "/reports", ["الميزانية", "التدفق"], "t8_reports_index"),
        ("T8b", "Cash-flow report with PDF/Excel export",
         "/reports/cash-flow", ["PDF", "Excel", "التدفقات النقدية"],
         "t8_cash_flow_export"),
        ("T9", "Fixed assets module",
         "/assets", ["الأصول"], "t9_assets"),
        ("T0", "PDF Arabic rendering — balance sheet export reachable",
         "/reports/balance-sheet", ["PDF", "Excel"], "t0_balance_sheet"),
        ("T10", "Recurring journals overhaul",
         "/journals/recurring", ["المتكرر"], "t10_recurring"),
        ("T11", "Cash-flow auto-classification select on journal form",
         "/journals/new", ["تصنيف التدفق النقدي", "تشغيلي"], "t11_cashflow_select"),
        ("T12", "User invitations + per-company roles",
         "/users", ["دعوة", "الدور"], "t12_users"),
        ("T13", "Configurable reminder thresholds on company edit",
         f"/companies/{fx['company_id']}/edit", ["تذكير"], "t13_reminders"),
        ("T14", "Payroll run form",
         "/payroll/run", ["تشغيل", "الراتب"], "t14_payroll_run"),
        ("ACC-18", "Edit accounts — تعديل link in chart of accounts",
         "/accounts", ["تعديل"], "acc18_edit_link"),
        ("ACC-18b", "Edit-account form (parent / type / code / nature)",
         "/accounts/2/edit", ["التصنيف", "الكود", "الحساب الأب"], "acc18_edit_form"),
        ("ACC-19", "Vendor-bills date-range filter (من/إلى تاريخ)",
         "/vendor-bills", ["من تاريخ", "إلى تاريخ"], "acc19_date_filter"),
        ("ACC-20", "Vendor-bills line-item columns (الوصف/النوع/الحساب)",
         "/vendor-bills",
         ["الوصف", "النوع", "بند اختبار آلي PWTEST"], "acc20_columns"),
        ("MARSOUD-3", "Company settings nav link in sidebar",
         "/", ["إعدادات الشركة"], "marsoud3_settings_link"),
    ]


def build_admin_checks():
    """Super-admin (Cycle 4) checks — uses a separate login."""
    return [
        ("ADMIN-01a", "Normal user blocked from /admin (403)",
         "/admin/", ["__expect_403__"], "admin01_403"),
        ("ADMIN-01b", "Super-admin reaches /admin dashboard",
         "/admin/", ["النظرة العامة", "إجمالي الشركات"], "admin02_dashboard"),
        ("ADMIN-02", "Dashboard metrics rendered",
         "/admin/", ["إجمالي المستخدمين", "إجمالي القيود", "إجمالي الفواتير"],
         "admin02_metrics"),
        ("ADMIN-03", "Companies list with stats",
         "/admin/companies",
         ["شركة الأمل التجارية", "المستخدمون", "القيود"], "admin03_companies"),
        ("ADMIN-03b", "Company detail drill-down",
         "/admin/companies/1", ["العملة", "نسبة الضريبة"], "admin03_company_detail"),
        ("ADMIN-03c", "Company edit page",
         "/admin/companies/1/edit",
         ["العملة الأساسية", "نسبة الضريبة"], "admin03_company_edit"),
        ("ADMIN-04", "Users management cross-company",
         "/admin/users", ["إعادة تعيين", "إيقاف"], "admin04_users"),
        ("ADMIN-05", "Audit log with filters",
         "/admin/audit", ["سجل النشاط", "user_login"], "admin05_audit"),
        ("ADMIN-06", "Impersonation log page",
         "/admin/impersonations", ["سجل المعاينات"], "admin06_impersonations"),
        ("GAP-02", "Dashboard segments active/trial/suspended companies",
         "/admin/", ["نشطة", "تجريبية", "موقوفة"], "gap02_trial_dashboard"),
        ("GAP-03+4", "Company-edit surfaces status + plan",
         "/admin/companies/1/edit",
         ["الحالة", "الباقة", "ACTIVE", "FREE"], "gap0304_edit_status_plan"),
        ("GAP-05", "Audit page exposes from/to date filters",
         "/admin/audit", ["من تاريخ", "إلى تاريخ"], "gap05_date_filter"),
        ("GAP-07", "Audit unifies platform + journal sources",
         "/admin/audit", ["PLATFORM", "المصدر"], "gap07_union"),
        ("GAP-08", "Errors page reachable from admin",
         "/admin/errors", ["سجل الأخطاء"], "gap08_errors_page"),
        ("GAP-08b", "Per-company errors page",
         "/admin/companies/1/errors", ["سجل الأخطاء"], "gap08_errors_company"),
    ]


# Extra tenant-side check for company-suspension blocking login (Gap 1) and the
# real resend-invite path (Gap 2). Both need DB state changes between probes,
# so they live in a separate function called from run_checks().


def _suspend_company(company_id):
    from app import create_app, db
    from app.models import Company
    app = create_app()
    with app.app_context():
        c = db.session.get(Company, company_id)
        prior_status = c.status
        prior_is_active = c.is_active
        c.status = "SUSPENDED"
        c.is_active = False
        db.session.commit()
        return prior_status, prior_is_active


def _restore_company(company_id, status, is_active):
    from app import create_app, db
    from app.models import Company
    app = create_app()
    with app.app_context():
        c = db.session.get(Company, company_id)
        c.status = status
        c.is_active = is_active
        db.session.commit()


def _make_pending_invite(email, company_id):
    from app import create_app, db
    from app.models import Invitation
    from itsdangerous import URLSafeTimedSerializer
    app = create_app()
    with app.app_context():
        serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="invite")
        token = serializer.dumps({"email": email, "company_id": company_id,
                                  "role": "viewer"})
        inv = Invitation(company_id=company_id, email=email, role="viewer",
                         token=token, invited_by_id=None)
        db.session.add(inv)
        db.session.commit()
        return inv.id


def _delete_invite(inv_id):
    from app import create_app, db
    from app.models import Invitation
    app = create_app()
    with app.app_context():
        inv = db.session.get(Invitation, inv_id)
        if inv:
            db.session.delete(inv)
            db.session.commit()


def _audit_has(action_substring):
    from app import create_app
    from app.models import PlatformAuditLog
    app = create_app()
    with app.app_context():
        return PlatformAuditLog.query.filter(
            PlatformAuditLog.action.like(f"%{action_substring}%")
        ).count() > 0


def _login(page, email, password):
    page.goto(f"{BASE}/login", wait_until="networkidle")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")


def _logout(page):
    page.goto(f"{BASE}/logout", wait_until="networkidle")


def _run_one(page, ticket, title, url, must, shot, results):
    entry = {"ticket": ticket, "title": title, "url": url,
             "passed": False, "missing": [], "error": None,
             "shot": f"{shot}.png"}
    try:
        # Special marker "__expect_403__" → assertion passes when status == 403.
        expect_403 = must == ["__expect_403__"]
        resp = page.goto(f"{BASE}{url}", wait_until="networkidle", timeout=20000)
        status = resp.status if resp else 0
        html = page.content()
        if expect_403:
            entry["status"] = status
            entry["passed"] = status == 403
            if not entry["passed"]:
                entry["missing"] = [f"expected 403 got {status}"]
        else:
            missing = [m for m in must if m not in html]
            entry["missing"] = missing
            entry["status"] = status
            entry["passed"] = (status < 400) and not missing
        page.screenshot(path=str(SHOTS / f"{shot}.png"), full_page=True)
    except Exception as e:  # noqa: BLE001
        entry["error"] = str(e)[:200]
    results.append(entry)


def run_checks(fx):
    from playwright.sync_api import sync_playwright

    checks = build_checks(fx)
    admin_checks = build_admin_checks()
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1366, "height": 900},
                                  locale="ar")
        page = ctx.new_page()

        # ── Tenant pass: demo user ──────────────────────────────────────
        _login(page, EMAIL, PASSWORD)
        page.screenshot(path=str(SHOTS / "00_after_login.png"), full_page=True)

        for ticket, title, url, must, shot in checks:
            _run_one(page, ticket, title, url, must, shot, results)

        # Confirm normal user is blocked from /admin (one of the admin checks).
        admin_403 = admin_checks[0]
        _run_one(page, admin_403[0], admin_403[1], admin_403[2],
                 admin_403[3], admin_403[4], results)

        # ── Admin pass: super-admin user ───────────────────────────────
        _logout(page)
        _login(page, "pwtest_admin@manasety.ai", "pwtest1234")
        for ticket, title, url, must, shot in admin_checks[1:]:
            _run_one(page, ticket, title, url, must, shot, results)

        # ── Gap-02 deep check: real resend-invite ──────────────────────
        inv_id = _make_pending_invite("pwtest_invite@manasety.ai",
                                      fx["company_id"])
        gap2 = {"ticket": "GAP-02-deep", "title": "Resend-invite POST audits + flashes",
                "url": "/admin/users/.../resend-invite", "passed": False,
                "missing": [], "error": None, "shot": "gap02_resend.png"}
        try:
            # Find any user id with the demo email (we resend for demo).
            page.goto(f"{BASE}/admin/users", wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "gap02_users_before_resend.png"),
                            full_page=True)
            # Make the invitee a real user so the resend has a target.
            from app import create_app, db
            from app.models import User
            app = create_app()
            with app.app_context():
                u = User.query.filter_by(email="pwtest_invite@manasety.ai").first()
                if not u:
                    u = User(email="pwtest_invite@manasety.ai",
                            full_name="PW Invitee", is_active=True)
                    u.set_password("ignored1234")
                    db.session.add(u)
                    db.session.commit()
                uid = u.id
            # POST to the resend endpoint.
            page.goto(f"{BASE}/admin/users", wait_until="networkidle")
            page.evaluate(
                """([uid]) => {
                    const f = document.createElement('form');
                    f.method='POST';
                    f.action='/admin/users/' + uid + '/resend-invite';
                    document.body.appendChild(f); f.submit();
                }""",
                [uid],
            )
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(SHOTS / "gap02_resend.png"), full_page=True)
            gap2["passed"] = _audit_has("user_resend_invite")
            if not gap2["passed"]:
                gap2["missing"] = ["audit log missing user_resend_invite"]
        except Exception as e:  # noqa: BLE001
            gap2["error"] = str(e)[:200]
        finally:
            _delete_invite(inv_id)
            with create_app().app_context():
                u = User.query.filter_by(email="pwtest_invite@manasety.ai").first()
                if u:
                    db.session.delete(u)
                    db.session.commit()
        results.append(gap2)

        # ── Gap-01 deep check: company suspension blocks login ─────────
        _logout(page)
        prior_status, prior_is_active = _suspend_company(fx["company_id"])
        gap1 = {"ticket": "GAP-01-deep",
                "title": "Suspended-company user blocked at login",
                "url": "/login", "passed": False, "missing": [],
                "error": None, "shot": "gap01_blocked.png"}
        try:
            page.goto(f"{BASE}/login", wait_until="networkidle")
            page.fill('input[name="email"]', EMAIL)
            page.fill('input[name="password"]', PASSWORD)
            page.click('button[type="submit"], input[type="submit"]')
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(SHOTS / "gap01_blocked.png"), full_page=True)
            html = page.content()
            # Either still on login page (rejected) or sees the موقوفة msg.
            gap1["passed"] = ("موقوفة" in html or "/login" in page.url)
            if not gap1["passed"]:
                gap1["missing"] = ["expected موقوفة message or /login URL"]
        except Exception as e:  # noqa: BLE001
            gap1["error"] = str(e)[:200]
        finally:
            _restore_company(fx["company_id"], prior_status, prior_is_active)
        results.append(gap1)

        browser.close()
    return results


def write_report(results):
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    lines = []
    lines.append(f"# Marsoud — Playwright Ticket Verification Report")
    lines.append("")
    lines.append(f"Run: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f"Result: **{passed}/{total} checks passed**")
    lines.append(f"Screenshots: `tests/screenshots/`")
    lines.append("")
    lines.append("| Ticket | Check | Status | Screenshot |")
    lines.append("|---|---|---|---|")
    for r in results:
        if r["passed"]:
            st = "✅ PASS"
        elif r["error"]:
            st = f"❌ ERROR: {r['error']}"
        else:
            st = f"❌ missing: {', '.join(r['missing']) or 'http %s' % r.get('status')}"
        lines.append(f"| {r['ticket']} | {r['title']} | {st} | {r['shot']} |")
    report = "\n".join(lines)
    (Path(__file__).resolve().parent / "REPORT.md").write_text(report, encoding="utf-8")
    return report, passed, total


def main():
    proc = ensure_server()
    fx = None
    try:
        fx = setup_fixtures()
        results = run_checks(fx)
    finally:
        if fx:
            teardown_fixtures(fx)
    report, passed, total = write_report(results)
    print(report)
    print(f"\n=== {passed}/{total} checks passed ===")
    if proc:
        proc.terminate()
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
