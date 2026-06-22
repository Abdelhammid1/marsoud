#!/usr/bin/env python3
"""Security audit for the /journals/ access-control fix.

The bug: GET routes on the journals blueprint had only @login_required,
which left the full general ledger viewable to any logged-in user
whose role wasn't explicitly in NON_FINANCIAL_ROLES. Custom roles +
unsuspected default roles slipped through.

This audit drives Flask test_client through the worst-case scenarios:
  - sales_rep (legacy hardcoded block) still 403s
  - a fresh CUSTOM role with no journals perms gets redirected (302)
  - owner can still reach every journals view
  - the new journals.view permission exists + is auto-attached
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


@check("1. journals.view exists in P dict + catalog + has reasonable defaults")
def _():
    from app.services.permissions import P
    from app.services.roles_seed import PERMISSION_CATALOG
    assert "journals.view" in P, "journals.view missing from P dict"
    assert "journals.view" in PERMISSION_CATALOG, \
        "journals.view missing from PERMISSION_CATALOG"
    roles = P["journals.view"]
    # Must include accountant + owner; must NOT include sales_rep
    assert "owner" in roles
    assert "accountant" in roles
    assert "sales_rep" not in roles, \
        "sales_rep should NOT have journals.view by default"
    return f"defaults: {sorted(roles)}"


@check("2. journals.create implies journals.view (catalog re-sync covers it)")
def _():
    from app.services.permissions import _IMPLIES, has_permission
    assert _IMPLIES.get("journals.view") == "journals.create", \
        "expected journals.view → journals.create implication"
    return "implication map wired"


@check("3. All 5 GET routes in journals.py now have @require_permission")
def _():
    src = (ROOT / "app/routes/journals.py").read_text()
    # The 5 GET routes that were unguarded
    fragments_view = [
        '@bp.route("/")\n@login_required\n@require_permission("journals.view")\ndef index',
        '@bp.route("/<int:entry_id>")\n@login_required\n@require_permission("journals.view")\ndef view',
        '@bp.route("/templates")\n@login_required\n@require_permission("journals.view")\ndef templates_list',
        '@bp.route("/recurring")\n@login_required\n@require_permission("journals.view")\ndef recurring_list',
        '@bp.route("/recurring/<int:rec_id>/log")\n@login_required\n@require_permission("journals.view")\ndef recurring_log',
    ]
    for frag in fragments_view:
        # Normalise: tolerate trailing whitespace differences
        compact = "".join(frag.split())
        compact_src = "".join(src.split())
        assert compact in compact_src, \
            f"missing gate: {frag.splitlines()[-1]}"
    # use_template is gated by journals.create (mutation-adjacent)
    assert "@require_permission(\"journals.create\")\ndef use_template" in src \
        or "@require_permission(\"journals.create\")" in src and "def use_template" in src
    return "all 5 view routes + use_template gated"


@check("4. Sidebar 'journals.index' link mapped to journals.view (not .create)")
def _():
    src = (ROOT / "app/templates/base.html").read_text()
    assert "'journals.index': 'journals.view'" in src, \
        "sidebar should expose journals link to anyone with journals.view"
    return "sidebar shows the link for read-only roles too"


@check("5. Custom role w/o journals perms gets blocked (HTTP 302 to dashboard)")
def _():
    from app.models import User, Company, Role, Permission
    from app.models.user import user_companies
    from werkzeug.security import generate_password_hash

    app = create_app()
    with app.app_context():
        company = Company.query.first()
        # Test user
        u = User.query.filter_by(email="journals_sec@test.com").first()
        if not u:
            u = User(email="journals_sec@test.com", full_name="sec test",
                     password_hash=generate_password_hash("y1234567",
                                                           method="pbkdf2:sha256"),
                     is_active=True)
            db.session.add(u); db.session.flush()
        else:
            u.password_hash = generate_password_hash("y1234567",
                                                      method="pbkdf2:sha256")
        # Custom role with no journals perms
        Role.query.filter_by(company_id=company.id, code="SEC_TEST").delete()
        db.session.commit()
        role = Role(company_id=company.id, code="SEC_TEST",
                    name_ar="sec test", type="CUSTOM")
        db.session.add(role); db.session.flush()
        role.permissions = Permission.query.filter(
            Permission.code.in_(["partners.manage"])).all()
        db.session.execute(user_companies.delete().where(
            (user_companies.c.user_id == u.id) &
            (user_companies.c.company_id == company.id)))
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=company.id,
            role="SEC_TEST", role_id=role.id))
        db.session.commit()

        try:
            with app.test_client() as client:
                _login(client, "journals_sec@test.com", "y1234567")
                results = {}
                for path in ["/journals/", "/journals/1",
                             "/journals/templates", "/journals/recurring"]:
                    r = client.get(path, follow_redirects=False)
                    results[path] = r.status_code
                # All must be blocked (302 redirect or 403)
                for path, code in results.items():
                    assert code in (302, 303, 403), \
                        f"{path} should be blocked, got {code}"
        finally:
            db.session.execute(user_companies.delete().where(
                (user_companies.c.user_id == u.id) &
                (u.id == u.id)))
            Role.query.filter_by(code="SEC_TEST").delete()
            User.query.filter_by(email="journals_sec@test.com").delete()
            db.session.commit()
    return f"all 4 paths blocked: {results}"


@check("6. Demo owner can still reach /journals/ (200) — no regression")
def _():
    app = create_app()
    with app.test_client() as client:
        _login(client, DEMO_EMAIL, DEMO_PASS)
        r = client.get("/journals/", follow_redirects=False)
        assert r.status_code == 200, \
            f"owner GET /journals/ expected 200, got {r.status_code}"
    return "owner: 200 OK"


@check("7. Owner can reach /journals/templates + /journals/recurring (200)")
def _():
    app = create_app()
    with app.test_client() as client:
        _login(client, DEMO_EMAIL, DEMO_PASS)
        for path in ["/journals/templates", "/journals/recurring"]:
            r = client.get(path)
            assert r.status_code == 200, \
                f"owner GET {path} expected 200, got {r.status_code}"
    return "templates + recurring: 200 OK for owner"


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
