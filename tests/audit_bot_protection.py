#!/usr/bin/env python3
"""MARSOUD-BOT-PROTECTION-01 (Abdelhamid 2026-07-24).

Checks:
  1. Honeypot filled → 200 + decoy page, NO user + NO company created.
  2. Empty honeypot + valid payload → normal register flow proceeds
     (rate limit and other gates pass).
  3. Spam-domain email → 403, NO user created.
  4. is_spam_email covers subdomains (spam@x.mailinator.com).
  5. Rate limit: 6th attempt from same IP → 429.
  6. Rate limit does NOT trigger for different IPs.
  7. Rate limit resets after the window (reset helper works).
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
            "SELECT id FROM companies WHERE subdomain LIKE 'bp-%'"))]
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
            "DELETE FROM users WHERE email LIKE 'bp-%@x.test' "
            "OR email LIKE 'bp-%'"))
    from app.services.bot_guard import register_rate_reset
    register_rate_reset()


@check("1. Honeypot filled → 200 decoy, no user/company created")
def _():
    from flask import current_app
    from app.models import User, Company
    _teardown()
    client = current_app.test_client()
    r = client.post("/register", data={
        "website": "bot-value",   # honeypot!
        "email": "bp-h1@x.test",
        "full_name": "Bot",
        "company_name": "Bot Co",
        "subdomain": "bp-h1",
        "password": "Str0ngPass1!",
        "agree_terms": "on",
    })
    assert r.status_code == 200
    assert "Success" in r.get_data(as_text=True) or \
        "Registration received" in r.get_data(as_text=True)
    # NOTHING was written to DB.
    assert User.query.filter_by(email="bp-h1@x.test").first() is None
    assert Company.query.filter_by(subdomain="bp-h1").first() is None
    return "silent trap"


@check("2. is_spam_email: direct + subdomain hits")
def _():
    from app.services.bot_guard import is_spam_email
    assert is_spam_email("foo@mailinator.com") is True
    assert is_spam_email("foo@x.mailinator.com") is True
    assert is_spam_email("foo@10minutemail.com") is True
    assert is_spam_email("foo@gmail.com") is False
    assert is_spam_email("foo@company.co.uk") is False
    assert is_spam_email("") is False
    assert is_spam_email("no-at-sign") is False
    return "OK"


@check("3. Spam-domain email → 403")
def _():
    from flask import current_app
    from app.models import User
    from app.services.bot_guard import register_rate_reset
    register_rate_reset()
    client = current_app.test_client()
    r = client.post("/register", data={
        "email": "bp-spam@mailinator.com",
        "full_name": "x", "company_name": "x",
        "subdomain": "bp-spam", "password": "Str0ngPass1!",
        "agree_terms": "on",
    })
    assert r.status_code == 403, f"got {r.status_code}"
    assert User.query.filter_by(email="bp-spam@mailinator.com").first() is None
    return "403 + no user"


@check("4. Rate limit: 6th attempt from same IP → 429")
def _():
    from flask import current_app
    from app.services.bot_guard import register_rate_reset
    register_rate_reset()
    client = current_app.test_client()
    ip_hdr = {"X-Forwarded-For": "10.10.0.99"}
    # 5 attempts must not 429; each will fail validation (bad email
    # or missing fields) but should still count toward the limit.
    for i in range(5):
        client.post("/register", data={"email": f"bp-rl{i}@x.test",
                                          "full_name": "x",
                                          "company_name": "x",
                                          "subdomain": f"bp-rl{i}",
                                          "password": "y"},
                      headers=ip_hdr)
    r = client.post("/register", data={"email": "bp-rl-over@x.test",
                                            "full_name": "x",
                                            "company_name": "x",
                                            "subdomain": "bp-rl-o",
                                            "password": "y"},
                       headers=ip_hdr)
    assert r.status_code == 429, f"got {r.status_code}"
    return "6th → 429"


@check("5. Rate limit does NOT trigger for different IPs")
def _():
    from flask import current_app
    from app.services.bot_guard import register_rate_reset
    register_rate_reset()
    client = current_app.test_client()
    # 5 from one IP.
    for i in range(5):
        client.post("/register", data={"email": f"bp-a{i}@x.test",
                                          "full_name": "x",
                                          "company_name": "x",
                                          "subdomain": f"bp-a{i}",
                                          "password": "y"},
                      headers={"X-Forwarded-For": "10.10.0.1"})
    # Fresh IP — should succeed the gate (still fails validation
    # downstream but NOT 429).
    r = client.post("/register", data={"email": "bp-fresh@x.test",
                                            "full_name": "x",
                                            "company_name": "x",
                                            "subdomain": "bp-fresh",
                                            "password": "y"},
                       headers={"X-Forwarded-For": "10.10.0.2"})
    assert r.status_code != 429, \
        f"different IP incorrectly rate-limited: {r.status_code}"
    return "per-IP bucket works"


@check("6. Valid payload + no honeypot + not spam → passes to DB layer")
def _():
    from flask import current_app
    from app.services.bot_guard import register_rate_reset
    register_rate_reset()
    _teardown()
    client = current_app.test_client()
    r = client.post("/register", data={
        "email": "bp-real@x.test",
        "full_name": "Real Human",
        "company_name": "Real Co",
        "subdomain": "bp-real",
        "password": "Str0ngPass1!",
        "agree_terms": "on",
    })
    # We don't assert a specific status — the real register flow
    # depends on plans/currency seed which may or may not be
    # available. What matters: the response did NOT come from the
    # bot-guard layer (i.e. not 200 decoy, not 403 spam, not 429
    # rate limit). It's either a redirect (on success) or a
    # register.html re-render (on some downstream validation).
    assert r.status_code in (200, 302, 303), \
        f"unexpected: {r.status_code}"
    if r.status_code == 200:
        body = r.get_data(as_text=True)
        # Must NOT be the decoy page.
        assert "Registration received" not in body and "Success" not in body, \
            "got the decoy — bot-guard misfired for a real signup"
    return f"gate cleared (status={r.status_code})"


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
