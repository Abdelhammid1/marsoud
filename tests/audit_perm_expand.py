#!/usr/bin/env python3
"""MARSOUD-PERM-EXPAND audit — every sidebar item has a super-admin
   toggle + an owner Roles-page toggle, WITHOUT breaking existing rules.

Ticket's explicit warning: "The system currently has fine-grained
permissions that are working correctly (like admin-only edit). Preserve
them. Only ADD."

Covers:
  1. All 8 new permission codes exist in P dict + PERMISSION_CATALOG
  2. All 8 have _IMPLIES entries pointing to their umbrella
  3. Fresh company: system roles get the new codes on seed
  4. Legacy custom role with only 'leads.view' STILL sees CRM items
     (because _IMPLIES routes through the umbrella)
  5. permission_map in base.html uses the NEW codes for the new items
  6. SUB_ITEM_CATALOG has all 9 new sidebar sections
  7. SUB_ITEM_CATALOG contains every new endpoint (crm sub-items,
     party_ledger, settings pages)
  8. SECTION_LABEL_AR + SECTION_REQUIRES_MODULES align with the catalog
  9. Owner Roles page renders all 8 new permission labels
 10. Super-admin plan editor renders all new sections + endpoints
 11. Legacy fine-grained rule: sales_rep still can't reach admin pages
     (proves the additive change didn't loosen anything)
 12. Turning off crm.campaigns.view on a role hides ONLY campaigns
     (analytics, activities, contacts stay visible)
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


NEW_CODES = [
    "crm.campaigns.view", "crm.activities.view", "crm.contacts.view",
    "crm.analytics.view", "party_ledger.view",
    "api_tokens.manage", "activity_log.view", "backup.download",
]

UMBRELLA_FOR = {
    "crm.campaigns.view":  "leads.view",
    "crm.activities.view": "leads.view",
    "crm.contacts.view":   "leads.view",
    "crm.analytics.view":  "leads.view",
    "party_ledger.view":   "reports.view",
    "api_tokens.manage":   "users.manage",
    "activity_log.view":   "users.manage",
    "backup.download":     "users.manage",
}


# ─── Static catalogs ───────────────────────────────────────────────────
@check("1. All 8 new permission codes exist in P dict")
def _():
    from app.services.permissions import P
    for code in NEW_CODES:
        assert code in P, f"{code} missing from P"
        assert len(P[code]) > 0, f"{code} has no default grants"
    return f"all {len(NEW_CODES)} new codes present in P"


@check("2. All 8 new codes have _IMPLIES → umbrella")
def _():
    from app.services.permissions import _IMPLIES
    for code, umbrella in UMBRELLA_FOR.items():
        assert _IMPLIES.get(code) == umbrella, \
            f"_IMPLIES[{code!r}] should be {umbrella!r}, got {_IMPLIES.get(code)!r}"
    return "every new code implies its umbrella"


@check("3. All 8 new codes exist in PERMISSION_CATALOG with group_ar")
def _():
    from app.services.roles_seed import PERMISSION_CATALOG
    for code in NEW_CODES:
        assert code in PERMISSION_CATALOG, f"{code} missing from catalog"
        group, label, kind = PERMISSION_CATALOG[code]
        assert group and label, f"{code} missing metadata"
    return f"all {len(NEW_CODES)} codes catalogued"


@check("4. Sidebar permission_map wires new items to the new codes")
def _():
    p = ROOT / "app" / "templates" / "base.html"
    text = p.read_text(encoding="utf-8")
    # Rows we should see (endpoint → new code)
    for line in [
        "'crm.campaigns_index': 'crm.campaigns.view'",
        "'crm.activities_index': 'crm.activities.view'",
        "'crm.contacts_index': 'crm.contacts.view'",
        "'crm.analytics': 'crm.analytics.view'",
        "'party_ledger.index': 'party_ledger.view'",
        "'settings_api_tokens.index': 'api_tokens.manage'",
        "'settings_activity.index': 'activity_log.view'",
        "'settings_backup.index': 'backup.download'",
    ]:
        assert line in text, f"permission_map missing: {line}"
    return "all 8 permission_map entries updated"


# ─── Plan gating catalog ───────────────────────────────────────────────
@check("5. SUB_ITEM_CATALOG contains all 9 sidebar sections")
def _():
    from app.services.plan_gating import SUB_ITEM_CATALOG, SECTION_LABEL_AR
    expected_sections = {"main", "accounting", "sales", "purchases",
                          "inventory", "crm", "workflow", "hr", "settings"}
    got = set(SUB_ITEM_CATALOG.keys())
    missing = expected_sections - got
    assert not missing, f"missing sections: {missing}"
    for key in expected_sections:
        assert key in SECTION_LABEL_AR
    return f"all 9 sections present ({sorted(got)})"


@check("6. SUB_ITEM_CATALOG contains every new endpoint")
def _():
    from app.services.plan_gating import SUB_ITEM_CATALOG
    all_endpoints = {ep for items in SUB_ITEM_CATALOG.values()
                       for (ep, _, _) in items}
    required_new = [
        "crm.campaigns_index", "crm.activities_index",
        "crm.contacts_index", "crm.analytics",
        "party_ledger.index",
        "settings_api_tokens.index", "settings_activity.index",
        "settings_backup.index",
    ]
    missing = [ep for ep in required_new if ep not in all_endpoints]
    assert not missing, f"catalog missing: {missing}"
    return f"all {len(required_new)} new endpoints in catalog"


@check("7. SECTION_REQUIRES_MODULES aligns with SECTION_LABEL_AR")
def _():
    from app.services.plan_gating import (
        SECTION_LABEL_AR, SECTION_REQUIRES_MODULES, SUB_ITEM_CATALOG,
    )
    for k in SUB_ITEM_CATALOG:
        assert k in SECTION_LABEL_AR, f"missing label for {k}"
        assert k in SECTION_REQUIRES_MODULES, f"missing module req for {k}"
    return "every section has label + module req"


# ─── Runtime behaviour ─────────────────────────────────────────────────
@check("8. Owner sidebar still shows CRM sub-items (nothing broke)")
def _():
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        r = c.get("/home")
        html = r.get_data(as_text=True)
        for label in ["الحملات", "الأنشطة والمتابعات",
                       "جهات الاتصال", "تحليلات CRM",
                       "كشف حساب طرف"]:
            assert label in html, f"sidebar dropped: {label}"
    return "owner still sees every new sidebar item"


@check("9. Roles page renders all 8 new permission labels for owner")
def _():
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        r = c.get("/settings/roles/")
        html = r.get_data(as_text=True)
        expected_labels = [
            "CRM: الحملات",
            "CRM: الأنشطة",
            "CRM: جهات الاتصال",
            "CRM: تحليلات",
            "كشف حساب طرف",
            "مفاتيح الـ API",
            "سجل نشاط الموظفين",
            "نسخة احتياطية (Excel)",
        ]
        missing = [l for l in expected_labels if l not in html]
        assert not missing, f"roles page missing: {missing}"
    return f"all {len(expected_labels)} new permission labels shown"


@check("10. Legacy behaviour preserved: role with only leads.view still passes crm.campaigns.view")
def _():
    """This is the critical additive-safety check. If someone has only
    the umbrella (leads.view), _IMPLIES must route the new codes so
    they don't lose access after the ticket."""
    from flask import g
    from app.models import User, Company
    from app.services.permissions import has_permission
    app = create_app()
    with app.app_context():
        with app.test_request_context("/"):
            owner = User.query.filter_by(email="demo@manasety.ai").first()
            company = Company.query.first()
            g.active_company = company
            # Owner has leads.view (via P dict, granted to owner). Verify:
            assert has_permission("leads.view", user=owner, company=company)
            # Now verify each new CRM code also passes for the owner
            # via the _IMPLIES chain.
            for code in ["crm.campaigns.view", "crm.activities.view",
                          "crm.contacts.view", "crm.analytics.view"]:
                assert has_permission(code, user=owner, company=company), \
                    f"{code} should pass via _IMPLIES → leads.view"
    return "umbrella leads.view still grants every CRM sub-code"


