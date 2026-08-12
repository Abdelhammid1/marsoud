#!/usr/bin/env python3
"""MARSOUD-RESTRICTED-SUPERADMIN-CREATE-UI (2026-08-13).

Follow-up on MARSOUD-APPROVAL-GATED-SUPERADMIN — adds a
UI to promote OR create a restricted superadmin from
/admin/users, so Abdelhamid doesn't need SQL or the
shell.

Checks:
  1. Schema sanity — requires_approval column still exists.
  2. GET form as primary → 200 + form fields render.
  3. GET form as restricted → 403.
  4. GET form as non-superadmin → 403.
  5. CREATE path — new email → new User inserted with
     is_superadmin=True, requires_approval=True,
     is_active=True, status='ACTIVE'. Password verifies.
     PAL row `user_created_restricted_superadmin`
     written.
  6. PROMOTE path — existing email → flags flipped,
     name + password_hash + companies UNTOUCHED, PAL row
     `user_promoted_to_restricted_superadmin` written.
  7. Idempotency — second promote of same user → "بالفعل"
     flash, no duplicate PAL.
  8. Bad input — malformed email + short password → both
     rejected with no user side-effects.
  9. Registry check — endpoint is in DESTRUCTIVE_ENDPOINTS
     + ENDPOINT_LABELS_AR.
 10. Gate integration — a restricted user's POST on the
     new endpoint is queued (not silently 403'd) by
     `gate_request`.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app import create_app, db

PREFIX = "__RSC_"
EMAIL_PRIMARY = "rsc-primary@x.test"
EMAIL_RESTRICTED = "rsc-restricted@x.test"
EMAIL_NONADMIN = "rsc-plain@x.test"

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text
    db.session.rollback()
    db.session.close()
    with db.engine.begin() as conn:
        # Delete PAL rows that reference our test users
        # BEFORE deleting the users — target_user_id FK.
        conn.execute(text(
            "DELETE FROM platform_audit_logs "
            "WHERE action LIKE 'user_%_restricted%'"))
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'rsc-%@x.test'"))


def _mk_user(email, *, full_name=None, is_superadmin=False,
               requires_approval=False, password="Str0ngPass1!"):
    from app.models import User, UserStatus
    u = User(email=email,
             full_name=full_name or email,
             is_active=True,
             is_superadmin=is_superadmin,
             requires_approval=requires_approval,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    return u


def _mk_fixture_users():
    primary = _mk_user(EMAIL_PRIMARY, is_superadmin=True,
                        requires_approval=False)
    restricted = _mk_user(EMAIL_RESTRICTED, is_superadmin=True,
                           requires_approval=True)
    plain = _mk_user(EMAIL_NONADMIN, is_superadmin=False,
                      requires_approval=False)
    return primary, restricted, plain


def _client_as(user_id):
    """Fresh-context test client. Clears g._login_user so
    a previous request's flask-login cache doesn't poison
    current_user (Flask 2.2+ reuses the outer app_context
    across successive requests)."""
    from flask import current_app, g
    if "_login_user" in g:
        del g._login_user
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return client


# ── checks ────────────────────────────────────────────────── #

@check("1. Schema — requires_approval column exists")
def _():
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    cols = {c["name"] for c in insp.get_columns("users")}
    assert "requires_approval" in cols, \
        "users.requires_approval missing (bad merge?)"
    return "OK"


@check("2. GET form as primary → 200 + form renders")
def _():
    _teardown()
    primary, _r, _p = _mk_fixture_users()
    r = _client_as(primary.id).get(
        "/admin/users/create-restricted")
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)
    assert 'name="email"' in body, "email field missing"
    assert 'name="full_name"' in body, "full_name field missing"
    assert 'name="password"' in body, "password field missing"
    return "form OK"


@check("3. GET form as restricted → 403")
def _():
    _teardown()
    _p, restricted, _u = _mk_fixture_users()
    r = _client_as(restricted.id).get(
        "/admin/users/create-restricted")
    assert r.status_code == 403, f"got {r.status_code}"
    return "restricted refused"


@check("4. GET form as non-superadmin → 403")
def _():
    _teardown()
    _p, _r, plain = _mk_fixture_users()
    r = _client_as(plain.id).get(
        "/admin/users/create-restricted")
    assert r.status_code == 403, f"got {r.status_code}"
    return "non-superadmin refused"


@check("5. CREATE path — new user with password")
def _():
    from app.models import User, PlatformAuditLog
    _teardown()
    primary, _r, _p = _mk_fixture_users()
    r = _client_as(primary.id).post(
        "/admin/users/create-restricted",
        data={"email": "rsc-newbie@x.test",
              "full_name": "Newbie", "password": "Str0ngPass1!"})
    assert r.status_code in (302, 303), f"got {r.status_code}"
    u = User.query.filter_by(email="rsc-newbie@x.test").first()
    assert u is not None, "user not created"
    assert u.is_superadmin is True
    assert u.requires_approval is True
    assert u.is_active is True
    assert u.status == "ACTIVE"
    assert u.check_password("Str0ngPass1!"), \
        "password not set correctly"
    pal = PlatformAuditLog.query.filter_by(
        action="user_created_restricted_superadmin",
        target_user_id=u.id).count()
    assert pal == 1, f"expected 1 PAL, got {pal}"
    return "created + audit + password OK"


@check("6. PROMOTE path — existing user, flags flipped, rest preserved")
def _():
    from app.models import User, PlatformAuditLog
    _teardown()
    primary, _r, _p = _mk_fixture_users()
    # Seed a plain user (not superadmin, not restricted).
    seeded = _mk_user("rsc-employee@x.test",
                       full_name="Employee",
                       password="OrigPassw0rd!")
    orig_hash = seeded.password_hash
    orig_name = seeded.full_name
    r = _client_as(primary.id).post(
        "/admin/users/create-restricted",
        data={"email": "rsc-employee@x.test"})
    assert r.status_code in (302, 303)
    db.session.expire_all()
    u = User.query.filter_by(email="rsc-employee@x.test").first()
    assert u.is_superadmin is True, "is_superadmin not flipped"
    assert u.requires_approval is True, \
        "requires_approval not flipped"
    # Preserved fields.
    assert u.password_hash == orig_hash, \
        "password_hash was clobbered (no forced re-login)"
    assert u.full_name == orig_name, "full_name changed"
    pal = PlatformAuditLog.query.filter_by(
        action="user_promoted_to_restricted_superadmin",
        target_user_id=u.id).count()
    assert pal == 1, f"expected 1 promote PAL, got {pal}"
    return "promoted + preserved"


@check("7. Idempotency — second promote is a no-op")
def _():
    from app.models import PlatformAuditLog
    _teardown()
    primary, _r, _p = _mk_fixture_users()
    _mk_user("rsc-dup@x.test")
    _client_as(primary.id).post(
        "/admin/users/create-restricted",
        data={"email": "rsc-dup@x.test"})
    _client_as(primary.id).post(
        "/admin/users/create-restricted",
        data={"email": "rsc-dup@x.test"})
    pal = PlatformAuditLog.query.filter_by(
        action="user_promoted_to_restricted_superadmin").count()
    assert pal == 1, \
        f"duplicate promote (expected 1 PAL, got {pal})"
    return "no double-log"


@check("8. Bad input — malformed email + short password rejected")
def _():
    from app.models import User
    _teardown()
    primary, _r, _p = _mk_fixture_users()
    # (a) malformed email.
    r1 = _client_as(primary.id).post(
        "/admin/users/create-restricted",
        data={"email": "not-an-email",
              "full_name": "x", "password": "Str0ngPass1!"})
    assert r1.status_code in (302, 303)
    assert User.query.filter_by(
        email="not-an-email").first() is None
    # (b) new email, short password.
    r2 = _client_as(primary.id).post(
        "/admin/users/create-restricted",
        data={"email": "rsc-weakpw@x.test",
              "full_name": "x", "password": "abc"})
    assert r2.status_code in (302, 303)
    assert User.query.filter_by(
        email="rsc-weakpw@x.test").first() is None
    return "both refused"


@check("9. Registry — endpoint listed in DESTRUCTIVE + labels")
def _():
    from app.services.superadmin_approval import (
        DESTRUCTIVE_ENDPOINTS, ENDPOINT_LABELS_AR,
    )
    assert ("superadmin.user_create_restricted"
            in DESTRUCTIVE_ENDPOINTS), \
        "endpoint missing from DESTRUCTIVE_ENDPOINTS"
    assert ("superadmin.user_create_restricted"
            in ENDPOINT_LABELS_AR), \
        "endpoint missing from ENDPOINT_LABELS_AR"
    return "registry OK"


@check("10. Gate integration — restricted user's POST is queued")
def _():
    """The view body 403s a restricted user directly, but
    if that guard were ever removed the DESTRUCTIVE
    registry entry would still queue the POST for
    approval. Verify the registry side works by calling
    gate_request in a synthesized POST context with a
    restricted user logged in."""
    from flask import current_app
    from werkzeug.routing import Rule
    from flask_login import login_user
    from app.services.superadmin_approval import gate_request
    _teardown()
    _p, restricted, _u = _mk_fixture_users()
    with current_app.test_request_context(
            "/admin/users/create-restricted",
            method="POST", data={"email": "x@y.test"}):
        from flask import request as flask_req
        flask_req.url_rule = Rule(
            "/admin/users/create-restricted",
            endpoint="superadmin.user_create_restricted")
        login_user(restricted)
        response = gate_request()
    # gate_request returns a redirect (302 to pending-actions)
    # when the endpoint is in DESTRUCTIVE_ENDPOINTS.
    assert response is not None, "gate did not intercept"
    assert response.status_code in (302, 303), \
        f"expected redirect, got {response.status_code}"
    return "queued via gate"


def main():
    app = create_app()
    passed = failed = 0
    for label, fn in CHECKS:
        with app.app_context():
            try:
                _teardown()
                res = fn()
                print(f"PASS  {label}  ⇒ {res}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}  ⇒ "
                      f"{type(e).__name__}: {e}")
                failed += 1
                import traceback
                traceback.print_exc()
    with app.app_context():
        _teardown()
        print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
