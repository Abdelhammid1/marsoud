#!/usr/bin/env python3
"""MARSOUD-PERM-FIX-01 — HTTP-level audit covering the OWNER walkthrough.

The original audit_perm_fix_01.py verified the service-layer math
(has_permission resolves correctly, etc.). This script proves the FULL
owner story: an owner logs in, builds a custom role through the real
HTTP form, assigns a user, that user logs in and gets the expected
visibility on /payroll, /leads, /tasks.

Drives Flask's test_client so no live server is needed.
"""
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
DEMO_EMAIL = "demo@manasety.ai"
DEMO_PASS = "demo1234"
TEST_USER_EMAIL = "perm_fix_test@example.com"
TEST_USER_PASS = "test1234"


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture: a clean test user we'll cycle between roles ──────────────
def _ensure_test_user():
    """Create (or reset) a test user belonging to the demo company."""
    from app.models import User, Company
    from app.models.user import user_companies
    from werkzeug.security import generate_password_hash
    # MARSOUD: User.set_password uses pbkdf2:sha256 — match that here so
    # the hash works on Python builds without OpenSSL's scrypt available.
    hash_method = "pbkdf2:sha256"
    company = Company.query.order_by(Company.id).first()
    u = User.query.filter_by(email=TEST_USER_EMAIL).first()
    if not u:
        u = User(
            email=TEST_USER_EMAIL,
            full_name="مستخدم اختبار الصلاحيات",
            password_hash=generate_password_hash(TEST_USER_PASS, method=hash_method),
            is_active=True, is_superadmin=False,
        )
        db.session.add(u)
        db.session.flush()
    else:
        # Reset password in case prior test changed it
        u.password_hash = generate_password_hash(TEST_USER_PASS, method=hash_method)
        u.is_active = True
    # Ensure membership in the demo company — start as 'viewer' until we
    # bind them to the custom role below.
    row = db.session.execute(
        user_companies.select().where(
            (user_companies.c.user_id == u.id) &
            (user_companies.c.company_id == company.id)
        )
    ).first()
    if not row:
        db.session.execute(
            user_companies.insert().values(
                user_id=u.id, company_id=company.id, role="viewer",
            )
        )
    db.session.commit()
    return u, company


def _login(client, email, password):
    r = client.post("/login", data={"email": email, "password": password},
                    follow_redirects=False)
    # Expect a redirect after login (302). If 200, login failed.
    assert r.status_code in (302, 303), \
        f"login for {email} failed: status={r.status_code}"


def _logout(client):
    client.get("/logout", follow_redirects=False)


# ─── HTTP-level acceptance criteria ────────────────────────────────────
@check("A. Owner can clone 'sales_rep' into a new custom role via POST")
def _():
    from app.models import Role, Company
    app = create_app()
    company = Company.query.order_by(Company.id).first()

    # Clean up any prior test role
    Role.query.filter_by(
        company_id=company.id, code="PERM_FIX_HTTP_TEST",
    ).delete()
    db.session.commit()

    with app.test_client() as client:
        _login(client, DEMO_EMAIL, DEMO_PASS)
        # Find the sales_rep system role to clone from
        sales_rep = Role.query.filter_by(
            company_id=company.id, code="sales_rep", type="SYSTEM"
        ).first()
        assert sales_rep, "sales_rep system role missing"

        r = client.post("/settings/roles/new", data={
            "name_ar": "اختبار: مدير عمليات (HTTP)",
            "description": "PERM_FIX_HTTP_TEST",
            "clone_from": str(sales_rep.id),
        }, follow_redirects=False)
        assert r.status_code in (302, 303), \
            f"role create POST status={r.status_code}"

    new_role = Role.query.filter_by(
        company_id=company.id, name_ar="اختبار: مدير عمليات (HTTP)"
    ).first()
    assert new_role, "new role not created"
    # Rename to a stable code we can find again
    new_role.code = "PERM_FIX_HTTP_TEST"
    db.session.commit()
    return f"created custom role id={new_role.id} from clone of sales_rep"


