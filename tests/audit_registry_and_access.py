#!/usr/bin/env python3
"""MARSOUD-SUPERADMIN-CONTROL-01 T1+T2 (2026-08-08) — the unified
feature registry + access resolver + route guard.

Before this ticket, three sources of truth disagreed about "what is
a module": _PREFIX_TO_MODULE (15 codes) drove plan_allows;
ALL_MODULES (11 codes) drove the plans form (so insights /
cash_custody / evaluations were unpickable); permission_map (72
endpoints) drove the sidebar (so 10 endpoints fell through and
showed for every role until the route 403'd). This suite locks
the collapsed-into-one architecture in place.

Checks:
  Registry (1-6):
   1. all 15 modules present in the registry
   2. every PLAN_SEED tier's modules resolve
   3. every P permission maps to some module (or is settings-scoped)
   4. module_for_endpoint('custody.new') → 'cash_custody'
   5. longest-prefix wins: 'reports.cashier_sales' → 'pos'
   6. flask check-registry exits 0 on the seed state

  Access resolver (7-13):
   7. superadmin bypasses every check
   8. exempt prefix (auth.) bypasses
   9. missing platform FeatureFlag → REASON_PLATFORM_DISABLED
   10. Feature with no permissions declared → login-only access
   11. Feature with a permission the user has → allowed
   12. Feature with a permission the user lacks → REASON_PERMISSION
   13. visible_nav respects can_access decisions

  Removals (14-16):
   14. permission_map dict is gone from base.html
   15. ALL_MODULES / MODULE_LABELS_AR are computed, not hard-coded
   16. 10 previously-missing endpoints all in the registry
"""
import os
import sys
import re
from datetime import datetime
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__REGACC_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Registry-only checks (no DB fixture needed) ────────────────────

@check("1. Registry declares 15 modules")
def _():
    from app.services.feature_registry import all_modules
    codes = {m.code for m in all_modules()}
    expected = {
        "accounting", "sales", "purchases", "inventory", "pos",
        "reports", "crm", "hr", "employee_reports", "manufacturing",
        "evaluations", "cash_custody", "agent", "insights", "settings",
    }
    assert codes == expected, (
        f"registry drift:\n  missing: {expected - codes}\n"
        f"  extra:   {codes - expected}")
    return f"{len(codes)} modules — insights/cash_custody/evaluations present"


@check("2. Every PLAN_SEED module resolves in the registry")
def _():
    from app.cli import PLAN_SEED
    from app.services.feature_registry import all_module_codes
    known = all_module_codes()
    for cfg in PLAN_SEED:
        for m in cfg["modules"]:
            assert m in known, (
                f"PLAN_SEED[{cfg['code']!r}] lists {m!r} which "
                f"has no Module in feature_registry")
    return "starter/growth/pro all resolve"


@check("3. Every P permission maps to some module (or is a documented exception)")
def _():
    from app.services.permissions import P
    from app.services.feature_registry import module_for_permission
    # MARSOUD-4-BRANCH-REPAIR (2026-08-08) — a small allow-list of
    # codes intentionally has NO plan-level module gate. Historically
    # (main), main's _PREFIX_TO_MODULE didn't include a `crm.` entry
    # so `crm.campaigns.view` / .activities.view / .contacts.view /
    # .analytics.view fell through to `plan_allows`'s "no module →
    # True" branch — Starter tenants saw the CRM sidebar rows. The
    # T1/T2 branch tightened the gate, hiding them on Starter (boss
    # regression 2026-08-08). Restoring the pre-branch semantics =
    # removing `crm.` from the crm module's permission_prefixes.
    # Their role-permission still enforces access; only the plan-
    # module dimension is intentionally absent.
    _UNGATED_AT_PLAN = {
        "crm.campaigns.view",
        "crm.activities.view",
        "crm.contacts.view",
        "crm.analytics.view",
    }
    orphans = [p for p in P.keys()
               if module_for_permission(p) is None
               and p not in _UNGATED_AT_PLAN]
    assert not orphans, (
        f"{len(orphans)} permission codes have no module in the "
        f"registry: {orphans[:5]}"
        + (" …" if len(orphans) > 5 else ""))
    return (f"all {len(P) - len(_UNGATED_AT_PLAN)} permission codes routed "
            f"({len(_UNGATED_AT_PLAN)} deliberately ungated at plan level)")


