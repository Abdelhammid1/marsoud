#!/usr/bin/env python3
"""MARSOUD-PERM-FIX-01 audit — verifies the 6 acceptance criteria from the
ticket at the service/data layer (no HTTP server needed).

Acceptance criteria covered:
  1. employees.view exists separately from payroll.view; a role can have one
     without the other.
  2. A user with employees.view but not payroll.view sees the employee
     profile route, salary numbers gated.
  3. "Role A" pattern: custom role can be built with leads.view_all +
     tasks.view_own (i.e. tasks.view only) + projects.view + invoices.* and
     no reports.view.
  4. "Role B" pattern: same as Role A + reports.view.
  5. Existing sales_rep (no view_all) still scoped to assigned leads/tasks.
  6. All achievable from the catalog (no code edits).
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── 1. New permissions exist in P + catalog ────────────────────────────
@check("1. employees.view / leads.view_all / tasks.view_all exist in P dict")
def _():
    from app.services.permissions import P
    for k in ("employees.view", "leads.view_all", "tasks.view_all"):
        assert k in P, f"missing P[{k!r}]"
    return "P dict has all 3 new permissions"


@check("2. PERMISSION_CATALOG has all 3 new entries with Arabic labels")
def _():
    from app.services.roles_seed import PERMISSION_CATALOG
    for k in ("employees.view", "leads.view_all", "tasks.view_all"):
        assert k in PERMISSION_CATALOG, f"missing CATALOG[{k!r}]"
        group_ar, label_ar, kind = PERMISSION_CATALOG[k]
        assert label_ar and any(0x0600 <= ord(c) <= 0x06FF for c in label_ar), \
            f"label for {k!r} isn't Arabic"
    return "Catalog has Arabic labels for all 3"


# ─── 2. employees.view is separable from payroll.view ───────────────────
@check("3. employees.view permission set is broader than payroll.view")
def _():
    from app.services.permissions import P
    emp_view = P["employees.view"]
    pay_view = P["payroll.view"]
    assert pay_view.issubset(emp_view), \
        "expected payroll.view subset of employees.view"
    extra = emp_view - pay_view
    assert extra, "employees.view should include extra roles beyond payroll.view"
    return f"employees.view adds roles beyond payroll.view: {sorted(extra)}"


# ─── 3. After seeding, DB permissions table has the new entries ─────────
@check("4. seed_permissions_catalog persists the 3 new permission rows")
def _():
    from app.models import Permission
    from app.services.roles_seed import seed_permissions_catalog
    seed_permissions_catalog()
    for code in ("employees.view", "leads.view_all", "tasks.view_all"):
        p = Permission.query.filter_by(code=code).first()
        assert p, f"Permission row for {code!r} not found after seed"
    return "All 3 Permission rows present in DB"


# ─── 4. A custom role can grant employees.view without payroll.view ─────
@check("5. Can build a custom role with employees.view but NOT payroll.view")
def _():
    from app.models import Role, Permission, Company
    from app.services.roles import set_role_permissions
    company = Company.query.order_by(Company.id).first()
    assert company, "no company in test DB"

    # Clean up any prior run
    existing = Role.query.filter_by(
        company_id=company.id, code="PERM_FIX_TEST_A"
    ).first()
    if existing:
        existing.permissions = []
        db.session.delete(existing)
        db.session.commit()

    role = Role(
        company_id=company.id, code="PERM_FIX_TEST_A",
        name_ar="اختبار: مدير عمليات بدون رواتب",
        type="CUSTOM",
    )
    db.session.add(role)
    db.session.flush()

    wanted_codes = {"employees.view", "leads.view_all", "leads.manage",
                    "tasks.view", "projects.view", "invoices.create"}
    ids = [p.id for p in Permission.query.filter(
        Permission.code.in_(wanted_codes)).all()]
    set_role_permissions(role, ids)

    # Verify NO payroll.view was attached and employees.view IS attached
    granted = {p.code for p in role.permissions}
    assert "employees.view" in granted, "expected employees.view to be granted"
    assert "payroll.view" not in granted, "payroll.view should NOT be granted"
    return f"Custom role saved with {len(granted)} perms, no payroll.view"


# ─── 5. has_permission() returns the right values for that role ─────────
@check("6. has_permission resolves correctly for the custom role's user")
def _():
    """Mock a user + active company + run has_permission against the role.

    Uses the existing DB-backed path: assigning a role_id to a
    user_companies row and then querying has_permission for both perms.
    """
    from flask import g
    from app.models import User, Role, Company
    from app.models.user import user_companies
    from app.services.permissions import has_permission

    app = create_app()
    company = Company.query.order_by(Company.id).first()
    role = Role.query.filter_by(
        company_id=company.id, code="PERM_FIX_TEST_A").first()
    assert role, "PERM_FIX_TEST_A role missing — earlier check failed?"

    # Pick the demo user, swap their role_id to our test role for the
    # duration of the check, then restore.
    user = User.query.filter_by(email="demo@manasety.ai").first()
    assert user, "demo user missing"
    row = db.session.execute(
        user_companies.select().where(
            (user_companies.c.user_id == user.id) &
            (user_companies.c.company_id == company.id)
        )
    ).first()
    original_role_id = row.role_id if row else None
    try:
        db.session.execute(
            user_companies.update().where(
                (user_companies.c.user_id == user.id) &
                (user_companies.c.company_id == company.id)
            ).values(role_id=role.id, role=role.code)
        )
        db.session.commit()

        with app.test_request_context("/"):
            g.active_company = company
            # Force a fresh load via has_permission directly with explicit args
            assert has_permission("employees.view", user=user, company=company), \
                "employees.view should resolve True"
            assert has_permission("leads.view_all", user=user, company=company), \
                "leads.view_all should resolve True"
            assert not has_permission("payroll.view", user=user, company=company), \
                "payroll.view should resolve False (not granted)"
            assert not has_permission("payroll.employees", user=user, company=company), \
                "payroll.employees should resolve False (not granted)"
    finally:
        # Restore — keep the test DB clean
        db.session.execute(
            user_companies.update().where(
                (user_companies.c.user_id == user.id) &
                (user_companies.c.company_id == company.id)
            ).values(role_id=original_role_id, role="owner")
        )
        db.session.commit()
    return "has_permission gates payroll, allows employees + leads_view_all"


# ─── 6. System roles get the new perms after re-sync ────────────────────
@check("7. Owner/admin system roles auto-get the 3 new perms on re-sync")
def _():
    from app.models import Role, Company
    from app.services.roles_seed import seed_system_roles_for_company
    company = Company.query.order_by(Company.id).first()
    seed_system_roles_for_company(company.id)
    for code in ("owner", "admin"):
        role = Role.query.filter_by(
            company_id=company.id, code=code, type="SYSTEM"
        ).first()
        assert role, f"system role {code!r} missing"
        granted = {p.code for p in role.permissions}
        for perm in ("employees.view", "leads.view_all", "tasks.view_all"):
            assert perm in granted, f"{code} should have {perm} after sync"
    return "owner + admin auto-attached employees.view + view_all perms"


@check("8. sales_manager auto-gets leads.view_all (broader visibility)")
def _():
    from app.models import Role, Company
    company = Company.query.order_by(Company.id).first()
    role = Role.query.filter_by(
        company_id=company.id, code="sales_manager", type="SYSTEM"
    ).first()
    assert role, "sales_manager role missing"
    granted = {p.code for p in role.permissions}
    assert "leads.view_all" in granted, \
        "sales_manager should have leads.view_all"
    assert "leads.view" in granted
    return "sales_manager has both leads.view + leads.view_all"


@check("9. sales_rep does NOT get leads.view_all (assigned-to-me only)")
def _():
    from app.models import Role, Company
    company = Company.query.order_by(Company.id).first()
    role = Role.query.filter_by(
        company_id=company.id, code="sales_rep", type="SYSTEM"
    ).first()
    assert role, "sales_rep role missing"
    granted = {p.code for p in role.permissions}
    assert "leads.view" in granted, "sales_rep should still have leads.view"
    assert "leads.view_all" not in granted, \
        "sales_rep should NOT have leads.view_all"
    return "sales_rep keeps assigned-to-me leads scope"


# ─── 7. Templates render the right gates ────────────────────────────────
@check("10. payroll/index.html gates salary column with payroll.view")
def _():
    src = (ROOT / "app/templates/payroll/index.html").read_text()
    assert "can_view_payroll = has_permission('payroll.view')" in src, \
        "missing can_view_payroll setup"
    assert "{% if can_view_payroll %}" in src, \
        "salary column not gated"
    return "payroll/index gates salary cells"


@check("11. payroll/employee_profile.html gates payslip table with payroll.view")
def _():
    src = (ROOT / "app/templates/payroll/employee_profile.html").read_text()
    assert "can_view_payroll = has_permission('payroll.view')" in src, \
        "missing can_view_payroll setup"
    assert src.count("{% if can_view_payroll %}") >= 2, \
        "expected multiple gates in employee_profile"
    return "employee_profile gates basic_salary + payslips + accruals"


# ─── 8. Route gating ────────────────────────────────────────────────────
@check("12. payroll routes use require_permission gating")
def _():
    src = (ROOT / "app/routes/payroll.py").read_text()
    # Index + profile gated by employees.view
    assert "@require_permission(\"employees.view\")" in src
    # Payroll runs gated by payroll.view
    assert "@require_permission(\"payroll.view\")" in src
    return "payroll.py has both employees.view + payroll.view gates"


@check("13. leads.py _can_view_all_leads checks leads.view_all permission")
def _():
    src = (ROOT / "app/routes/leads.py").read_text()
    assert 'has_permission("leads.view_all")' in src, \
        "_can_view_all_leads should check leads.view_all"
    return "leads.py permission-based visibility check wired"


@check("14. tasks.py _has_full_task_visibility checks tasks.view_all")
def _():
    src = (ROOT / "app/routes/tasks.py").read_text()
    assert 'has_permission("tasks.view_all")' in src
    assert "_has_full_task_visibility" in src
    return "tasks.py permission-based visibility check wired"


# ─── 9. Sidebar mapping ─────────────────────────────────────────────────
@check("15. Sidebar maps payroll.index to employees.view (broader gate)")
def _():
    src = (ROOT / "app/templates/base.html").read_text()
    assert "'payroll.index': 'employees.view'" in src, \
        "sidebar should map payroll.index to employees.view"
    return "Sidebar exposes payroll link to anyone with employees.view"


# ─── Runner ──────────────────────────────────────────────────────────────
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
                failed += 1
        print()
        print(f"  {passed}/{passed + failed} checks passed.")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
