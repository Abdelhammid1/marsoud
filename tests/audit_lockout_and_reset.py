#!/usr/bin/env python3
"""MARSOUD-LOCKOUT-RESET (Abdelhamid 2026-07-22).

Two related sub-features shipped together:

  1. **Account lockout.** After 5 wrong-password attempts, the user is
     locked for 15 min. Successful login resets the counter. Refuses
     even the correct password during the lock window (so brute-forcers
     can't detect success/failure by timing).

  2. **Forgot Password.** /forgot-password → user gets a reset link;
     /reset-password/<token> lets them set a new password. Token
     expires after 1 hour and is invalidated the moment the password
     is changed (via a hash snapshot in the token payload).

Checks:
  1. 5 wrong-password attempts lock the account.
  2. During the lock window, even correct pw is refused.
  3. Successful login resets failed_login_attempts + locked_until.
  4. Reset token round-trip works.
  5. Reset token is invalidated when the password is changed (single-use).
  6. Reset flow via POST actually changes the password + unlocks user.
  7. /forgot-password with unknown email still returns a friendly
     'sent' message (anti-enumeration).
  8. Weak new password on reset is rejected by password_policy.
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


def _mk_user(email, password="Passw0rd1"):
    from app.models import User
    from werkzeug.security import generate_password_hash
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE email = :e"),
                     {"e": email})
    u = User(email=email,
             password_hash=generate_password_hash(password, method="pbkdf2:sha256"),
             full_name=email,
             is_active=True, is_superadmin=True,   # skip company gate
             email_verified_at=datetime.utcnow(),
             failed_login_attempts=0, locked_until=None)
    db.session.add(u); db.session.commit()
    return u


def _teardown():
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'lockout-%@x.test'"))


@check("1. 5 wrong-password attempts lock the account")
def _():
    from flask import current_app
    u = _mk_user("lockout-a@x.test", "Correct1")
    client = current_app.test_client()
    for i in range(5):
        r = client.post("/login", data={
            "email": "lockout-a@x.test",
            "password": "WrongPass1",
        }, follow_redirects=False)
    db.session.expire_all()
    from app.models import User
    u = db.session.get(User, u.id)
    assert u.failed_login_attempts >= 5, \
        f"counter = {u.failed_login_attempts}"
    assert u.locked_until is not None, "locked_until must be set"
    assert u.locked_until > datetime.utcnow(), \
        "locked_until must be in the future"
    _STATE["locked_user_id"] = u.id
    return (f"attempts={u.failed_login_attempts}, "
            f"locked_until={u.locked_until.isoformat()}")


@check("2. During the lock window even CORRECT pw is refused")
def _():
    from flask import current_app
    from app.models import User
    client = current_app.test_client()
    r = client.post("/login", data={
        "email": "lockout-a@x.test",
        "password": "Correct1",   # actually correct
    }, follow_redirects=False)
    # The route flashes an Arabic "الحساب مقفل مؤقتاً" message and
    # re-renders login → HTTP 200 (not 302 to dashboard).
    assert r.status_code == 200, \
        f"correct pw during lock returned {r.status_code}, expected 200"
    body = r.get_data(as_text=True)
    assert "مقفل" in body, "lock message missing from response"
    return "correct pw refused during lock window"


@check("3. Successful login resets counter + unlocks")
def _():
    from flask import current_app
    from app.models import User
    # Manually clear locked_until to simulate the window passing.
    u = db.session.get(User, _STATE["locked_user_id"])
    u.locked_until = None
    db.session.commit()

    client = current_app.test_client()
    r = client.post("/login", data={
        "email": "lockout-a@x.test",
        "password": "Correct1",
    }, follow_redirects=False)
    assert r.status_code == 302, f"login → {r.status_code}"
    db.session.expire_all()
    u = db.session.get(User, u.id)
    assert u.failed_login_attempts == 0, \
        f"counter should be 0, got {u.failed_login_attempts}"
    assert u.locked_until is None
    return "counter=0, unlocked"


@check("4. Reset token round-trip")
def _():
    from app.services.permissions import (
        generate_password_reset_token, parse_password_reset_token,
    )
    from app.models import User
    u = _mk_user("lockout-b@x.test", "OldPass1")
    tok = generate_password_reset_token(u)
    payload = parse_password_reset_token(tok)
    assert payload["user_id"] == u.id
    assert payload["h"] == (u.password_hash or "")[-12:]
    _STATE["reset_user_id"] = u.id
    _STATE["reset_token"] = tok
    return "user_id + hash snapshot preserved"


@check("5. Reset token invalidated after password change (single-use)")
def _():
    from flask import current_app
    from app.models import User
    u = db.session.get(User, _STATE["reset_user_id"])
    tok = _STATE["reset_token"]
    # Change the password out-of-band.
    u.set_password("Different1")
    db.session.commit()
    # Now the OLD token's hash snapshot no longer matches.
    client = current_app.test_client()
    r = client.get(f"/reset-password/{tok}", follow_redirects=False)
    # Route flashes "الرابط تم استخدامه بالفعل" + redirects to /forgot-password.
    assert r.status_code == 302
    assert "/forgot-password" in r.headers["Location"], \
        f"expected redirect to forgot-password, got {r.headers['Location']}"
    return "old token rejected after pw change"


@check("6. Reset flow via POST actually changes password + unlocks")
def _():
    from flask import current_app
    from app.services.permissions import generate_password_reset_token
    from app.models import User
    u = _mk_user("lockout-c@x.test", "OldPass1")
    # Simulate a locked account so we can verify the reset unlocks it.
    u.failed_login_attempts = 5
    u.locked_until = datetime.utcnow() + timedelta(minutes=15)
    db.session.commit()
    tok = generate_password_reset_token(u)
    client = current_app.test_client()
    r = client.post(f"/reset-password/{tok}", data={
        "password": "BrandNew1",
        "confirm": "BrandNew1",
    }, follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]
    db.session.expire_all()
    u = db.session.get(User, u.id)
    assert u.check_password("BrandNew1")
    assert not u.check_password("OldPass1")
    assert u.failed_login_attempts == 0
    assert u.locked_until is None
    return "pw changed + lockout cleared"


@check("7. /forgot-password with unknown email → friendly 'sent' "
       "(anti-enumeration)")
def _():
    from flask import current_app
    client = current_app.test_client()
    r = client.post("/forgot-password", data={
        "email": "nonexistent-9999@x.test",
    }, follow_redirects=False)
    assert r.status_code == 302, f"got {r.status_code}"
    assert "/login" in r.headers["Location"]
    return "unknown email returns same response as known"


@check("8. Reset with weak new password is rejected")
def _():
    from flask import current_app
    from app.services.permissions import generate_password_reset_token
    from app.models import User
    u = _mk_user("lockout-d@x.test", "Original1")
    tok = generate_password_reset_token(u)
    client = current_app.test_client()
    r = client.post(f"/reset-password/{tok}", data={
        "password": "weak",
        "confirm": "weak",
    }, follow_redirects=False)
    # Weak → re-render form (200) with a flash; NOT 302 to login.
    assert r.status_code == 200
    db.session.expire_all()
    u = db.session.get(User, u.id)
    assert u.check_password("Original1"), \
        "password should NOT have changed"
    return "weak pw rejected, hash unchanged"


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