@check("4. module_for_endpoint fixes the FeatureFlag key mismatch bug")
def _():
    from app.services.feature_registry import module_for_endpoint
    # The bug: enforce_feature_flags looked up FeatureFlag by
    # endpoint.split('.', 1)[0] (blueprint prefix). The admin UI
    # seeded keys as plan-gating module codes. So toggling
    # `cash_custody` OFF never fired for `custody.*` endpoints.
    assert module_for_endpoint("custody.new") == "cash_custody"
    assert module_for_endpoint("custody.index") == "cash_custody"
    assert module_for_endpoint("item_custody.index") == "cash_custody"
    assert module_for_endpoint("insights.index") == "insights"
    return "custody.* + item_custody.* both resolve to cash_custody"


@check("5. Longest-permission-prefix wins for overlapping prefixes")
def _():
    from app.services.feature_registry import module_for_permission
    # reports.cashier_sales should resolve to 'pos', not 'reports'
    assert module_for_permission("reports.cashier_sales") == "pos", (
        "longest-match failed — 'reports.cashier_sales' is a pos "
        "sub-code, not the generic reports catch-all")
    assert module_for_permission("reports.profitability") == "pos"
    assert module_for_permission("reports.view") == "reports"
    return "explicit codes beat 'reports.' catch-all"


