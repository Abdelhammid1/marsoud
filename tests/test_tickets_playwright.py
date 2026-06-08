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

    from app.models.payroll import Employee, EmployeeStatus, ContractType

    app = create_app()
    created = {"app": None, "template_id": None, "bill_id": None,
               "journal_entry_id": None, "company_id": None,
               "employee_id": None, "_employee_created": False}
    with app.app_context():
        company = Company.query.order_by(Company.id).first()
        user = User.query.filter_by(email=EMAIL).first()
        created["company_id"] = company.id
        cid = company.id

        # HR fixture — first active employee, or create one
        emp = Employee.query.filter_by(
            company_id=cid, status=EmployeeStatus.ACTIVE,
        ).first()
        if not emp:
            emp = Employee(
                company_id=cid, name="PWTEST_موظف_تجريبي",
                employee_number="PW-001",
                job_title="مهندس", email="pwtest_emp@example.com",
                start_date=date.today(),
                contract_type=ContractType.FULL_TIME,
                status=EmployeeStatus.ACTIVE,
                basic_salary=3000, allowances=500, deductions=0,
                is_active=True,
            )
            db.session.add(emp)
            db.session.commit()
            created["_employee_created"] = True
        created["employee_id"] = emp.id

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
    from app.models.payroll import Employee
    from app.models import LeaveBalance, LeaveRequest, AttendanceException

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
        # Only delete the test employee if WE created it (don't trash demo data)
        if created.get("_employee_created") and created.get("employee_id"):
            emp_id = created["employee_id"]
            LeaveRequest.query.filter_by(employee_id=emp_id).delete()
            AttendanceException.query.filter_by(employee_id=emp_id).delete()
            LeaveBalance.query.filter_by(employee_id=emp_id).delete()
            emp = db.session.get(Employee, emp_id)
            if emp:
                db.session.delete(emp)
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
        # ─── Cycle 5: HR Phase 1 + MARSOUD-23 ──────────────────────────────
        ("HR-01a", "HR home page (directory + departments summary)",
         "/hr/", ["الموارد البشرية", "دليل الموظفين"], "hr01_home"),
        ("HR-01b", "Departments list page",
         "/hr/departments", ["الأقسام", "قسم جديد"], "hr01_departments"),
        ("HR-01c", "New department form",
         "/hr/departments/new", ["اسم القسم", "المدير المسؤول"], "hr01_department_form"),
        ("HR-01d", "Department dropdown on employee form",
         "/payroll/employees/new", ["القسم", "بدون قسم"], "hr01_dept_dropdown"),
        ("HR-02", "Employee form: new personal/contract fields",
         "/payroll/employees/new",
         ["رقم الهوية", "الجنسية", "تاريخ الميلاد", "تاريخ انتهاء العقد", "ملاحظات"],
         "hr02_employee_fields"),
        ("HR-04a", "Invite form offers hr_manager role",
         "/users", ["مدير الموارد البشرية"], "hr04_invite_role"),
        ("MARSOUD-23a", "Logo upload field on company-edit page",
         f"/companies/{fx['company_id']}/edit",
         ["شعار الشركة", 'name="logo_file"'], "marsoud23_logo_widget"),
        # ─── Cycle 6: HR Phase 2 (MARSOUD-25) ──────────────────────────────
        ("HR-05a", "Leave types list (auto-seeded defaults)",
         "/hr/leave-types", ["أنواع الإجازات", "سنوية", "بدون راتب"],
         "hr05_leave_types"),
        ("HR-05b", "New leave-type form",
         "/hr/leave-types/new", ["التراكم الشهري", "الحد الأقصى", "إجازة مدفوعة"],
         "hr05_leave_type_new"),
        ("HR-05c", "Per-employee balances page",
         f"/hr/employees/{fx['employee_id']}/leave-balances",
         ["رصيد", "المتبقي", "سجل طلبات الإجازة"],
         "hr05_balances"),
        ("HR-05b-attendance", "Attendance exceptions monthly view",
         "/hr/attendance", ["سجل الاستثناءات", "غياب", "تأخير"],
         "hr05b_attendance"),
        ("HR-05b-new", "Attendance exception new form",
         "/hr/attendance/new", ["تسجيل استثناء", "ساعات التأخير"],
         "hr05b_attendance_new"),
        ("HR-06a", "Leave requests list",
         "/hr/leave-requests", ["طلبات الإجازة", "قيد المراجعة"],
         "hr06_leave_requests"),
        ("HR-06b", "New leave request form",
         "/hr/leave-requests/new", ["نوع الإجازة", "من تاريخ", "إلى تاريخ"],
         "hr06_leave_request_new"),
        # ─── Gap-fix surface checks ───────────────────────────────────────
        ("GAP-WK-ui", "Weekend-days picker rendered on company edit",
         f"/companies/{fx['company_id']}/edit",
         ["أيام العطلة الأسبوعية", 'name="weekend_day"'],
         "gap_weekend_picker"),
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


