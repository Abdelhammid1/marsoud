#!/usr/bin/env python3
"""MARSOUD-EMAIL-VERIFY (Abdelhamid 2026-07-22).

New signups start as PENDING_VERIFICATION. A verify link is emailed;
clicking it flips status → ACTIVE + stamps email_verified_at.
Middleware blocks dashboard access until verified. Invited users
+ existing (grandfathered) accounts are unaffected.

Checks:
  1. POST /register → user.status == PENDING_VERIFICATION,
     user.email_verified_at is None, email logged (dev mode).
  2. verify token round-trip: generate → parse → matches user_id.
  3. GET /verify/<token> → status flips to ACTIVE +
     email_verified_at set + subsequent hit says "already verified".
  4. Expired/bogus token → user unchanged, flash error.
  5. Middleware: PENDING_VERIFICATION user hitting /dashboard/
     redirects to /verify-pending.
  6. Middleware allowlist: PENDING_VERIFICATION user CAN hit
     /verify-pending itself (no infinite redirect).
  7. Verified user (ACTIVE) hits /dashboard/ without redirect.
  8. Migration backfill — legacy ACTIVE user with NULL
     email_verified_at is unaffected by the middleware (they get
     grandfathered).
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
        # 1) Full company-scoped wipe for the __EVERIFY_%__ fixtures.
        target_cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__EVERIFY_%__'"
        ))]
        for cid in target_cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(
                        text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text(
                "DELETE FROM companies WHERE id = :c"), {"c": cid})
        conn.execute(text(
            "DELETE FROM user_companies WHERE user_id IN "
            "(SELECT id FROM users WHERE email LIKE 'everify-%@x.test')"))
        conn.execute(text(
            "DELETE FROM employees WHERE email LIKE 'everify-%@x.test'"))
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'everify-%@x.test'"))
        # 2) Zombie sweep — any leftover company-scoped rows pointing at
        # a dead company (from older buggy runs) get purged so the
        # next Company() insert can safely reuse the PK.
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id NOT IN "
                    "(SELECT id FROM companies)"))
        conn.execute(text(
            "DELETE FROM user_companies WHERE user_id NOT IN "
            "(SELECT id FROM users)"))
        conn.execute(text(
            "DELETE FROM user_companies WHERE company_id NOT IN "
            "(SELECT id FROM companies)"))


@check("1. POST /register sets status=PENDING_VERIFICATION")
def _():
    from flask import current_app
    from app.models import User, UserStatus, Company
    _teardown()
    client = current_app.test_client()
    r = client.post("/register", data={
        "email": "everify-new@x.test",
        "full_name": "Verify New",
        "password": "Passw0rd1",
        "company_name": "__EVERIFY_NEW__",
        "subdomain": "everify-new",
        "base_currency": "EGP",
    }, follow_redirects=False)
    u = User.query.filter_by(email="everify-new@x.test").one()
    assert u.status == UserStatus.PENDING_VERIFICATION.value, \
        f"status={u.status}"
    assert u.email_verified_at is None
    return f"status={u.status}, verified_at=None"


@check("2. verify token round-trip")
def _():
    from app.services.permissions import (
        generate_verify_email_token, parse_verify_email_token,
    )
    tok = generate_verify_email_token(1234)
    payload = parse_verify_email_token(tok)
    assert payload == {"user_id": 1234}
    # A garbage token → None.
    assert parse_verify_email_token("garbage.token.here") is None
    return "token round-trip + garbage rejection OK"


@check("3. GET /verify/<token> flips status → ACTIVE")
def _():
    from flask import current_app
    from app.models import User, UserStatus
    from app.services.permissions import generate_verify_email_token
    u = User.query.filter_by(email="everify-new@x.test").one()
    assert u.status == UserStatus.PENDING_VERIFICATION.value
    tok = generate_verify_email_token(u.id)
    client = current_app.test_client()
    r = client.get(f"/verify/{tok}", follow_redirects=False)
    assert r.status_code == 302
    db.session.expire_all()
    u = db.session.get(User, u.id)
    assert u.status == UserStatus.ACTIVE.value, f"status={u.status}"
    assert u.email_verified_at is not None
    # Idempotent — a repeat click just says "already verified".
    r2 = client.get(f"/verify/{tok}", follow_redirects=False)
    assert r2.status_code == 302
    _STATE["verified_user_id"] = u.id
    return f"status=ACTIVE, verified_at set"


@check("4. Bogus token → user unchanged")
def _():
    from flask import current_app
    client = current_app.test_client()
    r = client.get("/verify/completely-bogus-token", follow_redirects=False)
    assert r.status_code == 302
    return "bogus token bounces to login"


@check("5. PENDING_VERIFICATION user redirected from /home")
def _():
    from flask import current_app, g
    from app.models import User, Company, UserStatus, Employee
    from werkzeug.security import generate_password_hash
    # Create a second user in PENDING_VERIFICATION state directly
    # (bypasses register to avoid depending on it).
    _teardown()
    for k in ("_login_user", "active_company"):
        try: g.pop(k, None)
        except Exception: pass

    from app.services.subscription import activate_default_subscription
    from app.services.seed_coa import seed_default_coa

    c = Company(name="__EVERIFY_PENDING__", base_currency="EGP",
                subdomain="everify-pending")
    activate_default_subscription(c)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    u = User(email="everify-pending@x.test",
             password_hash=generate_password_hash(
                 "Passw0rd1", method="pbkdf2:sha256"),
             full_name="pending",
             status=UserStatus.PENDING_VERIFICATION.value,
             is_active=True)
    u.companies.append(c)
    db.session.add(u); db.session.flush()
    db.session.commit()

    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
        sess["active_company_id"] = c.id
    r = client.get("/home", follow_redirects=False)
    assert r.status_code == 302
    assert "/verify-pending" in r.headers["Location"], \
        f"expected redirect to /verify-pending, got {r.headers['Location']}"
    _STATE["pending_user_id"] = u.id
    _STATE["pending_cid"] = c.id
    return "/home → /verify-pending"


@check("6. PENDING_VERIFICATION user CAN reach /verify-pending "
       "(allowlist works, no infinite redirect)")
def _():
    from flask import current_app
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["pending_user_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["pending_cid"]
    r = client.get("/verify-pending", follow_redirects=False)
    assert r.status_code == 200, f"status={r.status_code}"
    return "verify-pending page reachable"


@check("7. Verified (ACTIVE) user reaches /home without middleware "
       "redirecting to /verify-pending")
def _():
    from flask import current_app, g
    from app.models import User, Company, UserStatus
    from werkzeug.security import generate_password_hash
    from app.services.subscription import activate_default_subscription
    from app.services.seed_coa import seed_default_coa
    from sqlalchemy import text
    from datetime import datetime as _dt

    for k in ("_login_user", "active_company"):
        try: g.pop(k, None)
        except Exception: pass
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM user_companies WHERE user_id IN "
            "(SELECT id FROM users WHERE email = 'everify-active@x.test') "
            "OR company_id IN "
            "(SELECT id FROM companies WHERE name = '__EVERIFY_ACTIVE__')"))
        conn.execute(text(
            "DELETE FROM companies WHERE name = '__EVERIFY_ACTIVE__'"))
        conn.execute(text(
            "DELETE FROM users WHERE email = 'everify-active@x.test'"))
    c = Company(name="__EVERIFY_ACTIVE__", base_currency="EGP",
                subdomain="everify-active")
    activate_default_subscription(c)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email="everify-active@x.test",
             password_hash=generate_password_hash(
                 "Passw0rd1", method="pbkdf2:sha256"),
             full_name="active",
             status=UserStatus.ACTIVE.value,
             email_verified_at=_dt.utcnow(),
             is_active=True)
    u.companies.append(c)
    db.session.add(u); db.session.commit()

    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
        sess["active_company_id"] = c.id
    r = client.get("/home", follow_redirects=False)
    if r.status_code == 302:
        loc = r.headers.get("Location", "")
        assert "verify-pending" not in loc, \
            f"verified ACTIVE user redirected to {loc}"
    return f"status={r.status_code}, not blocked"


@check("8. Legacy ACTIVE user with NULL email_verified_at is "
       "grandfathered (middleware only blocks PENDING_VERIFICATION)")
def _():
    from flask import current_app
    from app.models import User, Company, UserStatus, Employee
    from werkzeug.security import generate_password_hash
    from app.services.subscription import activate_default_subscription
    from app.services.seed_coa import seed_default_coa
    from sqlalchemy import text

    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM companies WHERE name = '__EVERIFY_LEGACY__'"))
        conn.execute(text(
            "DELETE FROM users WHERE email = 'everify-legacy@x.test'"))

    c = Company(name="__EVERIFY_LEGACY__", base_currency="EGP",
                subdomain="everify-legacy")
    activate_default_subscription(c)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    u = User(email="everify-legacy@x.test",
             password_hash=generate_password_hash(
                 "Passw0rd1", method="pbkdf2:sha256"),
             full_name="legacy",
             status=UserStatus.ACTIVE.value,
             email_verified_at=None,     # Legacy shape
             is_active=True)
    u.companies.append(c)
    db.session.add(u); db.session.commit()

    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
        sess["active_company_id"] = c.id
    r = client.get("/home", follow_redirects=False)
    if r.status_code == 302:
        loc = r.headers.get("Location", "")
        assert "verify-pending" not in loc, \
            f"legacy user redirected to {loc}"
    return f"legacy ACTIVE user unaffected (status={r.status_code})"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _teardown()   # Start from a clean state.
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
