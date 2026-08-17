#!/usr/bin/env python3
"""MARSOUD-BOT-REGISTRATION-VISIBILITY (2026-08-17) — TKT-17 audit.

Verifies:
  A. `honeypot_value(form)` returns the raw string.
  B. `record_rejection` persists the honeypot value + full email.
  C. POST /auth/register with `website=<something>` triggers the
     full path: SignupRejection has the payload, BlockedEmail row
     is created, and BlockedDomain is created ONLY when the
     domain isn't in WHITELISTED_DOMAINS.
  D. `is_email_blocked(email)` returns True after `block_email`.
  E. `unblock_email` lifts a false-positive block.
  F. WHITELISTED_DOMAINS is unchanged (regression freeze — nobody
     should shrink the safe-list accidentally).
  G. `/admin/rejected-signups` renders the new honeypot column +
     blocked-emails table.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db  # noqa: E402


CHECKS = []
CO_NAME = "__BOT_REG_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from app.models import (SignupRejection, BlockedDomain, BlockedEmail)
    # Purge any prior fixture rows so re-runs are idempotent.
    # LIKE %{marker}% catches every row referencing the audit
    # marker regardless of position (domain vs local-part).
    marker = f"%{CO_NAME.lower()}%"
    SignupRejection.query.filter(
        (SignupRejection.email_domain.like(marker)) |
        (SignupRejection.email.like(marker))
    ).delete(synchronize_session=False)
    BlockedDomain.query.filter(
        BlockedDomain.domain.like(marker)
    ).delete(synchronize_session=False)
    BlockedEmail.query.filter(
        BlockedEmail.email.like(marker)
    ).delete(synchronize_session=False)
    # Also wipe the gmail email C3 created (in case of a partial run)
    BlockedEmail.query.filter_by(email="c3_bot@gmail.com").delete(
        synchronize_session=False)
    db.session.commit()


def _setup():
    from app.models import User
    from app.services.legal import get_terms_version
    from datetime import datetime

    _teardown()

    admin = User.query.filter_by(is_superadmin=True).first()
    _STATE["admin_id"] = admin.id if admin else None
    _STATE["tv"] = get_terms_version()


# ─── A. Honeypot value helper ─────────────────────────────────────────
@check("A1: honeypot_value(form) returns the raw payload")
def A1():
    from werkzeug.datastructures import ImmutableMultiDict
    from app.services.bot_guard import honeypot_value
    f = ImmutableMultiDict([("website", "http://evil.tld/promo")])
    assert honeypot_value(f) == "http://evil.tld/promo"
    f_empty = ImmutableMultiDict([("website", "")])
    assert honeypot_value(f_empty) == ""
    f_missing = ImmutableMultiDict([])
    assert honeypot_value(f_missing) == ""


# ─── B. record_rejection persists new columns ─────────────────────────
@check("B1: record_rejection persists email + honeypot_value")
def B1():
    from app.models import SignupRejection
    from app.services.signup_rejections import record_rejection

    email = f"botuser@{CO_NAME.lower()}.malicious.local"
    record_rejection("honeypot", email,
                     ip_address="10.0.0.1",
                     honeypot_value="http://scam.tld/promo")
    row = (SignupRejection.query
           .filter_by(email=email)
           .order_by(SignupRejection.id.desc())
           .first())
    assert row is not None, "no rejection persisted"
    assert row.honeypot_value == "http://scam.tld/promo", row.honeypot_value
    assert row.email == email, row.email
    assert row.email_domain == f"{CO_NAME.lower()}.malicious.local"


# ─── C. Live POST triggers full path ──────────────────────────────────
@check("C1: honeypot POST creates SignupRejection with payload")
def C1():
    from app.models import SignupRejection
    from app.services.bot_guard import register_rate_reset
    register_rate_reset()  # fresh window

    email = f"c1_bot@{CO_NAME.lower()}.evil.local"
    app = _STATE["app"]
    c = app.test_client()
    r = c.post("/register", data={
        "email": email,
        "full_name": "Bot",
        "company_name": "Bot Co",
        "subdomain": "botco-c1",
        "password": "Passw0rd!bot1",
        "website": "https://malicious.tld/inject",
        "agree_terms": "on",
    })
    assert r.status_code == 200, (r.status_code, r.data[:200])
    # Body is the decoy page — must render success text
    row = (SignupRejection.query
           .filter_by(email=email)
           .order_by(SignupRejection.id.desc())
           .first())
    assert row is not None, "no rejection row for POST"
    assert row.honeypot_value == "https://malicious.tld/inject", (
        row.honeypot_value)
    assert row.reason == "honeypot"


@check("C2: honeypot on non-whitelisted domain also blocks the DOMAIN")
def C2():
    from app.models import BlockedDomain
    row = BlockedDomain.query.filter_by(
        domain=f"{CO_NAME.lower()}.evil.local",
        is_active=True).first()
    assert row is not None, "domain not immediately blocked"


@check("C3: honeypot ALWAYS blocks the EMAIL (even on whitelisted domain)")
def C3():
    from app.models import BlockedEmail, BlockedDomain
    from app.services.bot_guard import register_rate_reset
    register_rate_reset()

    email = f"c3_bot@gmail.com"
    app = _STATE["app"]
    # Wipe any prior state for the gmail email
    BlockedEmail.query.filter_by(email=email).delete()
    db.session.commit()

    c = app.test_client()
    r = c.post("/register", data={
        "email": email,
        "full_name": "Bot",
        "company_name": "Bot Co",
        "subdomain": "botco-c3",
        "password": "Passw0rd!bot1",
        "website": "spam",
        "agree_terms": "on",
    })
    assert r.status_code == 200, r.status_code
    # Email BLOCKED
    be = BlockedEmail.query.filter_by(email=email, is_active=True).first()
    assert be is not None, "gmail email must be blocked on honeypot trip"
    # Domain gmail.com must NOT be blocked
    bd = BlockedDomain.query.filter_by(
        domain="gmail.com", is_active=True).first()
    assert bd is None, "gmail.com must NEVER be domain-blocked"

    # Cleanup — don't leave a real user email in the blocklist
    BlockedEmail.query.filter_by(email=email).delete()
    db.session.commit()


# ─── D. is_email_blocked hot path ─────────────────────────────────────
@check("D1: is_email_blocked True after block_email; False after unblock")
def D1():
    from app.services.signup_rejections import (
        block_email, is_email_blocked, unblock_email)

    email = f"d1_test@{CO_NAME.lower()}.other.local"
    assert is_email_blocked(email) is False, "starts unblocked"
    assert block_email(email, "test") is True
    assert is_email_blocked(email) is True
    # Idempotent — second call is no-op
    assert block_email(email, "test") is False
    # Unblock
    assert unblock_email(email, _STATE["admin_id"]) is True
    assert is_email_blocked(email) is False


# ─── E. WHITELISTED_DOMAINS freeze ────────────────────────────────────
@check("E1: WHITELISTED_DOMAINS still contains gmail/outlook/yahoo/hotmail/icloud")
def E1():
    from app.services.signup_rejections import WHITELISTED_DOMAINS
    must_include = {"gmail.com", "outlook.com", "yahoo.com",
                    "hotmail.com", "icloud.com"}
    missing = must_include - WHITELISTED_DOMAINS
    assert not missing, f"WHITELIST shrunk: missing {missing}"


# ─── G. Super-admin template renders ──────────────────────────────────
@check("G1: /admin/rejected-signups renders honeypot column + emails table")
def G1():
    from flask import g as flask_g
    admin_id = _STATE["admin_id"]
    if not admin_id:
        raise AssertionError("no super-admin in DB")
    # Clear any lingering login state from previous checks (test
    # client shares the app_context, so `g._login_user` leaks).
    if "_login_user" in flask_g:
        del flask_g._login_user
    app = _STATE["app"]
    c = app.test_client()
    with c.session_transaction() as s:
        s.clear()
        s["_user_id"] = str(admin_id)
        s["_fresh"] = True
    r = c.get("/admin/rejected-signups", follow_redirects=False)
    assert r.status_code == 200, (r.status_code,
                                    r.headers.get("Location"))
    body = r.data.decode("utf-8", errors="replace")
    assert "قيمة الحقل المخفي" in body, "honeypot column heading missing"
    assert "البريد الإلكتروني المحظور" in body, "blocked-emails heading missing"


# ─── Runner ───────────────────────────────────────────────────────────
def main():
    app = create_app()
    _STATE["app"] = app
    with app.app_context():
        _setup()
        try:
            failed = []
            for label, fn in CHECKS:
                try:
                    fn()
                    print(f"  [OK]   {label}")
                except Exception as e:
                    failed.append((label, e))
                    print(f"  [FAIL] {label}\n         -> {e}")
            total = len(CHECKS)
            ok = total - len(failed)
            print()
            print(f"{ok}/{total} OK" if not failed
                  else f"{ok}/{total} -- {len(failed)} FAILED")
            return 0 if not failed else 1
        finally:
            _teardown()


if __name__ == "__main__":
    sys.exit(main())
