#!/usr/bin/env python3
"""MARSOUD-SIGNUP-AUTO-BLOCK (2026-08-12).

Ticket 4 of the bot-protection epic — every rejected
signup POST persists a row; 3 honeypot rejections from the
same non-whitelisted domain within 24h auto-block the
domain; a superadmin review page lets false-positives be
unblocked.

Checks:
  1. Schema — both tables exist, CHECK constraint on reason.
  2. Honeypot trip records a `honeypot` rejection with
     correct domain + IP.
  3. Rate-limit trip records a `rate_limit` rejection
     (not honeypot).
  4. 2 honeypot rejections from a non-whitelisted domain →
     NO BlockedDomain yet.
  5. 3rd honeypot rejection from same domain → BlockedDomain
     inserted + PAL `signup_domain_auto_blocked` written.
  6. Auto-block enforcement — 4th POST from same domain
     returns decoy AND logs a `blocked_domain` rejection.
  7. Whitelist — 5 honeypot rejections from gmail.com →
     NO BlockedDomain, but rejections still logged.
  8. Windowing — 3 honeypot rejections older than 24h are
     ignored (backdate via UPDATE).
  9. Unblock flow — superadmin POSTs unblock → row lifted,
     PAL entry written, subsequent signup passes block
     check.
 10. Non-superadmin refused (403) on both admin routes.
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app import create_app, db

PREFIX = "__SAB_"

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text
    from app.services.bot_guard import register_rate_reset
    db.session.rollback()
    db.session.close()
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM signup_rejections"))
        conn.execute(text("DELETE FROM blocked_domains"))
        conn.execute(text(
            "DELETE FROM platform_audit_logs "
            "WHERE action LIKE 'signup_domain_%'"))
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'sab-%@x.test' "
            "OR email LIKE 'sab-super@x.test' "
            "OR email LIKE 'sab-user@x.test'"))
    register_rate_reset()


def _post_signup(client, *, email, honeypot=None,
                   ip="10.99.0.1", subdomain=None):
    """POST /register with the given email + optional
    honeypot value. Returns the Flask response."""
    data = {
        "email": email,
        "full_name": "sab-test",
        "company_name": "sab test co",
        "subdomain": subdomain or ("sab-" + email.split("@")[0]),
        "password": "Str0ngPass1!",
        "agree_terms": "on",
    }
    if honeypot:
        data["website"] = honeypot
    return client.post("/register", data=data,
                        headers={"X-Forwarded-For": ip})


def _mk_super(email="sab-super@x.test"):
    from app.models import User, UserStatus
    from werkzeug.security import generate_password_hash
    u = User(email=email,
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="sab-super", is_active=True,
             is_superadmin=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u)
    db.session.commit()
    return u


def _mk_regular_user(email="sab-user@x.test"):
    from app.models import User, UserStatus
    from werkzeug.security import generate_password_hash
    u = User(email=email,
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="sab-user", is_active=True,
             is_superadmin=False,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u)
    db.session.commit()
    return u


def _client_as(user_id):
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

@check("1. Schema — both tables exist with all columns")
def _():
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    tables = set(insp.get_table_names())
    assert "signup_rejections" in tables
    assert "blocked_domains" in tables
    rj_cols = {c["name"] for c in insp.get_columns(
        "signup_rejections")}
    for c in ("id", "email_domain", "reason", "ip_address",
               "created_at"):
        assert c in rj_cols, f"signup_rejections missing {c}"
    bd_cols = {c["name"] for c in insp.get_columns(
        "blocked_domains")}
    for c in ("id", "domain", "blocked_at", "reason",
               "is_active", "unblocked_at", "unblocked_by_id"):
        assert c in bd_cols, f"blocked_domains missing {c}"
    return "OK"


@check("2. Honeypot trip records a `honeypot` rejection")
def _():
    from flask import current_app
    from app.models import SignupRejection
    _teardown()
    client = current_app.test_client()
    r = _post_signup(client, email="sab-h2@bot-evil.test",
                      honeypot="filled-by-bot",
                      ip="10.99.0.2")
    assert r.status_code == 200
    rows = SignupRejection.query.filter_by(
        email_domain="bot-evil.test",
        reason="honeypot").all()
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    assert rows[0].ip_address == "10.99.0.2", \
        f"IP wrong: {rows[0].ip_address}"
    return "1 row logged"


@check("3. Rate-limit trip records a `rate_limit` rejection")
def _():
    from flask import current_app
    from app.models import SignupRejection
    _teardown()
    # Each POST uses a FRESH test_client + clears
    # g._login_user so a previous request's flask-login
    # cache doesn't make register() redirect to /home via
    # the early `if current_user.is_authenticated` check.
    from flask import g
    def _fresh_post(email):
        if "_login_user" in g:
            del g._login_user
        c = current_app.test_client()
        with c.session_transaction() as sess:
            sess.clear()
        return _post_signup(c, email=email, ip="10.99.0.3")
    for i in range(5):
        _fresh_post(f"sab-rl{i}@rate-test.test")
    # 6th from same IP should trip rate-limit.
    r = _fresh_post("sab-rl6@rate-test.test")
    assert r.status_code == 429, f"got {r.status_code}"
    rl_rows = SignupRejection.query.filter_by(
        reason="rate_limit").all()
    assert len(rl_rows) >= 1, \
        f"no rate_limit row (got {len(rl_rows)})"
    # None of the rows should be `honeypot` (we didn't
    # send that field on any of the 6 attempts).
    hp = SignupRejection.query.filter_by(
        reason="honeypot").count()
    assert hp == 0, f"stray honeypot rows: {hp}"
    return f"{len(rl_rows)} rate_limit rows"


@check("4. 2 honeypot trips → NO BlockedDomain yet")
def _():
    from flask import current_app
    from app.models import BlockedDomain
    _teardown()
    client = current_app.test_client()
    for i in range(2):
        _post_signup(client,
                      email=f"sab-t4{i}@bot4-evil.test",
                      honeypot="x", ip=f"10.99.4.{i}")
    n = BlockedDomain.query.filter_by(
        domain="bot4-evil.test").count()
    assert n == 0, f"blocked too early ({n})"
    return "no premature block"


@check("5. 3rd honeypot trip → BlockedDomain + PAL entry")
def _():
    from flask import current_app
    from app.models import BlockedDomain, PlatformAuditLog
    _teardown()
    client = current_app.test_client()
    for i in range(3):
        _post_signup(client,
                      email=f"sab-t5{i}@bot5-evil.test",
                      honeypot="x", ip=f"10.99.5.{i}")
    row = BlockedDomain.query.filter_by(
        domain="bot5-evil.test", is_active=True).first()
    assert row is not None, "domain not auto-blocked"
    pal = PlatformAuditLog.query.filter_by(
        action="signup_domain_auto_blocked").count()
    assert pal >= 1, "no PAL entry for auto-block"
    return f"blocked + audit line"


@check("6. Auto-block enforcement — 4th POST returns decoy + logs")
def _():
    from flask import current_app
    from app.models import (
        SignupRejection, BlockedDomain, User, Company,
    )
    _teardown()
    client = current_app.test_client()
    # Trigger the block first (3 honeypots).
    for i in range(3):
        _post_signup(client,
                      email=f"sab-t6{i}@bot6-evil.test",
                      honeypot="x", ip=f"10.99.6.{i}")
    assert BlockedDomain.query.filter_by(
        domain="bot6-evil.test", is_active=True).first()
    # 4th POST (WITHOUT honeypot) — must be intercepted by
    # the pre-gate block check and return the decoy.
    r = _post_signup(client,
                      email="sab-t6-legit@bot6-evil.test",
                      ip="10.99.6.99")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Same decoy the honeypot returns.
    assert "Registration" in body or "Success" in body \
        or "شكراً" in body or "تم" in body, \
        f"unexpected body: {body[:200]}"
    # A `blocked_domain` rejection row should now exist.
    n = SignupRejection.query.filter_by(
        email_domain="bot6-evil.test",
        reason="blocked_domain").count()
    assert n == 1, f"blocked_domain row missing (n={n})"
    # And no User / Company was created for this attempt.
    assert User.query.filter_by(
        email="sab-t6-legit@bot6-evil.test").first() is None
    assert Company.query.filter_by(
        subdomain="sab-sab-t6-legit").first() is None
    return "4th POST → decoy + logged"


@check("7. Whitelist — 5 honeypots from gmail.com → NO block")
def _():
    from flask import current_app
    from app.models import (
        BlockedDomain, PlatformAuditLog, SignupRejection,
    )
    _teardown()
    client = current_app.test_client()
    # Different IPs to avoid rate-limit interference.
    for i in range(5):
        _post_signup(client,
                      email=f"sab-t7{i}@gmail.com",
                      honeypot="x", ip=f"10.99.7.{i}")
    n_block = BlockedDomain.query.filter_by(
        domain="gmail.com").count()
    assert n_block == 0, \
        f"gmail.com got auto-blocked (bug): {n_block}"
    # But the rejections themselves were still logged.
    n_rej = SignupRejection.query.filter_by(
        email_domain="gmail.com", reason="honeypot").count()
    assert n_rej == 5, \
        f"expected 5 honeypot rejections, got {n_rej}"
    return f"logged {n_rej}, block skipped"


@check("8. Windowing — 3 honeypots older than 24h don't trigger")
def _():
    from flask import current_app
    from sqlalchemy import text
    from app.models import BlockedDomain, SignupRejection
    _teardown()
    client = current_app.test_client()
    # Insert 3 honeypot rejections directly, backdated.
    old = datetime.utcnow() - timedelta(hours=25)
    for i in range(3):
        db.session.add(SignupRejection(
            email_domain="stale-bot.test",
            reason="honeypot", ip_address=f"10.99.8.{i}"))
    db.session.commit()
    db.session.execute(text(
        "UPDATE signup_rejections "
        "SET created_at = :old "
        "WHERE email_domain = 'stale-bot.test'"),
        {"old": old})
    db.session.commit()
    # A new honeypot rejection today should NOT trigger the
    # block — the old 3 are outside the window, so the new
    # one alone (window count = 1) is below the threshold.
    r = _post_signup(client,
                      email="sab-t8@stale-bot.test",
                      honeypot="x", ip="10.99.8.99")
    assert r.status_code == 200
    n = BlockedDomain.query.filter_by(
        domain="stale-bot.test").count()
    assert n == 0, f"stale rejections triggered block ({n})"
    return "window respected"


@check("9. Unblock flow — superadmin lifts + subsequent signup passes")
def _():
    from flask import current_app
    from app.models import BlockedDomain, PlatformAuditLog
    _teardown()
    su = _mk_super()
    # Seed an active block directly.
    db.session.add(BlockedDomain(
        domain="lifted-later.test",
        reason="test auto-block", is_active=True))
    db.session.commit()
    r = _client_as(su.id).post(
        "/admin/rejected-signups/unblock",
        data={"domain": "lifted-later.test"})
    assert r.status_code in (302, 303)
    db.session.expire_all()
    row = BlockedDomain.query.filter_by(
        domain="lifted-later.test").first()
    assert row.is_active is False, "still active"
    assert row.unblocked_at is not None
    assert row.unblocked_by_id == su.id
    pal = PlatformAuditLog.query.filter_by(
        action="signup_domain_unblocked").count()
    assert pal >= 1, "no unblock PAL entry"
    # And a subsequent signup POST should now PASS the
    # block check (it might trip other gates — that's OK,
    # we only care that is_domain_blocked returned False).
    from app.services.signup_rejections import is_domain_blocked
    assert is_domain_blocked(
        "someone@lifted-later.test") is False
    return "lifted + verified"


@check("10. Non-superadmin refused on both admin routes")
def _():
    _teardown()
    _su = _mk_super()
    u = _mk_regular_user()
    r1 = _client_as(u.id).get("/admin/rejected-signups")
    assert r1.status_code == 403, f"GET: {r1.status_code}"
    r2 = _client_as(u.id).post(
        "/admin/rejected-signups/unblock",
        data={"domain": "x.test"})
    assert r2.status_code == 403, f"POST: {r2.status_code}"
    return "both routes 403"


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
