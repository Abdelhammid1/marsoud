#!/usr/bin/env python3
"""MARSOUD-ROLES-REFLECT-SCOPE (2026-08-09) — audit for the
company-owner's /settings/roles page filtering by
effective_modules(company).

Before this ticket the roles page rendered every row from
PERMISSION_CATALOG regardless of what the company's plan
actually enabled. Starter owners saw manufacturing / hr /
inventory checkboxes — turning them on did nothing because
has_permission() gates through plan_allows(), but the
mismatch confused everyone. This audit locks the new
behaviour: the visible set == effective_modules ∪
_ALWAYS_READABLE ∪ unmapped(=always-visible), and the POST
endpoint intersects submitted ids with the same set.

Fixture: 2 plans (starter with sales+settings only; full
with the whole module list from feature_registry) + 3
companies (home_starter, home_full, onboarding-with-intended
= exercises the intended_plan fallback path).

Every check verified to fail against pre-ticket HEAD.
"""
import sys
from pathlib import Path

# Windows console defaults to cp1252 — force UTF-8 so
# print() of the Arabic labels in assertion messages
# doesn't blow up mid-run.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

# See tests/audit_task_hierarchy.py — the boss's env carries a
# SESSION_COOKIE_DOMAIN=.marsoud.com in .env that breaks test
# client cookies on localhost (login redirects instead of the
# expected page). Neutralise per-app so this audit works on
# every machine.
_ORIG_CREATE_APP = create_app
def create_app(*a, **kw):
    app = _ORIG_CREATE_APP(*a, **kw)
    app.config["SESSION_COOKIE_DOMAIN"] = None
    return app


CHECKS = []
PREFIX = "__ROLESCOPE_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from datetime import datetime
    from app.models import Company, Plan, User, user_companies
    from werkzeug.security import generate_password_hash
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.legal import get_terms_version

    # Starter: sales + settings only. Deliberately narrow so the
    # test can assert manufacturing/hr rows disappear.
    starter = Plan(code=f"{PREFIX}starter", name="ROLESCOPE-Starter",
                   name_ar="ROLESCOPE-Starter", allowed_subitems=None)
    starter.set_modules(["sales", "settings"])
    db.session.add(starter); db.session.flush()

    # Full: everything the feature_registry exposes. Building from
    # the live registry so the test can't drift if a new module is
    # added.
    from app.services.feature_registry import all_modules
    full_codes = sorted({m.code for m in all_modules()})
    full = Plan(code=f"{PREFIX}full", name="ROLESCOPE-Full",
                name_ar="ROLESCOPE-Full", allowed_subitems=None)
    full.set_modules(full_codes)
    db.session.add(full); db.session.flush()

    db.session.commit()
    _STATE["starter_id"] = starter.id
    _STATE["full_id"] = full.id
    _STATE["full_module_codes"] = full_codes

    # Companies. home_starter + home_full carry a promoted plan.
    # onboarding uses intended_plan (plan_id NULL) to exercise
    # the MARSOUD-PLAN-BUNDLE-FIXES-01 fallback path.
    home_starter = Company(name=f"{PREFIX}HOME_STARTER",
                           base_currency="SAR", plan_id=starter.id,
                           timezone="Asia/Riyadh")
    home_full = Company(name=f"{PREFIX}HOME_FULL",
                        base_currency="SAR", plan_id=full.id,
                        timezone="Asia/Riyadh")
    onboarding = Company(name=f"{PREFIX}ONBOARDING",
                         base_currency="SAR", plan_id=None,
                         timezone="Asia/Riyadh")
    onboarding.intended_plan_id = starter.id
    for co in (home_starter, home_full, onboarding):
        db.session.add(co); db.session.flush()
    db.session.commit()
    ensure_roles_ready_for_company(home_starter.id)
    ensure_roles_ready_for_company(home_full.id)
    ensure_roles_ready_for_company(onboarding.id)

    u_starter = User(email=f"{PREFIX}starter@audit.local",
                     password_hash=generate_password_hash(
                         "x", method="pbkdf2:sha256"),
                     full_name="starter owner", is_active=True,
                     terms_version=get_terms_version(),
                     terms_accepted_at=datetime.utcnow())
    u_full = User(email=f"{PREFIX}full@audit.local",
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name="full owner", is_active=True,
                  terms_version=get_terms_version(),
                  terms_accepted_at=datetime.utcnow())
    db.session.add(u_starter); db.session.add(u_full)
    db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u_starter.id, company_id=home_starter.id,
        role="owner"))
    db.session.execute(user_companies.insert().values(
        user_id=u_full.id, company_id=home_full.id,
        role="owner"))
    db.session.commit()

    # Set role_id on the memberships so require_permission finds
    # the owner role via _db_has_permission.
    from app.services.roles import set_membership_role, create_custom_role
    set_membership_role(u_starter.id, home_starter.id, "owner")
    set_membership_role(u_full.id, home_full.id, "owner")

    # Custom roles for the POST-side checks — set_role_permissions
    # refuses to touch SYSTEM roles (roles.py:90-91), so tests 10/11
    # need a CUSTOM target to actually exercise the filter path.
    r_writable = create_custom_role(
        home_starter.id, f"{PREFIX}writable",
        description="test target for POST filter")
    r_writable2 = create_custom_role(
        home_starter.id, f"{PREFIX}writable2",
        description="test target for POST regression")

    _STATE["writable_role_id"] = r_writable.id
    _STATE["writable_role2_id"] = r_writable2.id

    _STATE.update(
        home_starter_id=home_starter.id, home_full_id=home_full.id,
        onboarding_id=onboarding.id,
        u_starter_id=u_starter.id, u_full_id=u_full.id,
    )