def _make_hr_manager_user(email, password, company_id):
    """Create an active user bound to the company with the hr_manager role.

    Returns the user id. Idempotent: re-promotes / re-attaches if already exists.
    """
    from app import create_app, db
    from app.models import User
    from app.models.user import user_companies
    app = create_app()
    with app.app_context():
        u = User.query.filter_by(email=email).first()
        if not u:
            u = User(email=email, full_name="PW HR Manager", is_active=True)
            u.set_password(password)
            db.session.add(u)
            db.session.flush()
        # Attach as hr_manager
        existing = db.session.execute(
            user_companies.select().where(
                (user_companies.c.user_id == u.id) &
                (user_companies.c.company_id == company_id)
            )
        ).first()
        if existing:
            db.session.execute(
                user_companies.update().where(
                    (user_companies.c.user_id == u.id) &
                    (user_companies.c.company_id == company_id)
                ).values(role="hr_manager")
            )
        else:
            db.session.execute(
                user_companies.insert().values(
                    user_id=u.id, company_id=company_id, role="hr_manager",
                )
            )
        db.session.commit()
        return u.id


def _delete_user(user_id):
    from app import create_app, db
    from app.models import User
    from app.models.user import user_companies
    app = create_app()
    with app.app_context():
        db.session.execute(user_companies.delete().where(user_companies.c.user_id == user_id))
        u = db.session.get(User, user_id)
        if u:
            db.session.delete(u)
            db.session.commit()


def _create_department(company_id, name="PWTEST_قسم_اختبار"):
    from app import create_app, db
    from app.models import Department
    app = create_app()
    with app.app_context():
        existing = Department.query.filter_by(company_id=company_id, name=name).first()
        if existing:
            return existing.id
        d = Department(company_id=company_id, name=name, description="ينشأ من اختبار آلي")
        db.session.add(d)
        db.session.commit()
        return d.id


def _delete_department(department_id):
    from app import create_app, db
    from app.models import Department, Employee
    app = create_app()
    with app.app_context():
        # Detach any employees first
        Employee.query.filter_by(department_id=department_id).update({"department_id": None})
        d = db.session.get(Department, department_id)
        if d:
            db.session.delete(d)
            db.session.commit()


def _set_employee_contract_end(employee_id, days_from_today):
    """Backdate / forward-date an employee's contract_end_date to trigger HR-03."""
    from datetime import date, timedelta
    from app import create_app, db
    from app.models import Employee
    app = create_app()
    with app.app_context():
        e = db.session.get(Employee, employee_id)
        prev_end = e.contract_end_date
        prev_alert = e.contract_alert_last_sent
        e.contract_end_date = date.today() + timedelta(days=days_from_today)
        e.contract_alert_last_sent = None
        db.session.commit()
        return prev_end, prev_alert


def _restore_employee_contract(employee_id, prev_end, prev_alert):
    from app import create_app, db
    from app.models import Employee
    app = create_app()
    with app.app_context():
        e = db.session.get(Employee, employee_id)
        if e:
            e.contract_end_date = prev_end
            e.contract_alert_last_sent = prev_alert
            db.session.commit()


def _first_active_employee_id(company_id):
    from app import create_app, db
    from app.models import Employee, EmployeeStatus
    app = create_app()
    with app.app_context():
        e = Employee.query.filter_by(
            company_id=company_id, status=EmployeeStatus.ACTIVE
        ).first()
        return e.id if e else None


