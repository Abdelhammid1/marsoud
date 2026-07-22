#!/usr/bin/env python3
"""MARSOUD-CHOOSE-PLAN (Abdelhamid 2026-07-22).

Signup used to auto-assign the Enterprise plan silently. Now:
  · Signup leaves plan_id + intended_plan_id NULL.
  · After email verify, the OWNER is nudged to /choose-plan before
    they reach the dashboard.
  · During the trial window (subscription_expires_at > now), the
    plan_gating middleware treats the company as full-access
    regardless of intended_plan_id.
  · After trial expiry, the chosen plan's module set is enforced.

Checks:
  1. /register no longer stamps plan_id or intended_plan_id.
  2. Middleware: verified OWNER with no intended_plan_id → /choose-plan.
  3. Middleware: team_member users are NOT nudged (only owner).
  4. POST /choose-plan sets company.intended_plan_id.
  5. subitem_allowed() returns True for any endpoint while the company
     is inside its trial window (regardless of plan).
  6. subitem_allowed() enforces the plan's list once the trial expires.
"""
import os
import sys
from datetime import datetime, timedelta
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
        target_cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__CP_%__'"))]
        for cid in target_cids:
            conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"),
                          {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                                  {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"), {"c": cid})
        conn.execute(text("DELETE FROM user_companies WHERE user_id IN "
                          "(SELECT id FROM users WHERE email LIKE 'cp-%@x.test')"))
        conn.execute(text("DELETE FROM employees WHERE email LIKE 'cp-%@x.test'"))
        conn.execute(text("DELETE FROM users WHERE email LIKE 'cp-%@x.test'"))
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id NOT IN "
                    "(SELECT id FROM companies)"))


def _login(uid, cid=None):
    from flask import current_app, g
    for k in ("_login_user", "active_company"):
        try: g.pop(k, None)
        except Exception: pass
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
        if cid:
            sess["active_company_id"] = cid
    return client


@check("1. /register no longer stamps plan_id or intended_plan_id")
def _():
    from flask import current_app
    from app.models import User, Company
    _teardown()
    client = current_app.test_client()
    r = client.post("/register", data={
        "email": "cp-signup@x.test",
        "full_name": "Sign",
        "password": "Passw0rd1",
        "company_name": "__CP_SIGNUP__",
        "subdomain": "cp-signup",
        "base_currency": "EGP",
        "agree_terms": "on",
    }, follow_redirects=False)
    c = Company.query.filter_by(name="__CP_SIGNUP__").one()
    assert c.plan_id is None, f"plan_id={c.plan_id} (should be NULL)"
    assert c.intended_plan_id is None
    # But trial window IS set.
    assert c.subscription_expires_at is not None
    return "plan_id + intended_plan_id both NULL, trial set"


@check("2. Verified OWNER with no intended_plan_id → /choose-plan")
def _():
    from flask import current_app, g
    from app.models import User, Company, UserStatus
    from werkzeug.security import generate_password_hash
    from app.models.user import user_companies
    from app.services.subscription import activate_default_subscription
    from app.services.seed_coa import seed_default_coa
    from sqlalchemy import text

    for k in ("_login_user", "active_company"):
        try: g.pop(k, None)
        except Exception: pass
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE email = 'cp-owner@x.test'"))
        conn.execute(text("DELETE FROM companies WHERE name = '__CP_OWNER__'"))
    c = Company(name="__CP_OWNER__", base_currency="EGP",
                subdomain="cp-owner")
    activate_default_subscription(c, plan_code=None)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email="cp-owner@x.test",
             password_hash=generate_password_hash(
                 "Passw0rd1", method="pbkdf2:sha256"),
             full_name="owner", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow())
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()

    client = _login(u.id, cid=c.id)
    r = client.get("/home", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers.get("Location", "")
    assert "/choose-plan" in loc, f"expected /choose-plan, got {loc}"
    _STATE["owner_id"] = u.id
    _STATE["cid"] = c.id
    return "owner redirected to /choose-plan"


@check("3. Team member is NOT nudged (only owner)")
def _():
    from flask import current_app, g
    from app.models import User, UserStatus
    from app.models.user import user_companies
    from werkzeug.security import generate_password_hash
    from sqlalchemy import text
    for k in ("_login_user", "active_company"):
        try: g.pop(k, None)
        except Exception: pass
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE email = 'cp-member@x.test'"))
    m = User(email="cp-member@x.test",
             password_hash=generate_password_hash("x", method="pbkdf2:sha256"),
             full_name="member", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow())
    db.session.add(m); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=m.id, company_id=_STATE["cid"], role="team_member"))
    db.session.commit()

    client = _login(m.id, cid=_STATE["cid"])
    r = client.get("/home", follow_redirects=False)
    if r.status_code == 302:
        loc = r.headers.get("Location", "")
        assert "/choose-plan" not in loc, \
            f"team member redirected to {loc}"
    return "team member not blocked"


