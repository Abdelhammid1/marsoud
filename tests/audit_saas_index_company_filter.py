#!/usr/bin/env python3
"""MARSOUD-TKT-SAAS-INDEX-COMPANY-FILTER (Abdelhamid 2026-08-31) —
`/admin/saas` supports `?company_id=<id>` so the "فواتير SaaS
مفتوحة → راجع" link from a company's detail page narrows the list
to that single tenant.

Before the fix, that link opened /admin/saas WITHOUT any filter,
so clicking it from Company A showed every tenant's outstanding
invoices — misleading + wasted the user's time.

Checks:
  1. saas_index route accepts a `company_id` query param (route
     signature still resolves).
  2. Without the param — behaves exactly as before (all companies
     with an intended_plan_id are listed).
  3. With a valid `?company_id=X` — only Company X in the rendered
     `companies` list; the active-filter banner names it.
  4. With an invalid `?company_id=99999` — silently ignored (falls
     back to full list, no 404).
  5. With a soft-deleted company id — also silently ignored.
  6. company_detail template — the "راجع" link builds a URL that
     includes `company_id=` matching the current company (regression
     guard against the pre-fix link that had no query param).
  7. saas_index template — active-filter banner renders when
     `filter_company` is present, and has the "عرض كل الشركات"
     clear link back to /admin/saas.
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _strip_comments(src):
    return re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)


def _boot_two_tenants():
    """Create two tenant companies (A + B) that both have an
    intended_plan_id + a superadmin user to log in as."""
    from datetime import datetime
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        "SELECT id FROM companies WHERE name LIKE '__SAAS_CF__%'"))]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        "DELETE FROM users WHERE email LIKE '%__saas_cf__%'"))
    db.session.commit()

    plan = Plan.query.filter_by(code="__SAAS_CF__").first()
    if not plan:
        plan = Plan(code="__SAAS_CF__", name="F", name_ar="F",
                    allowed_subitems=None)
        plan.set_modules(["accounting"])
        db.session.add(plan); db.session.flush()

    def _mk(name, sub):
        c = Company(name=name, base_currency="EGP", subdomain=sub,
                    plan_id=plan.id,
                    intended_plan_id=plan.id,
                    subscription_started_at=datetime.utcnow(),
                    subscription_expires_at=datetime(2999, 1, 1))
        db.session.add(c); db.session.commit()
        seed_default_coa(c.id); db.session.commit()
        return c

    cA = _mk("__SAAS_CF__A", "saascfa")
    cB = _mk("__SAAS_CF__B", "saascfb")

    try:
        from app.services.legal import get_terms_version
        tv = get_terms_version() or "audit"
    except Exception:
        tv = "audit"
    u = User(email="admin__saas_cf__@x.io", full_name="SA CF",
             is_active=True, email_verified_at=datetime.utcnow(),
             terms_version=tv, terms_accepted_at=datetime.utcnow(),
             is_superadmin=True)
    u.set_password("pw12345678")
    db.session.add(u); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=cA.id, role="owner"))
    db.session.commit()
    return u.email, cA, cB


def _teardown():
    from sqlalchemy import text, inspect
    from app import db
    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        "SELECT id FROM companies WHERE name LIKE '__SAAS_CF__%'"))]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        "DELETE FROM users WHERE email LIKE '%__saas_cf__%'"))
    db.session.commit()


@check("1. superadmin.saas_index endpoint accepts company_id query param")
def _():
    """The route signature stays a GET on /admin/saas with no path
    args. company_id is a query param, so the URL map just needs to
    resolve /admin/saas."""
    from app import create_app
    app = create_app()
    routes = [r for r in app.url_map.iter_rules()
              if r.endpoint == "superadmin.saas_index"]
    assert routes, "superadmin.saas_index not registered"
    r = routes[0]
    methods = set(r.methods or []) - {"HEAD", "OPTIONS"}
    assert methods == {"GET"}, f"expected GET-only; got {methods}"
    return "route stable, GET-only"


@check("2. no query param → both companies present (regression)")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cA, cB = _boot_two_tenants()
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.get("/admin/saas")
                assert r.status_code == 200
                html = r.data.decode("utf-8")
                assert cA.name in html and cB.name in html, \
                    "both companies must appear when no filter is set"
                # And the active-filter banner MUST NOT appear
                assert "عرض كل الشركات" not in html, \
                    "the clear-filter banner should be hidden when " \
                    "no filter is active"
            return "unfiltered view shows both tenants"
        finally:
            _teardown()


@check("3. ?company_id=A → only company A + banner names it")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cA, cB = _boot_two_tenants()
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.get(f"/admin/saas?company_id={cA.id}")
                assert r.status_code == 200
                html = r.data.decode("utf-8")
                assert cA.name in html, \
                    "company A must appear in filtered view"
                # The other company's NAME must not appear anywhere on
                # the page — most reliable narrow check we can do.
                assert cB.name not in html, \
                    "company B should be hidden when filter=A"
                # Banner present + clear link back to /admin/saas
                assert "عرض كل الشركات" in html, \
                    "clear-filter link should render when a filter is active"
            return "filtered view shows only the chosen company + banner"
        finally:
            _teardown()


@check("4. invalid ?company_id=999999 → silent full fallback (no 404)")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cA, cB = _boot_two_tenants()
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.get("/admin/saas?company_id=99999999")
                assert r.status_code == 200, \
                    f"expected 200 fallback; got {r.status_code}"
                html = r.data.decode("utf-8")
                # Both companies visible again (filter treated as absent)
                assert cA.name in html and cB.name in html, \
                    "invalid company_id should silently fall back to " \
                    "the full list, not filter to nothing"
                assert "عرض كل الشركات" not in html, \
                    "the banner shouldn't render for an invalid id"
            return "invalid id gracefully falls back to unfiltered"
        finally:
            _teardown()


@check("5. soft-deleted company id → silent full fallback")
def _():
    from datetime import datetime
    from app import create_app, db
    app = create_app()
    with app.app_context():
        from app.models import Company
        email, cA, cB = _boot_two_tenants()
        try:
            # Soft-delete company B and try to filter by it
            cB_obj = db.session.get(Company, cB.id)
            cB_obj.deleted_at = datetime.utcnow()
            db.session.commit()

            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.get(f"/admin/saas?company_id={cB.id}")
                assert r.status_code == 200
                html = r.data.decode("utf-8")
                # Fall back to the default list — but B is deleted so
                # only A should show
                assert cA.name in html, \
                    "deleted-id filter should fall back to the default " \
                    "list which still includes company A"
                assert "عرض كل الشركات" not in html, \
                    "no active-filter banner for a deleted id"
            return "deleted-id filter treated as absent"
        finally:
            _teardown()


@check("6. company_detail 'راجع' link carries company_id=")
def _():
    """The bug was that this link had NO company_id — clicking it
    from Company A opened every tenant's SaaS invoices. Regression
    guard against re-introducing the bare url."""
    src = _strip_comments(_read("app/templates/admin/company_detail.html"))
    m = re.search(
        r"url_for\(\s*'superadmin\.saas_index'\s*,\s*company_id\s*=\s*company\.id\s*\)",
        src)
    assert m, \
        "the 'راجع' link in company_detail.html must build the " \
        "URL with company_id=company.id — otherwise the SaaS list " \
        "opens for every tenant, defeating the ticket."
    return "link carries company_id in company_detail"


@check("7. saas_index template renders the filter banner when active")
def _():
    src = _strip_comments(_read("app/templates/admin/saas_index.html"))
    assert "filter_company" in src, \
        "template must read `filter_company` from the context to " \
        "know whether a filter is active"
    assert "عرض كل الشركات" in src, \
        "template must render a 'clear filter' link"
    # And the clear link must point back to saas_index without params
    assert "url_for('superadmin.saas_index')" in src, \
        "clear-filter link must go back to /admin/saas with no query"
    return "banner + clear link both wired"


def main():
    passed = failed = 0
    for label, fn in CHECKS:
        try:
            res = fn()
            print(f"PASS  {label}  ⇒ {res}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
            failed += 1
            import traceback; traceback.print_exc()
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