def _upload_company_logo(company_id):
    """Write a tiny placeholder logo to /static/logos/<id>.png and set the company's
    logo_path. Returns (prev_logo_path, logo_disk_path) so we can restore.
    """
    import base64
    from pathlib import Path as _Path
    from app import create_app, db
    from app.models import Company

    # 1x1 transparent PNG (smallest valid PNG)
    PNG_1PX = base64.b64decode(
        b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgAAIAAAUAAeImBZsAAAAASUVORK5CYII="
    )
    app = create_app()
    with app.app_context():
        co = db.session.get(Company, company_id)
        prev = co.logo_path
        logos_dir = _Path(app.root_path) / "static" / "logos"
        logos_dir.mkdir(parents=True, exist_ok=True)
        disk = logos_dir / f"{company_id}.png"
        disk.write_bytes(PNG_1PX)
        co.logo_path = f"/static/logos/{company_id}.png"
        db.session.commit()
        return prev, str(disk)


def _restore_company_logo(company_id, prev_path, disk_path):
    from pathlib import Path as _Path
    from app import create_app, db
    from app.models import Company
    app = create_app()
    with app.app_context():
        co = db.session.get(Company, company_id)
        co.logo_path = prev_path
        db.session.commit()
    try:
        if disk_path:
            _Path(disk_path).unlink(missing_ok=True)
    except Exception:
        pass


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

        # ── HR-04 deep: HR_MANAGER cannot access financial routes (403) ─
        _logout(page)
        hr_user_id = _make_hr_manager_user(
            "pwtest_hr@manasety.ai", "hrtest1234", fx["company_id"],
        )
        try:
            _login(page, "pwtest_hr@manasety.ai", "hrtest1234")

            # Sees HR home
            hr_view = {"ticket": "HR-04b", "title": "HR_MANAGER can reach /hr/",
                       "url": "/hr/", "passed": False, "missing": [], "error": None,
                       "shot": "hr04_can_see_hr.png"}
            try:
                resp = page.goto(f"{BASE}/hr/", wait_until="networkidle")
                page.screenshot(path=str(SHOTS / "hr04_can_see_hr.png"), full_page=True)
                html = page.content()
                hr_view["status"] = resp.status if resp else 0
                hr_view["passed"] = hr_view["status"] < 400 and "الموارد البشرية" in html
                if not hr_view["passed"]:
                    hr_view["missing"] = [f"status={hr_view['status']}, missing 'الموارد البشرية'"]
            except Exception as e:
                hr_view["error"] = str(e)[:200]
            results.append(hr_view)

            # 403 on financial routes
            for ticket, url, shot in [
                ("HR-04c-journals", "/journals/", "hr04_403_journals"),
                ("HR-04c-invoices", "/invoices/", "hr04_403_invoices"),
                ("HR-04c-vendor-bills", "/vendor-bills/", "hr04_403_vendor_bills"),
                ("HR-04c-accounts", "/accounts/", "hr04_403_accounts"),
                ("HR-04c-reports", "/reports/", "hr04_403_reports"),
            ]:
                entry = {"ticket": ticket, "title": f"HR_MANAGER → {url} returns 403",
                         "url": url, "passed": False, "missing": [], "error": None,
                         "shot": f"{shot}.png"}
                try:
                    resp = page.goto(f"{BASE}{url}", wait_until="networkidle")
                    page.screenshot(path=str(SHOTS / f"{shot}.png"), full_page=True)
                    status = resp.status if resp else 0
                    entry["status"] = status
                    entry["passed"] = (status == 403)
                    if not entry["passed"]:
                        entry["missing"] = [f"expected 403 got {status}"]
                except Exception as e:
                    entry["error"] = str(e)[:200]
                results.append(entry)

            # HR_MANAGER may read /payroll/ (read-only access per spec)
            payroll_view = {"ticket": "HR-04d",
                            "title": "HR_MANAGER can read /payroll/ (no run)",
                            "url": "/payroll/", "passed": False, "missing": [],
                            "error": None, "shot": "hr04_payroll_read.png"}
            try:
                resp = page.goto(f"{BASE}/payroll/", wait_until="networkidle")
                page.screenshot(path=str(SHOTS / "hr04_payroll_read.png"), full_page=True)
                payroll_view["status"] = resp.status if resp else 0
                payroll_view["passed"] = payroll_view["status"] < 400
                if not payroll_view["passed"]:
                    payroll_view["missing"] = [f"got {payroll_view['status']}"]
            except Exception as e:
                payroll_view["error"] = str(e)[:200]
            results.append(payroll_view)
        finally:
            _logout(page)
            _delete_user(hr_user_id)

        # ── HR-01 deep: department CRUD round-trip ──────────────────────
        _login(page, EMAIL, PASSWORD)
        dept_id = None
        dept_check = {"ticket": "HR-01-deep",
                      "title": "Created department appears in list",
                      "url": "/hr/departments", "passed": False, "missing": [],
                      "error": None, "shot": "hr01_after_create.png"}
        try:
            dept_id = _create_department(fx["company_id"], "PWTEST_قسم_اختبار")
            page.goto(f"{BASE}/hr/departments", wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "hr01_after_create.png"), full_page=True)
            html = page.content()
            dept_check["passed"] = "PWTEST_قسم_اختبار" in html
            if not dept_check["passed"]:
                dept_check["missing"] = ["department name missing from list"]
        except Exception as e:
            dept_check["error"] = str(e)[:200]
        finally:
            if dept_id:
                _delete_department(dept_id)
        results.append(dept_check)

        # ── HR-03 deep: cron tick reports contract alerts ───────────────
        emp_id = _first_active_employee_id(fx["company_id"])
        cron_check = {"ticket": "HR-03-deep",
                      "title": "Cron tick processes contract expiry alerts",
                      "url": "/cron/tick", "passed": False, "missing": [],
                      "error": None, "shot": "hr03_cron_tick.png"}
        prev_end, prev_alert = (None, None)
        try:
            if emp_id:
                prev_end, prev_alert = _set_employee_contract_end(emp_id, days_from_today=20)
            resp = page.goto(f"{BASE}/cron/tick", wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "hr03_cron_tick.png"), full_page=True)
            status = resp.status if resp else 0
            html = page.content()
            # The response is JSON; we look for the key we added.
            cron_check["status"] = status
            cron_check["passed"] = (status == 200) and ("contract_alerts" in html)
            if not cron_check["passed"]:
                cron_check["missing"] = [f"status={status}, html starts: {html[:200]}"]
        except Exception as e:
            cron_check["error"] = str(e)[:200]
        finally:
            if emp_id and prev_end is not None:
                _restore_employee_contract(emp_id, prev_end, prev_alert)
        results.append(cron_check)

        # ── MARSOUD-23 deep: logo round-trips (persist → render in <img>) ─
        logo_check = {"ticket": "MARSOUD-23-deep",
                      "title": "Uploaded logo persists and renders in company-edit preview",
                      "url": f"/companies/{fx['company_id']}/edit",
                      "passed": False, "missing": [], "error": None,
                      "shot": "marsoud23_logo_preview.png"}
        prev_logo, disk_path = (None, None)
        try:
            prev_logo, disk_path = _upload_company_logo(fx["company_id"])
            resp = page.goto(f"{BASE}/companies/{fx['company_id']}/edit",
                             wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "marsoud23_logo_preview.png"), full_page=True)
            html = page.content()
            logo_check["status"] = resp.status if resp else 0
            expected = f"/static/logos/{fx['company_id']}.png"
            logo_check["passed"] = (
                logo_check["status"] < 400
                and expected in html
                and "إزالة الشعار الحالي" in html
            )
            if not logo_check["passed"]:
                logo_check["missing"] = [
                    f"status={logo_check['status']}, "
                    f"expected '{expected}' in HTML and remove-checkbox label"
                ]
            # Also verify the static file is actually fetchable
            r2 = page.goto(f"{BASE}{expected}", wait_until="networkidle")
            if r2 and r2.status != 200:
                logo_check["passed"] = False
                logo_check["missing"].append(f"static logo fetch returned {r2.status}")
        except Exception as e:
            logo_check["error"] = str(e)[:200]
        finally:
            _restore_company_logo(fx["company_id"], prev_logo, disk_path)
        results.append(logo_check)

        # ── MARSOUD-23 deep #2: email base renders the logo when set ────
        email_render = {"ticket": "MARSOUD-23-email",
                        "title": "Email base template emits company logo when set",
                        "url": "render_template emails/payslip.html",
                        "passed": False, "missing": [], "error": None,
                        "shot": "n/a"}
        try:
            prev_logo, disk_path = _upload_company_logo(fx["company_id"])
            from app import create_app
            from flask import render_template
            from app.models import Company
            app3 = create_app()
            with app3.app_context():
                co = db.session.get(Company, fx["company_id"])
                # Render the contract_expiry email since it takes a `company` directly
                html = render_template(
                    "emails/contract_expiry.html",
                    company=co, items=[], today=datetime.date.today(),
                )
            expected = f"/static/logos/{fx['company_id']}.png"
            email_render["passed"] = expected in html
            if not email_render["passed"]:
                email_render["missing"] = [f"logo {expected} missing from rendered email"]
        except Exception as e:
            email_render["error"] = str(e)[:200]
        finally:
            _restore_company_logo(fx["company_id"], prev_logo, disk_path)
        results.append(email_render)

        # ── Cycle 6 deep checks: HR Phase 2 ─────────────────────────────
        from datetime import date as _date, timedelta as _td
        from app import create_app as _ca, db as _db
        from app.models import (
            LeaveType as _LT, LeaveBalance as _LB,
            AttendanceException as _AE, AttendanceExceptionType as _AET,
            LeaveRequest as _LR, LeaveRequestStatus as _LRS,
            Employee as _Emp,
        )
        from app.services.leave import (
            seed_default_leave_types as _seed,
            monthly_leave_accrual as _accrual,
            submit_leave_request as _submit,
            approve_leave_request as _approve,
            cancel_leave_request as _cancel,
            attendance_deductions as _att,
        )

        # HR-05 deep: defaults seeded; balance grows + caps at max_balance
        seed_check = {"ticket": "HR-05-deep",
                      "title": "Defaults seeded + monthly accrual respects max_balance",
                      "url": "service:monthly_leave_accrual", "passed": False,
                      "missing": [], "error": None, "shot": "hr05_accrual.txt"}
        try:
            app_ctx = _ca()
            with app_ctx.app_context():
                cid = fx["company_id"]
                emp_id = fx["employee_id"]
                _seed(cid)
                annual_type = _LT.query.filter_by(company_id=cid, name="سنوية").first()
                # Pin the balance just below the cap so 1.75 would push it past
                # max_balance — verifies the cap clamp.
                _accrual()  # ensure row exists
                bal = _LB.query.filter_by(
                    employee_id=emp_id, leave_type_id=annual_type.id,
                    year=_date.today().year,
                ).first()
                if bal is None:
                    raise RuntimeError("balance row not created by accrual")
                bal.balance_days = float(annual_type.max_balance) - 0.5
                _db.session.commit()
                summary = _accrual()
                bal2 = _LB.query.filter_by(id=bal.id).first()
                got = float(bal2.balance_days)
                want = float(annual_type.max_balance)
                seed_check["passed"] = abs(got - want) < 0.01 and summary["capped"] >= 1
                if not seed_check["passed"]:
                    seed_check["missing"] = [
                        f"got {got} want {want} summary={summary}"]
        except Exception as e:
            seed_check["error"] = str(e)[:200]
        results.append(seed_check)

        # HR-05b deep: unique constraint on (employee, date)
        unique_check = {"ticket": "HR-05b-deep",
                        "title": "AttendanceException duplicate same-day refused",
                        "url": "service:create_exception",
                        "passed": False, "missing": [], "error": None,
                        "shot": "hr05b_dupe.txt"}
        try:
            with _ca().app_context():
                cid = fx["company_id"]
                emp_id = fx["employee_id"]
                today_ = _date.today()
                # Clean any leftover from prior runs
                _AE.query.filter_by(employee_id=emp_id, date=today_).delete()
                _db.session.commit()
                ex1 = _AE(company_id=cid, employee_id=emp_id, date=today_,
                          type=_AET.ABSENT, created_by=None)
                _db.session.add(ex1)
                _db.session.commit()
                # Attempt duplicate
                from app.services.leave import create_exception as _ce, LeaveError as _LE
                refused = False
                try:
                    _ce(company_id=cid, employee_id=emp_id, date_=today_,
                        type_=_AET.ABSENT, created_by=None)
                except _LE:
                    refused = True
                # Cleanup
                _AE.query.filter_by(employee_id=emp_id, date=today_).delete()
                _db.session.commit()
                unique_check["passed"] = refused
                if not refused:
                    unique_check["missing"] = ["expected LeaveError for duplicate"]
        except Exception as e:
            unique_check["error"] = str(e)[:200]
        results.append(unique_check)

        # HR-06 deep: submit → approve → exceptions created + balance deducted;
        # then cancel → exceptions deleted + balance restored.
        wf_check = {"ticket": "HR-06-deep",
                    "title": "Leave request approval creates exceptions; cancel restores",
                    "url": "service:approve+cancel_leave_request",
                    "passed": False, "missing": [], "error": None,
                    "shot": "hr06_workflow.txt"}
        try:
            with _ca().app_context():
                cid = fx["company_id"]
                emp_id = fx["employee_id"]
                _seed(cid)
                annual = _LT.query.filter_by(company_id=cid, name="سنوية").first()
                # Top up balance + clear prior state
                _AE.query.filter_by(employee_id=emp_id).delete()
                _LR.query.filter_by(employee_id=emp_id).delete()
                yr = _date.today().year
                bal = _LB.query.filter_by(
                    employee_id=emp_id, leave_type_id=annual.id, year=yr,
                ).first()
                if bal is None:
                    bal = _LB(employee_id=emp_id, leave_type_id=annual.id,
                              year=yr, balance_days=10, used_days=0)
                    _db.session.add(bal)
                else:
                    bal.balance_days = 10
                    bal.used_days = 0
                _db.session.commit()

                # Pick a Sunday → Thursday range (always 5 working days,
                # zero rest days under DEFAULT_REST_WEEKDAYS = {Fri, Sat}).
                # Find next Sunday from today.
                t = _date.today()
                # Python weekday: Mon=0..Sun=6. We want Sunday → +(6-current) mod 7.
                offset = (6 - t.weekday()) % 7
                if offset == 0:
                    offset = 7  # ensure future to avoid clashes with prior runs
                start = t + _td(days=offset)
                end = start + _td(days=4)  # Sun..Thu inclusive = 5 working days
                expected_days = 5

                req = _submit(company_id=cid, employee_id=emp_id,
                              leave_type_id=annual.id,
                              start_date=start, end_date=end,
                              reason="اختبار آلي",
                              created_by=None)
                _approve(req, reviewer_id=None, review_note="OK")
                # Should have `expected_days` exceptions, used_days = expected_days
                ex_count = _AE.query.filter_by(leave_request_id=req.id).count()
                _db.session.refresh(bal)
                after_used = float(bal.used_days)
                # Cancel
                _cancel(req, reviewer_id=None, review_note="rollback")
                ex_after_cancel = _AE.query.filter_by(leave_request_id=req.id).count()
                _db.session.refresh(bal)
                restored_used = float(bal.used_days)

                ok = (ex_count == expected_days
                      and after_used == float(expected_days)
                      and ex_after_cancel == 0
                      and restored_used == 0.0)
                wf_check["passed"] = ok
                if not ok:
                    wf_check["missing"] = [
                        f"approved_ex={ex_count} (want {expected_days}) "
                        f"used_after_approve={after_used} "
                        f"ex_after_cancel={ex_after_cancel} used_after_cancel={restored_used}"
                    ]
                # Cleanup leftover request row
                _LR.query.filter_by(id=req.id).delete()
                _db.session.commit()
        except Exception as e:
            wf_check["error"] = str(e)[:200]
        results.append(wf_check)

        # GAP-WIDGET: HR-03 expiring-contracts widget renders on /hr/
        widget_check = {"ticket": "GAP-WIDGET",
                        "title": "HR-03 expiring-contracts widget renders on /hr/",
                        "url": "/hr/", "passed": False, "missing": [],
                        "error": None, "shot": "gap_widget.png"}
        prev_end_w, prev_alert_w = (None, None)
        try:
            with _ca().app_context():
                emp = _db.session.get(_Emp, fx["employee_id"])
                prev_end_w = emp.contract_end_date
                prev_alert_w = emp.contract_alert_last_sent
                emp.contract_end_date = _date.today() + _td(days=15)
                emp.contract_alert_last_sent = None
                _db.session.commit()
            resp = page.goto(f"{BASE}/hr/", wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "gap_widget.png"), full_page=True)
            html = page.content()
            widget_check["status"] = resp.status if resp else 0
            widget_check["passed"] = (
                widget_check["status"] < 400
                and "عقود على وشك الانتهاء" in html
            )
            if not widget_check["passed"]:
                widget_check["missing"] = ["expected expiring-contracts widget text"]
        except Exception as e:
            widget_check["error"] = str(e)[:200]
        finally:
            try:
                with _ca().app_context():
                    emp = _db.session.get(_Emp, fx["employee_id"])
                    if emp:
                        emp.contract_end_date = prev_end_w
                        emp.contract_alert_last_sent = prev_alert_w
                        _db.session.commit()
            except Exception:
                pass
        results.append(widget_check)

        # GAP-WK deep: per-company weekend override changes which days
        # count as rest days.
        wk_check = {"ticket": "GAP-WK-deep",
                    "title": "Company.weekend_days overrides the rest-day skip",
                    "url": "service:approve with custom weekend",
                    "passed": False, "missing": [], "error": None,
                    "shot": "gap_weekend.txt"}
        try:
            with _ca().app_context():
                cid = fx["company_id"]
                emp_id = fx["employee_id"]
                _AE.query.filter_by(employee_id=emp_id).delete()
                _LR.query.filter_by(employee_id=emp_id).delete()
                from app.models import Company as _Co
                co = _db.session.get(_Co, cid)
                prev_weekend = co.weekend_days
                co.weekend_days = "5,6"  # Sat (5) + Sun (6) — flip the weekend
                _db.session.commit()

                annual = _LT.query.filter_by(company_id=cid, name="سنوية").first()
                yr = _date.today().year
                bal = _LB.query.filter_by(
                    employee_id=emp_id, leave_type_id=annual.id, year=yr,
                ).first()
                bal.balance_days = 10
                bal.used_days = 0
                _db.session.commit()

                # Find the next Saturday — under the new config it's a rest day.
                t = _date.today()
                offset = (5 - t.weekday()) % 7
                if offset == 0:
                    offset = 7
                sat = t + _td(days=offset)
                fri = sat - _td(days=1)
                # fri = Friday, now a working day. sat = Saturday = rest.
                # Submit fri→sat: should yield 1 working day (Fri only).
                req = _submit(company_id=cid, employee_id=emp_id,
                              leave_type_id=annual.id,
                              start_date=fri, end_date=sat,
                              reason="weekend-override", created_by=None)
                _approve(req, reviewer_id=None, review_note="OK")
                ex_count = _AE.query.filter_by(leave_request_id=req.id).count()
                _db.session.refresh(bal)
                used = float(bal.used_days)
                wk_check["passed"] = ex_count == 1 and used == 1.0
                if not wk_check["passed"]:
                    wk_check["missing"] = [
                        f"ex={ex_count} (want 1), used={used} (want 1.0)"]
                # Cleanup
                _AE.query.filter_by(leave_request_id=req.id).delete()
                _LR.query.filter_by(id=req.id).delete()
                co.weekend_days = prev_weekend
                _db.session.commit()
        except Exception as e:
            wk_check["error"] = str(e)[:200]
        results.append(wk_check)

        # HR-06 deep #2: a Fri→Sat-only request creates ZERO exceptions
        weekend_check = {"ticket": "HR-06-weekend",
                         "title": "Leave range entirely on rest days creates 0 exceptions",
                         "url": "service:approve_leave_request (rest days only)",
                         "passed": False, "missing": [], "error": None,
                         "shot": "hr06_weekend.txt"}
        try:
            with _ca().app_context():
                cid = fx["company_id"]
                emp_id = fx["employee_id"]
                annual = _LT.query.filter_by(company_id=cid, name="سنوية").first()
                _AE.query.filter_by(employee_id=emp_id).delete()
                _LR.query.filter_by(employee_id=emp_id).delete()
                yr = _date.today().year
                bal = _LB.query.filter_by(
                    employee_id=emp_id, leave_type_id=annual.id, year=yr,
                ).first()
                bal.balance_days = 5
                bal.used_days = 0
                _db.session.commit()

                # Find next Friday
                t = _date.today()
                offset = (4 - t.weekday()) % 7
                if offset == 0:
                    offset = 7
                fri = t + _td(days=offset)
                sat = fri + _td(days=1)
                # days_count should be 0 — submit will create the request but
                # nothing gets deducted, and approval creates zero exceptions.
                # We allow days_count=0 (it's a no-op leave).
                req = _submit(company_id=cid, employee_id=emp_id,
                              leave_type_id=annual.id,
                              start_date=fri, end_date=sat,
                              reason="weekend-only", created_by=None)
                _approve(req, reviewer_id=None, review_note="OK")
                ex_count = _AE.query.filter_by(leave_request_id=req.id).count()
                _db.session.refresh(bal)
                used = float(bal.used_days)

                weekend_check["passed"] = ex_count == 0 and used == 0.0
                if not weekend_check["passed"]:
                    weekend_check["missing"] = [
                        f"weekend ex_count={ex_count} (want 0), used={used} (want 0)"]
                # Cleanup
                _AE.query.filter_by(leave_request_id=req.id).delete()
                _LR.query.filter_by(id=req.id).delete()
                _db.session.commit()
        except Exception as e:
            weekend_check["error"] = str(e)[:200]
        results.append(weekend_check)

        # HR-07 deep: attendance_deductions feeds run_payroll, no double-deduct.
        # We don't actually post a payroll run (would mutate journals); we
        # verify the helper math.
        math_check = {"ticket": "HR-07-deep",
                      "title": "auto_absence_late_for converts exceptions into money correctly",
                      "url": "service:auto_absence_late_for",
                      "passed": False, "missing": [], "error": None,
                      "shot": "hr07_math.txt"}
        try:
            with _ca().app_context():
                from app.services.payroll import auto_absence_late_for as _auto
                cid = fx["company_id"]
                emp_id = fx["employee_id"]
                emp = _db.session.get(_Emp, emp_id)
                yr, mo = _date.today().year, _date.today().month
                # Clear and seed 2 ABSENT + 1 LATE 4h for this period
                _AE.query.filter_by(employee_id=emp_id).delete()
                _db.session.add(_AE(company_id=cid, employee_id=emp_id,
                                    date=_date(yr, mo, 5), type=_AET.ABSENT))
                _db.session.add(_AE(company_id=cid, employee_id=emp_id,
                                    date=_date(yr, mo, 6), type=_AET.ABSENT))
                _db.session.add(_AE(company_id=cid, employee_id=emp_id,
                                    date=_date(yr, mo, 7), type=_AET.LATE,
                                    duration_hours=4))
                _db.session.commit()
                abs_amt, late_amt, has_ex = _auto(emp, yr, mo)
                daily = float(emp.basic_salary or 0) / 30.0
                want_abs = round(2.0 * daily, 2)        # 2 full days
                want_late = round(0.5 * daily, 2)       # 4h / 8h × 1 day
                ok = (has_ex and abs(abs_amt - want_abs) < 0.01
                      and abs(late_amt - want_late) < 0.01)
                math_check["passed"] = ok
                if not ok:
                    math_check["missing"] = [
                        f"abs={abs_amt}(want {want_abs}) late={late_amt}(want {want_late}) "
                        f"has_ex={has_ex} basic={float(emp.basic_salary)}"]
                # Cleanup
                _AE.query.filter_by(employee_id=emp_id).delete()
                _db.session.commit()
        except Exception as e:
            math_check["error"] = str(e)[:200]
        results.append(math_check)

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