@check("4. POST /choose-plan stamps intended_plan_id")
def _():
    from flask import current_app
    from app.models import Plan, Company
    plan = Plan.query.filter_by(is_active=True).first()
    assert plan, "need at least one active plan in DB"
    client = _login(_STATE["owner_id"], cid=_STATE["cid"])
    r = client.post("/choose-plan", data={
        "plan_id": plan.id,
    }, follow_redirects=False)
    assert r.status_code == 302
    db.session.expire_all()
    c = db.session.get(Company, _STATE["cid"])
    assert c.intended_plan_id == plan.id, \
        f"intended_plan_id={c.intended_plan_id}"
    return f"chose plan #{plan.id} ({plan.code})"


@check("5. During trial, subitem_allowed() returns True for ANY "
       "endpoint (full access regardless of intended plan)")
def _():
    from app.models import Company, Plan
    from app.services.plan_gating import subitem_allowed
    from datetime import datetime as _dt, timedelta as _td
    c = db.session.get(Company, _STATE["cid"])
    # Force intended_plan to something with a limited module set so
    # we can prove the trial override.
    limited_plan = Plan(code="cp-limited", name="Limited",
                        name_ar="محدود",
                        allowed_subitems='["invoices.index"]',
                        is_active=True)
    db.session.add(limited_plan); db.session.flush()
    c.subscription_plan_id = limited_plan.id if hasattr(c, "subscription_plan_id") else None
    c.plan_id = limited_plan.id  # actual FK column
    c.subscription_expires_at = _dt.utcnow() + _td(days=5)  # trial live
    db.session.commit()
    # Even endpoints NOT in the plan's list should pass during trial.
    assert subitem_allowed("crm.leads_index", c) is True, \
        "trial should grant access to endpoints outside the plan"
    db.session.delete(limited_plan)
    db.session.commit()
    return "trial → full access"


@check("6. After trial expiry, subitem_allowed enforces the plan list")
def _():
    from app.models import Company, Plan
    from app.services.plan_gating import subitem_allowed
    from datetime import datetime as _dt, timedelta as _td
    limited_plan = Plan(code="cp-limited-2", name="Limited",
                        name_ar="محدود",
                        allowed_subitems='["invoices.index"]',
                        is_active=True)
    db.session.add(limited_plan); db.session.flush()
    c = db.session.get(Company, _STATE["cid"])
    c.plan_id = limited_plan.id
    c.subscription_expires_at = _dt.utcnow() - _td(days=1)  # EXPIRED
    db.session.commit()
    assert subitem_allowed("invoices.index", c) is True
    assert subitem_allowed("crm.leads_index", c) is False, \
        "post-trial should enforce the plan's list"
    # Repair.
    c.subscription_expires_at = _dt.utcnow() + _td(days=30)
    db.session.delete(limited_plan)
    db.session.commit()
    return "post-trial → plan list enforced"


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