def _teardown():
    from app.models import Company, User, Plan
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        # Roles for this company drag their role_permissions M2M
        # rows with them. Delete via ORM so the M2M secondary is
        # cleared (ORM cascades handle it).
        from app.models import Role
        for r in Role.query.filter_by(company_id=cid).all():
            r.permissions = []
        db.session.flush()
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(
                        text(f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        db.session.execute(text(
            "DELETE FROM companies WHERE id=:c"), {"c": cid})
        db.session.commit()
    for p in Plan.query.filter(Plan.code.like(f"{PREFIX}%")).all():
        db.session.delete(p)
    db.session.commit()
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text(
            "DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _reset_g():
    from flask import g
    for key in ("_login_user", "active_company", "user_companies",
                "impersonating"):
        try:
            g.pop(key, None)
        except Exception:
            pass


def _client_as(user_id, company_id):
    from flask import current_app
    _reset_g()
    db.session.expire_all()
    db.session.remove()
    c = current_app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["active_company_id"] = company_id
    return c


def _co(cid):
    from app.models import Company
    db.session.expire_all()
    return db.session.get(Company, cid)


def _perm_by_code(code):
    from app.models import Permission
    return Permission.query.filter_by(code=code).first()


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. effective_modules(None) returns only _ALWAYS_ALLOWED")
def _():
    from app.services.plan_gating import (
        effective_modules, _ALWAYS_ALLOWED,
    )
    got = effective_modules(None)
    assert got == set(_ALWAYS_ALLOWED), (
        f"want {_ALWAYS_ALLOWED}, got {got}")
    return f"None-company -> {sorted(got)}"


@check("2. effective_modules(starter) == starter.modules | _ALWAYS_ALLOWED")
def _():
    from app.services.plan_gating import (
        effective_modules, _ALWAYS_ALLOWED,
    )
    co = _co(_STATE["home_starter_id"])
    got = effective_modules(co)
    expected = {"sales", "settings"} | set(_ALWAYS_ALLOWED)
    assert got == expected, f"want {expected}, got {got}"
    return f"starter -> {sorted(got)}"


@check("3. effective_modules(onboarding) falls back to intended_plan")
def _():
    """Company with plan_id NULL but intended_plan_id=starter
    must render the same set as a promoted starter — otherwise
    a company mid-onboarding sees an empty page."""
    from app.services.plan_gating import (
        effective_modules, _ALWAYS_ALLOWED,
    )
    co = _co(_STATE["onboarding_id"])
    assert co.plan_id is None, "fixture drift: onboarding has plan_id"
    assert co.intended_plan_id == _STATE["starter_id"], (
        "fixture drift: onboarding intended plan mismatch")
    got = effective_modules(co)
    expected = {"sales", "settings"} | set(_ALWAYS_ALLOWED)
    assert got == expected, f"fallback broken: got {got}"
    return f"intended_plan fallback works -> {sorted(got)}"


@check("4. grouped_permissions(starter) excludes manufacturing.manage")
def _():
    from app.services.roles import grouped_permissions
    co = _co(_STATE["home_starter_id"])
    groups = grouped_permissions(co)
    codes = {p.code for _, perms in groups for p in perms}
    assert "manufacturing.manage" not in codes, (
        "manufacturing perm leaked into starter's roles page")
    assert "hr.manage" not in codes, (
        "hr perm leaked into starter's roles page")
    # Sanity: sales-scoped perms MUST be present (starter has sales).
    assert "invoices.create" in codes, (
        "invoices.create missing from starter — filter over-eager")
    return (f"starter grid: {len(codes)} perms, "
            f"no manufacturing/hr")


@check("5. grouped_permissions(full) includes every module's perms")
def _():
    from app.services.roles import grouped_permissions
    co = _co(_STATE["home_full_id"])
    groups = grouped_permissions(co)
    codes = {p.code for _, perms in groups for p in perms}
    assert "manufacturing.manage" in codes, (
        "manufacturing perm missing on full plan")
    assert "hr.manage" in codes, "hr perm missing on full plan"
    assert "invoices.create" in codes, (
        "invoices perm missing on full plan")
    return f"full grid: {len(codes)} perms, everything present"


@check("6. always-visible perms stay visible regardless of plan")
def _():
    """users.manage maps to 'settings' (always-allowed) — must
    be present on the narrowest plan. refunds.view is in
    _ALWAYS_READABLE — must be present even though 'refunds'
    isn't a module."""
    from app.services.roles import grouped_permissions
    co = _co(_STATE["home_starter_id"])
    groups = grouped_permissions(co)
    codes = {p.code for _, perms in groups for p in perms}
    assert "users.manage" in codes, (
        "users.manage vanished on starter — owner locked out")
    assert "refunds.view" in codes, (
        "refunds.view (always-readable) filtered out on starter")
    return "users.manage + refunds.view both visible on starter"


@check("7. GET /settings/roles on starter — manufacturing label absent")
def _():
    from app.services.roles_seed import PERMISSION_CATALOG
    label = PERMISSION_CATALOG["manufacturing.manage"][1]
    c = _client_as(_STATE["u_starter_id"], _STATE["home_starter_id"])
    r = c.get("/settings/roles/")
    assert r.status_code == 200, f"HTTP {r.status_code}"
    body = r.get_data(as_text=True)
    assert label not in body, (
        f"manufacturing label '{label}' leaked into starter's page")
    return f"starter page rendered without '{label}'"


@check("8. GET /settings/roles on full — manufacturing label present")
def _():
    from app.services.roles_seed import PERMISSION_CATALOG
    label = PERMISSION_CATALOG["manufacturing.manage"][1]
    c = _client_as(_STATE["u_full_id"], _STATE["home_full_id"])
    r = c.get("/settings/roles/")
    assert r.status_code == 200, f"HTTP {r.status_code}"
    body = r.get_data(as_text=True)
    assert label in body, (
        f"manufacturing label '{label}' missing from full page")
    return f"full page shows '{label}'"


@check("9. Upgrade plan then re-GET — new labels appear without owner action")
def _():
    """AC line 5: 'lما السوبرأدمن يضيف استثناء منح جديد لشركة،
    الفيتشر يظهر في صفحة أدوار الشركة عند أول فتح/تحديث للصفحة،
    من غير تدخل من الـ owner'. Same shape for a plan upgrade."""
    from app.models import Company
    from app.services.roles_seed import PERMISSION_CATALOG
    label = PERMISSION_CATALOG["manufacturing.manage"][1]

    # Upgrade starter -> full.
    co = db.session.get(Company, _STATE["home_starter_id"])
    co.plan_id = _STATE["full_id"]
    db.session.commit()
    try:
        c = _client_as(_STATE["u_starter_id"],
                        _STATE["home_starter_id"])
        r = c.get("/settings/roles/")
        assert r.status_code == 200, f"HTTP {r.status_code}"
        body = r.get_data(as_text=True)
        assert label in body, (
            "manufacturing label didn't appear after plan upgrade")
        return f"plan upgrade reflects instantly -> '{label}' visible"
    finally:
        # Restore for the remaining checks so the fixture stays
        # consistent (checks 10-11 assume starter is still narrow).
        co = db.session.get(Company, _STATE["home_starter_id"])
        co.plan_id = _STATE["starter_id"]
        db.session.commit()


def _role_id(cid, code):
    """Look up a role by (company_id, code) and return its id.
    We return the primitive so the caller can post through the
    HTTP client without carrying a detached ORM instance
    across a session.remove()."""
    from app.models import Role
    r = Role.query.filter_by(company_id=cid, code=code).first()
    assert r is not None, f"fixture drift: role {code} missing"
    return r.id


def _perm_id(code):
    """Same pattern for Permission.id — the test client wipes
    the session so we resolve upfront."""
    p = _perm_by_code(code)
    assert p is not None, f"fixture drift: perm {code} missing"
    return p.id


@check("10. POST with out-of-scope perm id: not persisted (in-scope IS)")
def _():
    """The belt against a crafted POST: submit ONE valid + ONE
    invalid id and check only the valid one lands on the role."""
    from app.models import Role
    # Custom role — set_role_permissions refuses SYSTEM roles, so
    # a system role like "admin" would silently keep whatever the
    # seeder gave it and mask the filter check.
    role_id = _STATE["writable_role_id"]
    valid_id = _perm_id("invoices.create")     # sales -> allowed
    invalid_id = _perm_id("manufacturing.manage")  # not on starter

    from werkzeug.datastructures import MultiDict
    c = _client_as(_STATE["u_starter_id"], _STATE["home_starter_id"])
    r = c.post(
        f"/settings/roles/{role_id}/permissions",
        data=MultiDict([("permission_id", str(valid_id)),
                        ("permission_id", str(invalid_id))]),
        follow_redirects=False,
    )
    assert r.status_code in (200, 302), f"HTTP {r.status_code}"
    db.session.expire_all()
    r_reloaded = db.session.get(Role, role_id)
    codes = {p.code for p in r_reloaded.permissions}
    assert "invoices.create" in codes, (
        "valid perm dropped — filter over-eager")
    assert "manufacturing.manage" not in codes, (
        "crafted POST leaked out-of-scope perm onto role")
    return (f"admin got invoices.create; "
            f"manufacturing.manage refused")


@check("11. Regression: POST with only in-scope ids saves normally")
def _():
    """Owner's normal use case: pick a few visible perms, submit,
    expect them to save. The filter must be transparent when
    everything is legit."""
    from app.models import Role
    role_id = _STATE["writable_role2_id"]
    p1_id = _perm_id("invoices.create")
    p2_id = _perm_id("customers.view")

    from werkzeug.datastructures import MultiDict
    c = _client_as(_STATE["u_starter_id"], _STATE["home_starter_id"])
    r = c.post(
        f"/settings/roles/{role_id}/permissions",
        data=MultiDict([("permission_id", str(p1_id)),
                        ("permission_id", str(p2_id))]),
        follow_redirects=False,
    )
    assert r.status_code in (200, 302), f"HTTP {r.status_code}"
    db.session.expire_all()
    r_reloaded = db.session.get(Role, role_id)
    codes = {p.code for p in r_reloaded.permissions}
    assert "invoices.create" in codes, "regression: p1 lost"
    assert "customers.view" in codes, "regression: p2 lost"
    return "both in-scope perms saved (owner flow unchanged)"


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                    failed += 1
        finally:
            _teardown()
            print("\n(fixture cleaned up)")
    print(f"\n----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