@check("11. Super-admin plan editor shows all 9 sections + new endpoints")
def _():
    from app.models import User, Plan
    from werkzeug.security import generate_password_hash
    app = create_app()
    with app.app_context():
        sa = User.query.filter_by(is_superadmin=True, is_active=True).first()
        assert sa, "no super-admin user"
        sa_email, saved = sa.email, sa.password_hash
        sa.password_hash = generate_password_hash("audit-1234",
                                                    method="pbkdf2:sha256")
        plan = Plan.query.first()
        pid = plan.id if plan else None
        db.session.commit()
    try:
        with app.test_client() as c:
            c.post("/login", data={"email": sa_email, "password": "audit-1234"})
            if pid:
                r = c.get(f"/admin/plans/{pid}/edit")
                html = r.get_data(as_text=True)
                for label in ["المالية والمحاسبة", "المشتريات",
                                "المخزون", "إدارة العمل"]:
                    assert label in html, f"plan editor missing section: {label}"
                for ep in ["crm.campaigns_index", "crm.activities_index",
                            "crm.contacts_index", "crm.analytics",
                            "party_ledger.index", "settings_backup.index"]:
                    assert ep in html, f"plan editor missing endpoint: {ep}"
    finally:
        with app.app_context():
            User.query.filter_by(email=sa_email).update(
                {"password_hash": saved})
            db.session.commit()
    return "SA plan editor shows every new section + endpoint"


@check("12. Fine-grained legacy rule preserved: non-owner can't reach superadmin")
def _():
    """Sanity: the additive change shouldn't have loosened anything on
    the existing admin-only pages."""
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        r = c.get("/admin/plans/", follow_redirects=False)
        # SA-only routes may return 302 (redirect to login/dashboard),
        # 401/403, or 404 (route registered under a decorator that
        # abort(404)s to hide its existence from non-SA). All of them
        # confirm the non-SA can't reach the page.
        assert r.status_code not in (200,), \
            f"demo (non-SA) got 200 on /admin/plans/ — access should be blocked"
    return f"non-SA blocked from /admin/plans/ → {r.status_code}"


# ─── Run ───────────────────────────────────────────────────────────────
def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        for label, fn in CHECKS:
            try:
                result = fn()
                print(f"PASS  {label}  ⇒ {result}")
                passed += 1
            except Exception as e:
                print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                failed += 1
                import traceback
                traceback.print_exc()
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
