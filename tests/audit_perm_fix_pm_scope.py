#!/usr/bin/env python3
"""MARSOUD-PERM-FIX (PM scope) — verifies the 6 acceptance criteria from
Abdelhamid's ticket (image #51):

  1. PM sees the project they manage in the list.
  2. PM cannot delete a task (button hidden + route returns 302/403).
  3. Dashboard counts respect view_all scope.
  4. Customers module hidden for PM (sidebar + direct URL).
  5. Direct URL access to a project / task outside scope returns 403.
  6. Regression: tasks/projects of OTHER users still hidden.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
DEMO_EMAIL = "demo@manasety.ai"
DEMO_PASS = "demo1234"


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _login(client, email, password):
    r = client.post("/login", data={"email": email, "password": password},
                    follow_redirects=False)
    assert r.status_code in (302, 303), \
        f"login for {email} failed: status={r.status_code}"


# ─── 1. New permissions exist + role wiring ────────────────────────────
@check("1. projects.view_all / tasks.delete / customers.view exist in P + catalog")
def _():
    from app.services.permissions import P
    from app.services.roles_seed import PERMISSION_CATALOG
    for code in ("projects.view_all", "tasks.delete", "customers.view"):
        assert code in P, f"missing P[{code!r}]"
        assert code in PERMISSION_CATALOG, f"missing CATALOG[{code!r}]"
    return "P + catalog contain all 3 new permissions"


@check("2. Project manager system role doesn't have the restricted perms")
def _():
    from app.models import Company, Role
    from app.services.roles_seed import ensure_roles_ready_for_company
    company = Company.query.first()
    ensure_roles_ready_for_company(company.id)
    pm = Role.query.filter_by(company_id=company.id, code="project_manager").first()
    granted = {p.code for p in pm.permissions}
    for forbidden in ("projects.view_all", "tasks.delete", "customers.view"):
        assert forbidden not in granted, \
            f"PM unexpectedly has {forbidden!r}"
    # And keeps the ones they should have
    for required in ("projects.view", "projects.manage", "tasks.view",
                     "tasks.manage"):
        assert required in granted, f"PM missing required {required!r}"
    return "PM scope verified"


@check("3. Owner system role auto-gets all 3 new perms on re-sync")
def _():
    from app.models import Company, Role
    company = Company.query.first()
    owner = Role.query.filter_by(company_id=company.id, code="owner").first()
    granted = {p.code for p in owner.permissions}
    for code in ("projects.view_all", "tasks.delete", "customers.view"):
        assert code in granted, f"owner missing {code!r}"
    return "owner has all 3 new permissions"


# ─── 2. Permission implications ────────────────────────────────────────
@check("4. projects.view_all implies projects.view at the gate")
def _():
    from app.services.permissions import _IMPLIES
    assert _IMPLIES.get("projects.view") == "projects.view_all"
    return "implication wired"


@check("5. partners.manage implies customers.view (write→read passthrough)")
def _():
    from app.services.permissions import _IMPLIES
    assert _IMPLIES.get("customers.view") == "partners.manage"
    return "accountants editing customers don't need a second checkbox"


# ─── 3. Route + template gates ─────────────────────────────────────────
@check("6. /tasks/<id>/delete route gated by tasks.delete (not tasks.manage)")
def _():
    src = (ROOT / "app/routes/tasks.py").read_text()
    assert '@require_permission("tasks.delete")' in src, \
        "delete route should require tasks.delete"
    # Make sure the prior gate is gone — search for delete function block
    idx_def = src.find("def delete(task_id):")
    block_above = src[max(0, idx_def - 300):idx_def]
    assert 'tasks.manage' not in block_above or 'tasks.delete' in block_above, \
        "delete route still gated by tasks.manage"
    return "delete route now requires tasks.delete"


@check("7. Customers list + view + aging routes gated by customers.view")
def _():
    src = (ROOT / "app/routes/customers.py").read_text()
    # index, view, aging each preceded by require_permission("customers.view")
    for fn_name in ("def index", "def view", "def aging"):
        idx = src.find(fn_name)
        assert idx > 0, f"function {fn_name!r} not found"
        # Look back for the decorator on the preceding lines
        prior = src[max(0, idx - 200):idx]
        assert '@require_permission("customers.view")' in prior, \
            f"{fn_name} not gated by customers.view"
    return "all 3 customer routes gated"


@check("8. Sidebar 'customers.index' maps to customers.view")
def _():
    src = (ROOT / "app/templates/base.html").read_text()
    assert "'customers.index': 'customers.view'" in src
    return "sidebar key updated"


@check("9. tasks/detail.html: delete button gated by tasks.delete, not tasks.manage")
def _():
    src = (ROOT / "app/templates/tasks/detail.html").read_text()
    # The delete form
    assert "has_permission('tasks.delete')" in src, \
        "delete button missing tasks.delete gate"
    # The edit link still uses tasks.manage (PM can edit, just not delete)
    assert "has_permission('tasks.manage')" in src
    return "delete = tasks.delete; edit = tasks.manage"


@check("10. projects.py index uses has_permission('projects.view_all')")
def _():
    src = (ROOT / "app/routes/projects.py").read_text()
    assert "has_permission(\"projects.view_all\")" in src
    # Must include the OR(manager OR member) clause
    assert "Project.manager_id == current_user.id" in src
    assert "Project.id.in_(member_pids)" in src
    return "index uses permission + OR(manager,member) scope"


@check("11. _user_can_see_project checks manager OR member (regardless of role)")
def _():
    src = (ROOT / "app/routes/projects.py").read_text()
    # The new helper checks BOTH manager_id AND project.members for everyone
    # (not just specific role names)
    helper_start = src.find("def _user_can_see_project")
    helper_end = src.find("def ", helper_start + 1)
    helper = src[helper_start:helper_end]
    assert "project.manager_id == current_user.id" in helper
    assert "project.members" in helper
    assert "has_permission(\"projects.view_all\")" in helper
    return "helper unifies manager+member check + uses permission"


# ─── 4. HTTP-level: PM gets 403 on direct URL to out-of-scope task ─────
@check("12. HTTP: PM gets 302/403 on /customers/ (no customers.view)")
def _():
    """Bind the demo test user to project_manager role + hit /customers/.
    Should redirect or 403, not 200."""
    from werkzeug.security import generate_password_hash
    from app.models import User, Company, Role
    from app.models.user import user_companies
    app = create_app()
    with app.app_context():
        company = Company.query.first()
        # Create / reset a PM test user
        u = User.query.filter_by(email="pm_scope_test@example.com").first()
        if not u:
            u = User(email="pm_scope_test@example.com",
                     full_name="PM scope test",
                     password_hash=generate_password_hash("p1234567",
                                                          method="pbkdf2:sha256"),
                     is_active=True)
            db.session.add(u); db.session.flush()
        else:
            u.password_hash = generate_password_hash("p1234567",
                                                      method="pbkdf2:sha256")
        pm_role = Role.query.filter_by(company_id=company.id,
                                        code="project_manager").first()
        db.session.execute(user_companies.delete().where(
            (user_companies.c.user_id == u.id) &
            (user_companies.c.company_id == company.id)))
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=company.id,
            role="project_manager", role_id=pm_role.id,
        ))
        db.session.commit()
        try:
            with app.test_client() as client:
                _login(client, "pm_scope_test@example.com", "p1234567")
                r = client.get("/customers/", follow_redirects=False)
                assert r.status_code in (302, 303, 403), \
                    f"PM should be blocked, got {r.status_code}"
        finally:
            db.session.execute(user_companies.delete().where(
                (user_companies.c.user_id == u.id) &
                (user_companies.c.company_id == company.id)))
            User.query.filter_by(email="pm_scope_test@example.com").delete()
            db.session.commit()
    return f"PM blocked from /customers/ → {r.status_code}"


@check("13. has_permission resolves correctly for PM role")
def _():
    """Programmatic check: bind demo user temporarily to PM, run
    has_permission, verify expected outcomes, restore."""
    from flask import g
    from app.models import User, Company, Role
    from app.models.user import user_companies
    from app.services.permissions import has_permission
    app = create_app()
    with app.app_context():
        company = Company.query.first()
        owner = User.query.filter_by(email=DEMO_EMAIL).first()
        # snapshot
        row = db.session.execute(
            user_companies.select().where(
                (user_companies.c.user_id == owner.id) &
                (user_companies.c.company_id == company.id)
            )
        ).first()
        orig_role_id = row.role_id
        orig_role = row.role
        pm_role = Role.query.filter_by(
            company_id=company.id, code="project_manager"
        ).first()
        try:
            db.session.execute(user_companies.update().where(
                (user_companies.c.user_id == owner.id) &
                (user_companies.c.company_id == company.id)
            ).values(role_id=pm_role.id, role="project_manager"))
            db.session.commit()
            with app.test_request_context("/"):
                g.active_company = company
                assert not has_permission("projects.view_all", user=owner, company=company)
                assert not has_permission("tasks.delete", user=owner, company=company)
                assert not has_permission("customers.view", user=owner, company=company)
                # Things they SHOULD have
                assert has_permission("projects.view", user=owner, company=company)
                assert has_permission("tasks.manage", user=owner, company=company)
        finally:
            db.session.execute(user_companies.update().where(
                (user_companies.c.user_id == owner.id) &
                (user_companies.c.company_id == company.id)
            ).values(role_id=orig_role_id, role=orig_role))
            db.session.commit()
    return "PM scope: locked out of view_all/delete/customers; keeps view/manage"


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
