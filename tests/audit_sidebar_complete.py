#!/usr/bin/env python3
"""MARSOUD-SIDEBAR-COMPLETE (Abdelhamid 2026-07-24).

Checks:
  1. Owner sees every one of the 8 previously-orphan endpoints as a
     sidebar link.
  2. Reports index page has cards for `profitability` + `cashier_sales`.
  3. Non-owner (accountant) does NOT see settings_usage or companies.index
     (those are owner-only per the new permission).
  4. `settings_usage.view` + `companies.manage` are registered in P.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__SB_%__'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'sb-%@x.test'"))
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id NOT IN "
                    "(SELECT id FROM companies)"))


def _mk_user_and_company(suffix, role):
    from app.models import Company, User, UserStatus, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    plan = Plan.query.first()
    c = Company(name=f"__SB_{suffix}__", base_currency="EGP",
                 subdomain=f"sb-{suffix.lower()}",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1),
                 plan_id=plan.id if plan else None,
                 intended_plan_id=plan.id if plan else None)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email=f"sb-{suffix.lower()}@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name=f"sb-{suffix}", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role=role))
    db.session.commit()
    return u, c


ORPHAN_ENDPOINTS = [
    "settings_usage.index",
    "reports.cashier_sales",
    "reports.profitability",
    "inventory.transfers",
    "inventory.movements",
    "pos.shifts",
    "pos.history",
    "companies.index",
]


@check("1. settings_usage.view and companies.manage registered in P")
def _():
    from app.services.permissions import P
    assert "settings_usage.view" in P, \
        "settings_usage.view missing from P"
    assert "companies.manage" in P, "companies.manage missing from P"
    assert P["settings_usage.view"] == {"owner"}
    assert P["companies.manage"] == {"owner"}
    return "2 codes registered, owner-only"


@check("2. Owner sees every one of the 8 orphan endpoints in sidebar")
def _():
    from flask import current_app
    _teardown()
    u, c = _mk_user_and_company("OWNER", "owner")
    # is_superadmin bypasses the plan-selection + terms middleware,
    # so the dashboard renders the full sidebar (not the choose-plan
    # shell which hides it).
    u.is_superadmin = True; db.session.commit()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
        sess["active_company_id"] = c.id
    # /reports/ is a well-known page that renders the full sidebar
    # and works for owners+superadmins alike.
    r = client.get("/reports/", follow_redirects=True)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)
    assert 'id="sidebar"' in body, "sidebar not rendered on /reports/"
    missing = []
    for ep in ORPHAN_ENDPOINTS:
        # url_for-generated URLs live in href="..." attributes. Check
        # both the endpoint token and the URL fragment.
        blueprint, action = ep.split(".", 1)
        # simple check: the endpoint URL fragment is present in a link
        url_map = {
            "settings_usage.index":   "/settings/usage",
            "reports.cashier_sales": "/reports/cashier-sales",
            "reports.profitability": "/reports/profitability",
            "inventory.transfers":    "/inventory/transfers",
            "inventory.movements":    "/inventory/movements",
            "pos.shifts":             "/pos/shifts",
            "pos.history":            "/pos/history",
            "companies.index":        "/companies/",
        }
        url = url_map[ep]
        if url not in body:
            missing.append(ep)
    assert not missing, f"missing from owner sidebar: {missing}"
    return f"all 8 orphan endpoints present"


@check("3. /reports has profitability + cashier_sales cards")
def _():
    from flask import current_app
    u = _STATE.get("u") or None
    # Reuse the owner session from check 2 by re-creating it against
    # the freshest company (in case check 2's fixture was cleaned).
    from app.models import User
    owner = User.query.filter_by(email="sb-owner@x.test").first()
    assert owner, "prior owner missing"
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(owner.id)
        sess["_fresh"] = True
        cid = owner.companies[0].id
        sess["active_company_id"] = cid
    r = client.get("/reports/")
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)
    assert "تقرير الربحية" in body, "profitability card missing"
    assert "مبيعات الكاشيرز" in body, "cashier_sales card missing"
    return "both cards render in /reports"


@check("4. Accountant does NOT see settings_usage/companies.index")
def _():
    from flask import current_app
    u, c = _mk_user_and_company("ACCT", "accountant")
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
        sess["active_company_id"] = c.id
    r = client.get("/")
    if r.status_code == 302:
        r = client.get(r.headers.get("Location") or "/")
    body = r.get_data(as_text=True)
    # Owner-only URLs must NOT appear in accountant sidebar.
    assert "/settings/usage" not in body, \
        "settings/usage leaked to accountant"
    # Note: /companies/edit stays visible under 'بيانات الشركة' via
    # users.manage — the ONE we hide from accountant is the multi-
    # company "كل شركاتي" listing at /companies/ (no path suffix).
    # We check the exact anchor with the label text to avoid false
    # matches on /companies/edit which is legit.
    assert "كل شركاتي" not in body, "companies.index leaked to accountant"
    return "settings_usage + companies.index correctly hidden"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _teardown()
            for label, fn in CHECKS:
                try:
                    res = fn()
                    print(f"PASS  {label}  ⇒ {res}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback; traceback.print_exc()
        finally:
            _teardown()
            print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