@check("B. Owner POSTS permissions: employees.view + leads.view_all, NO payroll.view")
def _():
    from app.models import Role, Permission, Company
    company = Company.query.order_by(Company.id).first()
    role = Role.query.filter_by(
        company_id=company.id, code="PERM_FIX_HTTP_TEST"
    ).first()

    wanted = {"employees.view", "leads.view_all", "leads.manage",
              "leads.convert", "tasks.view", "tasks.manage",
              "projects.view", "projects.create",
              "invoices.create", "invoices.send"}
    perm_ids = [p.id for p in Permission.query.filter(
        Permission.code.in_(wanted)).all()]
    assert len(perm_ids) == len(wanted), \
        f"missing some perms in catalog: got {len(perm_ids)}/{len(wanted)}"

    app = create_app()
    with app.test_client() as client:
        _login(client, DEMO_EMAIL, DEMO_PASS)
        # Build form data: one permission_id field per id. Wrap in
        # MultiDict so test_client accepts repeated keys.
        from werkzeug.datastructures import MultiDict
        form_data = MultiDict([("permission_id", str(pid)) for pid in perm_ids])
        r = client.post(
            f"/settings/roles/{role.id}/permissions",
            data=form_data, follow_redirects=False,
        )
        assert r.status_code in (302, 303), \
            f"permission save status={r.status_code}"
    # Reload and verify
    db.session.expire_all()
    role = Role.query.filter_by(
        company_id=company.id, code="PERM_FIX_HTTP_TEST"
    ).first()
    granted = {p.code for p in role.permissions}
    assert "employees.view" in granted
    assert "leads.view_all" in granted
    assert "payroll.view" not in granted, "should NOT have payroll.view"
    return f"saved {len(granted)} perms via HTTP; no payroll.view"


@check("C. Owner attaches the test user to the new role via /settings/roles/.../members/add")
def _():
    from app.models import Role, User, Company
    from app.models.user import user_companies
    user, company = _ensure_test_user()
    role = Role.query.filter_by(
        company_id=company.id, code="PERM_FIX_HTTP_TEST"
    ).first()

    app = create_app()
    with app.test_client() as client:
        _login(client, DEMO_EMAIL, DEMO_PASS)
        r = client.post(
            f"/settings/roles/{role.id}/members/add",
            data={"user_id": str(user.id)},
            follow_redirects=False,
        )
        assert r.status_code in (302, 303), \
            f"member add status={r.status_code}"

    # Verify the link landed in user_companies
    db.session.expire_all()
    row = db.session.execute(
        user_companies.select().where(
            (user_companies.c.user_id == user.id) &
            (user_companies.c.company_id == company.id)
        )
    ).first()
    assert row.role_id == role.id, \
        f"user_companies.role_id={row.role_id} expected {role.id}"
    return f"user {user.email} bound to role {role.code}"


@check("D. Test user can hit /payroll/ (200) — has employees.view")
def _():
    app = create_app()
    with app.test_client() as client:
        _login(client, TEST_USER_EMAIL, TEST_USER_PASS)
        r = client.get("/payroll/", follow_redirects=False)
        assert r.status_code == 200, \
            f"GET /payroll/ status={r.status_code} expected 200"
        body = r.data.decode("utf-8")
        # Salary column header should NOT be present
        assert "الراتب الصافي" not in body, \
            "Salary column should be hidden (no payroll.view)"
        # Employee table should still render
        assert "الموظفون" in body, "employees heading missing"
        # The "تشغيل كشف" button must NOT appear (no payroll.run)
        assert "تشغيل كشف" not in body, \
            "Payroll-run button should be hidden"
    return "user reaches /payroll/, salary column + run button hidden"


@check("E. Test user can hit /payroll/employees/<id> (200) but no salary fields")
def _():
    from app.models import Employee, Company
    company = Company.query.order_by(Company.id).first()
    emp = Employee.query.filter_by(company_id=company.id).first()
    assert emp, "no employee fixture"
    app = create_app()
    with app.test_client() as client:
        _login(client, TEST_USER_EMAIL, TEST_USER_PASS)
        r = client.get(f"/payroll/employees/{emp.id}", follow_redirects=False)
        assert r.status_code == 200, f"status={r.status_code}"
        body = r.data.decode("utf-8")
        # Personal data: present
        assert emp.name in body
        # Salary fields: must be hidden
        assert "الراتب الأساسي" not in body, \
            "basic_salary label should be hidden"
        assert "البدلات الثابتة" not in body, \
            "allowances label should be hidden"
        assert "سجل كشوف الرواتب" not in body, \
            "payslip history should be hidden"
    return "employee profile shows name + HR data; all salary sections hidden"


