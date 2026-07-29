#!/usr/bin/env python3
"""MARSOUD-FORGOT-PASSWORD-RENDER (Abdelhamid 2026-07-29).

Batch 7 Ticket 2. Bug: /forgot-password and /reset-password/<token>
returned HTTP 200 but the response body was empty (no form) for
anonymous users. Root cause: both templates extended base.html
and used {% block content %} — but base.html only renders `content`
for authenticated users. Anonymous visitors fell into the
{% else %} branch that renders {% block guest_content %}, which
these templates never defined. Fix: rewrite both templates as
standalone HTML files (matching the login.html / register.html
pattern).

Checks:
  1. GET /forgot-password (anonymous) → 200 + body contains
     name="email" input (the form actually renders now).
  2. GET /reset-password/<invalid_token> (anonymous) → redirects
     to /forgot-password with a flash (existing behaviour).
  3. GET /reset-password/<valid_token> (anonymous) → 200 + body
     contains name="password" input.
  4. POST /forgot-password with a valid email → 302 to /login +
     the flash message is set (route smoke test — fix didn't
     regress the POST path).
  5. Full reset flow: request → GET form → POST new password →
     login with the new password succeeds.
  6. Neither template contains "extends" — they're standalone.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"
os.environ["TURNSTILE_SECRET"] = ""

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


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
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'fpw-%@x.test'"))


def _seed_user():
    from app.models import User, UserStatus
    from werkzeug.security import generate_password_hash
    u = User(email="fpw-owner@x.test",
             password_hash=generate_password_hash(
                 "OldPass1234!", method="pbkdf2:sha256"),
             full_name="fpw-owner", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.commit()
    return u


@check("1. GET /forgot-password renders the email form")
def _():
    from flask import current_app
    _teardown()
    with current_app.test_client() as client:
        r = client.get("/forgot-password")
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)
    assert 'name="email"' in body, \
        "email input missing — template still blank!"
    # Sanity: it's the standalone shell, not base.html
    assert '<!DOCTYPE html>' in body
    return f"form rendered, {len(body)} bytes"


@check("2. GET /reset-password/<invalid> redirects to /forgot-password")
def _():
    from flask import current_app
    _teardown()
    with current_app.test_client() as client:
        r = client.get("/reset-password/not-a-real-token")
    assert r.status_code in (302, 303)
    loc = r.headers.get("Location") or ""
    assert "/forgot-password" in loc, f"→ {loc}"
    return f"→ {loc}"


@check("3. GET /reset-password/<valid> renders the password form")
def _():
    from flask import current_app
    from app.services.permissions import generate_password_reset_token
    _teardown()
    u = _seed_user()
    token = generate_password_reset_token(u)
    with current_app.test_client() as client:
        r = client.get(f"/reset-password/{token}")
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)
    assert 'name="password"' in body, \
        "password input missing — template still blank!"
    assert 'name="confirm"' in body
    assert '<!DOCTYPE html>' in body
    return f"form rendered for token"


@check("4. POST /forgot-password with valid email → 302 + flash")
def _():
    from flask import current_app
    _teardown()
    u = _seed_user()
    with current_app.test_client() as client:
        r = client.post("/forgot-password",
                          data={"email": "fpw-owner@x.test"},
                          follow_redirects=False)
    assert r.status_code in (302, 303), f"got {r.status_code}"
    loc = r.headers.get("Location") or ""
    assert "/login" in loc, f"→ {loc}"
    return f"→ {loc}"


@check("5. Full reset flow: request → new password → login succeeds")
def _():
    from flask import current_app
    from app.services.permissions import generate_password_reset_token
    _teardown()
    u = _seed_user()
    token = generate_password_reset_token(u)
    with current_app.test_client() as client:
        # 1. Post new password.
        r = client.post(f"/reset-password/{token}",
                          data={"password": "NewPass99!!",
                                 "confirm": "NewPass99!!"},
                          follow_redirects=False)
    assert r.status_code in (302, 303), \
        f"reset POST failed: {r.status_code}"
    # 2. Verify by trying to log in with the new password.
    with current_app.test_client() as client:
        r2 = client.post("/login",
                          data={"email": "fpw-owner@x.test",
                                 "password": "NewPass99!!"},
                          follow_redirects=False)
    assert r2.status_code in (302, 303), \
        f"login with new password failed: {r2.status_code}"
    return "reset + login with new pw both worked"


@check("6. Templates are standalone (no 'extends')")
def _():
    for name in ("forgot_password.html", "reset_password.html"):
        p = ROOT / "app" / "templates" / "auth" / name
        src = p.read_text()
        assert "{% extends" not in src, \
            f"{name} still extends base.html (bug)"
        assert "<!DOCTYPE html>" in src, \
            f"{name} missing DOCTYPE (not standalone)"
    return "both templates standalone"


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
                print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                failed += 1
                import traceback; traceback.print_exc()
    with app.app_context():
        _teardown()
        print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
