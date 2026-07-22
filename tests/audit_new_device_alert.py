#!/usr/bin/env python3
"""MARSOUD-NEW-DEVICE (Abdelhamid 2026-07-22).

`UserSession` at app/models/activity.py already captured device
metadata on every login but never notified the user. Now: first
login from a new device signature fires an email; repeat logins
from the same signature don't.

Signature = SHA-256(user_agent + ip_class), stored in
UserSession.session_token so an O(1) probe on every login tells us
"seen before?" without a separate table.

Checks:
  1. _device_signature is stable for the same (UA, IP) pair.
  2. First call to start_session for a fresh user → sends 1 email.
  3. Second call with SAME UA + SAME IP → no additional email.
  4. Third call with DIFFERENT UA → sends another email.
  5. Fourth call with SAME UA but DIFFERENT /24 → sends another
     (different ip_class → different signature).
  6. Email HTML contains browser + IP + login time.
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
_STATE = {"emails_sent": []}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM user_sessions WHERE user_id IN "
            "(SELECT id FROM users WHERE email LIKE 'newdev-%@x.test')"))
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'newdev-%@x.test'"))


def _mk_user(email):
    from app.models import User
    from werkzeug.security import generate_password_hash
    u = User(email=email,
             password_hash=generate_password_hash("x", method="pbkdf2:sha256"),
             full_name=email, is_active=True,
             email_verified_at=datetime.utcnow())
    db.session.add(u); db.session.commit()
    return u


def _fake_send_email(to, subject, html_body, **kwargs):
    _STATE["emails_sent"].append({
        "to": to, "subject": subject, "html": html_body,
    })
    return True


def _install_email_mock():
    import app.services.activity as _act
    from app.services import email as _email_mod
    _STATE["orig_send"] = _email_mod.send_email
    _email_mod.send_email = _fake_send_email


def _restore_email():
    if "orig_send" in _STATE:
        from app.services import email as _email_mod
        _email_mod.send_email = _STATE["orig_send"]


def _fake_request(ua, ip):
    """Push a request context with a specific User-Agent + REMOTE_ADDR
    so start_session's parse_user_agent + _client_ip see them."""
    from flask import current_app
    return current_app.test_request_context(
        headers={"User-Agent": ua},
        environ_overrides={"REMOTE_ADDR": ip},
    )


@check("1. _device_signature is deterministic per (UA, IP)")
def _():
    from app.services.activity import _device_signature
    a = _device_signature("Mozilla/5.0 Firefox", "10.0.0.5")
    b = _device_signature("Mozilla/5.0 Firefox", "10.0.0.5")
    c = _device_signature("Mozilla/5.0 Firefox", "192.168.0.5")
    d = _device_signature("Mozilla/5.0 Chrome",  "10.0.0.5")
    assert a == b, "same input → same sig"
    assert a != c, "different /24 → different sig"
    assert a != d, "different UA → different sig"
    return "hash stable + collision-shaped"


@check("2. First login for a fresh user → sends 1 email")
def _():
    _teardown()
    _STATE["emails_sent"] = []
    u = _mk_user("newdev-a@x.test")
    from app.services.activity import start_session
    with _fake_request("MozFirefox/1", "10.0.0.5"):
        start_session(u)
    assert len(_STATE["emails_sent"]) == 1, \
        f"expected 1 email, sent {len(_STATE['emails_sent'])}"
    _STATE["u_id"] = u.id
    return f"1 email fired"


@check("3. Second login SAME UA + SAME IP → no additional email")
def _():
    from app.models import User
    u = db.session.get(User, _STATE["u_id"])
    _STATE["emails_sent"] = []
    from app.services.activity import start_session
    with _fake_request("MozFirefox/1", "10.0.0.5"):
        start_session(u)
    assert len(_STATE["emails_sent"]) == 0, \
        f"expected 0 email, sent {len(_STATE['emails_sent'])}"
    return "no spam on repeat device"


@check("4. Third login DIFFERENT UA → sends another email")
def _():
    from app.models import User
    u = db.session.get(User, _STATE["u_id"])
    _STATE["emails_sent"] = []
    from app.services.activity import start_session
    with _fake_request("MozChrome/2", "10.0.0.5"):
        start_session(u)
    assert len(_STATE["emails_sent"]) == 1
    return "different UA fires new alert"


@check("5. Fourth login SAME UA but DIFFERENT /24 → sends another")
def _():
    from app.models import User
    u = db.session.get(User, _STATE["u_id"])
    _STATE["emails_sent"] = []
    from app.services.activity import start_session
    with _fake_request("MozFirefox/1", "203.0.113.5"):   # totally different subnet
        start_session(u)
    assert len(_STATE["emails_sent"]) == 1
    return "different /24 fires new alert"


@check("6. Email HTML contains browser + IP + login time")
def _():
    email = _STATE["emails_sent"][0]
    html = email["html"]
    assert "203.0.113" in html
    assert "Firefox" in html or "Moz" in html.lower(), \
        "expected browser info"
    # Any timestamp shape works — check for a hyphenated year-month-day.
    import re
    assert re.search(r"20\d\d-\d\d-\d\d", html), "expected timestamp"
    return "email carries browser/IP/time"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _install_email_mock()
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
            _restore_email()
            _teardown()
            print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