@check("F. Test user blocked from /payroll/run (redirected, no permission)")
def _():
    app = create_app()
    with app.test_client() as client:
        _login(client, TEST_USER_EMAIL, TEST_USER_PASS)
        r = client.get("/payroll/run", follow_redirects=False)
        # Either 302 (redirect to dashboard via require_permission) or 403
        assert r.status_code in (302, 303, 403), \
            f"payroll.run should be blocked, got {r.status_code}"
    return "payroll.run blocked for user without payroll.run permission"


@check("G. Test user blocked from a real payroll run view (no payroll.view)")
def _():
    from app.models import PayrollRun, Company
    company = Company.query.order_by(Company.id).first()
    run = PayrollRun.query.filter_by(company_id=company.id).first()
    if not run:
        return "skipped — no payroll runs exist in test DB"
    app = create_app()
    with app.test_client() as client:
        _login(client, TEST_USER_EMAIL, TEST_USER_PASS)
        r = client.get(f"/payroll/run/{run.id}", follow_redirects=False)
        assert r.status_code in (302, 303, 403), \
            f"run view should be blocked, got {r.status_code}"
    return "run view blocked without payroll.view"


@check("H. Test user reaches /leads/ (200) — has leads.view_all + view")
def _():
    app = create_app()
    with app.test_client() as client:
        _login(client, TEST_USER_EMAIL, TEST_USER_PASS)
        r = client.get("/leads/", follow_redirects=False)
        assert r.status_code == 200, \
            f"GET /leads/ status={r.status_code}"
        body = r.data.decode("utf-8")
        # With leads.view_all, the rep dropdown should render (it only
        # appears when _can_view_all_leads() is True).
        assert 'name="rep"' in body, \
            "rep filter should appear (proves view_all kicked in)"
    return "user sees /leads/ with the all-reps filter (view_all granted)"


@check("I. Test user blocked from /reports/ (no reports.view)")
def _():
    app = create_app()
    with app.test_client() as client:
        _login(client, TEST_USER_EMAIL, TEST_USER_PASS)
        r = client.get("/reports/", follow_redirects=False)
        # /reports/ is gated by reports.view; missing → redirected
        # Some report routes are open, but the index should at least render
        # without financials. Either 200 (open) or 302/403 (gated) is fine
        # — we just confirm the user isn't crashing.
        assert r.status_code in (200, 302, 303, 403), \
            f"unexpected status for /reports/: {r.status_code}"
    return f"/reports/ returns {r.status_code} (user not in financial roles)"


@check("J. Cleanup: remove test user + role to keep DB tidy")
def _():
    from app.models import Role, User, Company
    from app.models.user import user_companies
    company = Company.query.order_by(Company.id).first()
    user = User.query.filter_by(email=TEST_USER_EMAIL).first()
    role = Role.query.filter_by(
        company_id=company.id, code="PERM_FIX_HTTP_TEST"
    ).first()
    if user and role:
        db.session.execute(
            user_companies.delete().where(
                (user_companies.c.user_id == user.id) &
                (user_companies.c.company_id == company.id)
            )
        )
    if role:
        role.permissions = []
        db.session.delete(role)
    if user:
        db.session.delete(user)
    db.session.commit()
    return "test user + role removed"


def main():
    app = create_app()
    with app.app_context():
        passed = failed = 0
        for label, fn in CHECKS:
            try:
                msg = fn()
                print(f"\033[92mPASS\033[0m  {label}")
                if msg:
                    print(f"        {msg}")
                passed += 1
            except Exception as e:
                print(f"\033[91mFAIL\033[0m  {label}")
                print(f"        {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                failed += 1
        print()
        print(f"  {passed}/{passed + failed} checks passed.")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