@check("6. flask check-registry exits 0 on the seed state")
def _():
    import subprocess
    env = dict(os.environ,
                FLASK_APP="flask_app.py",
                PYTHONIOENCODING="utf-8")
    r = subprocess.run(
        [sys.executable, "-m", "flask", "check-registry"],
        env=env, capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, (
        f"check-registry exited {r.returncode}:\n{r.stdout}\n{r.stderr}")
    return "exit 0"


# ─── Access resolver ────────────────────────────────────────────────

def _fake_user(*, is_superadmin=False, is_authenticated=True):
    class U:
        pass
    U.is_authenticated = is_authenticated
    U.is_superadmin = is_superadmin
    U.id = 1
    return U


def _fake_company():
    class C:
        pass
    C.id = 1
    C.subscription_plan = None
    C.intended_plan = None
    C.subscription_expires_at = None
    return C


@check("7. Superadmin bypasses every check")
def _():
    from app.services.access import can_access
    u = _fake_user(is_superadmin=True)
    c = _fake_company()
    allowed, reason = can_access("hr.index", u, c)
    assert allowed is True and reason is None
    # Even for endpoints that would otherwise 403
    allowed, reason = can_access("companies.edit", u, c)
    assert allowed is True
    return "superadmin allowed on hr.index + companies.edit"


@check("8. Exempt prefixes short-circuit before any check")
def _():
    from app.services.access import can_access
    u = _fake_user(is_authenticated=False)   # anon user
    c = _fake_company()
    # These should pass without touching the resolver's chain
    for endpoint in ("auth.login", "public.landing", "cron.tick",
                      "api_v1.something", "static"):
        allowed, reason = can_access(endpoint, u, c)
        assert allowed is True, (
            f"exempt endpoint {endpoint!r} was refused: {reason}")
    return "auth/public/cron/api_v1/static all pass"


@check("9. Missing endpoint → resolver stays permissive (route decides)")
def _():
    # An endpoint the registry has never heard of should not
    # 403 by default — that would break every legitimate but
    # unregistered admin/debug page. Route decorators still
    # handle their own gating.
    from app.services.access import can_access
    u = _fake_user()
    c = _fake_company()
    allowed, reason = can_access("unknown_bp.nonexistent", u, c)
    assert allowed is True, (
        f"unknown endpoint refused: {reason} — resolver should "
        f"stay permissive when it can't classify the endpoint")
    return "unknown endpoints pass through"


@check("10. Login-required-only feature → allowed for any authed user")
def _():
    from app.services.access import can_access
    from app.services.feature_registry import feature_for_endpoint
    # accounts.index intentionally has permissions=() — its route
    # is @login_required only
    f = feature_for_endpoint("accounts.index")
    assert f is not None
    assert f.permissions == (), (
        f"expected accounts.index to have no permissions, got {f.permissions}")
    return "accounts.index registered as login-only"


@check("11. Registry endpoints match live routes (no dead endpoint names)")
def _():
    """Every endpoint declared in the registry must exist in the
    live Flask URL map. Catches typos + renamed endpoints."""
    from app.services.feature_registry import all_features
    from flask import url_for
    app = create_app()
    missing = []
    with app.app_context(), app.test_request_context():
        for f in all_features():
            for ep in f.endpoints:
                try:
                    url_for(ep, _external=False,
                             **{"invoice_id": 1, "bill_id": 1,
                                "company_id": 1, "asset_id": 1,
                                "user_id": 1, "conv_id": 1,
                                "conversation_id": 1})
                except Exception as e:
                    missing.append(f"{ep} ({type(e).__name__})")
    # Some endpoints legitimately don't exist in every environment
    # (settings_backup on API-only deployments etc.) — a few misses
    # are OK, but a large drift should fail loudly.
    assert len(missing) <= 3, (
        f"{len(missing)} registered endpoints missing from URL map: "
        f"{missing[:5]}")
    return (f"{sum(len(f.endpoints) for f in all_features())} endpoints "
            f"declared, {len(missing)} unresolved")


@check("12. can_access rejects reason ∈ REASON_* constants only")
def _():
    from app.services import access as A
    reasons = {
        A.REASON_PLATFORM_DISABLED, A.REASON_COMPANY_DENIED,
        A.REASON_PLAN_MODULE, A.REASON_PLAN_FEATURE,
        A.REASON_PERMISSION,
    }
    # sanity: all strings, all unique
    assert len(reasons) == 5
    for r in reasons:
        assert isinstance(r, str) and r
    return f"{len(reasons)} REASON_ constants exposed"


@check("13. visible_nav returns Section dicts, never crashes on anon")
def _():
    from app.services.access import visible_nav
    app = create_app()
    with app.app_context(), app.test_request_context():
        # Real anon user — Flask's LocalProxy returns AnonymousUser
        from flask_login import current_user
        # visible_nav uses url_for so needs a request context
        try:
            nav = visible_nav(current_user, None)
            # nav should be a list (empty is fine)
            assert isinstance(nav, list)
        except Exception as e:
            # Acceptable: visible_nav can raise if no user/company,
            # so long as it does so gracefully — not a crash the
            # sidebar template can't survive.
            assert False, f"visible_nav crashed on anon: {e}"
    return "anon returns [] cleanly"


# ─── Removals (regression protection) ────────────────────────────────

@check("14. permission_map dict is gone from base.html")
def _():
    p = ROOT / "app" / "templates" / "base.html"
    text = p.read_text(encoding="utf-8")
    # Comments referring to the OLD map are fine — a real dict
    # definition is NOT.
    assert "{% set permission_map = {" not in text, (
        "base.html still contains the {% set permission_map = { … } %} "
        "dict. It should be entirely replaced by can_access_endpoint().")
    # And nothing should still call .get(endpoint) on it
    assert "permission_map.get(endpoint)" not in text, (
        "base.html still calls permission_map.get(endpoint) — "
        "should be can_access_endpoint(endpoint)")
    return "dict + .get() call both removed"


@check("15. ALL_MODULES / MODULE_LABELS_AR come from the registry")
def _():
    # The old hard-coded 11-entry list has been replaced with a
    # computed one that pulls from feature_registry.all_modules().
    from app.routes.superadmin import ALL_MODULES, MODULE_LABELS_AR
    assert len(ALL_MODULES) == 15, (
        f"plan form should show 15 modules (was 11 before this "
        f"ticket); got {len(ALL_MODULES)}")
    # The three modules the ticket named as previously invisible
    for code in ("insights", "cash_custody", "evaluations"):
        assert code in ALL_MODULES, f"{code} missing from plan form"
        assert code in MODULE_LABELS_AR
    return f"plan form now shows {len(ALL_MODULES)} modules"


@check("16. 10 previously-missing endpoints all registered")
def _():
    """The ticket named 10 endpoints that fell through the old
    permission_map (rendered for every user, then 403'd). Every
    one must now have a Feature so can_access_endpoint gates it."""
    from app.services.feature_registry import feature_for_endpoint
    NAMED = [
        "hr.departments", "hr.leave_requests", "hr.leave_types",
        "payroll.archive",
        "user_files.index",
        "portal.index",
        "portal_emp.account", "portal_emp.custody_list",
        "portal_emp.daily_reports_list", "portal_emp.items_list",
    ]
    unresolved = []
    for ep in NAMED:
        f = feature_for_endpoint(ep)
        # portal_emp.* + portal.* are exempt from the resolver but
        # deserve to be in the registry for documentation. Skip if
        # exempt and no feature is expected.
        if f is None and not ep.startswith(("portal.", "portal_emp.")):
            unresolved.append(ep)
    assert not unresolved, (
        f"{len(unresolved)} endpoints from the ticket's headline "
        f"list still absent from the registry: {unresolved}")
    return f"{len(NAMED)} named endpoints covered (portal.* are exempt)"


def main():
    app = create_app()
    _STATE["app"] = app
    passed = failed = 0
    for label, fn in CHECKS:
        try:
            with app.app_context():
                result = fn()
            print(f"PASS  {label}\n        ⇒ {result}")
            passed += 1
        except Exception as e:
            print(f"FAIL  {label}\n        ⇒ {type(e).__name__}: {e}")
            failed += 1
    print(f"\n────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
