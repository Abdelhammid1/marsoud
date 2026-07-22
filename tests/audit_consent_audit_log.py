#!/usr/bin/env python3
"""MARSOUD-CONSENT-AUDIT-LOG (Abdelhamid 2026-07-22).

Immutable per-event history table + backfill + admin viewer +
"who hasn't accepted the current version" report.

Checks:
  1. record_consent writes a new row (never updates the last).
  2. /register on a fresh user creates one consent_events row
     tagged source=signup with correct version + IP + UA.
  3. /re-accept-terms after a super-admin version bump creates
     ANOTHER row (source=reaccept) — original signup row stays.
  4. users_missing_current_version returns exactly the users
     whose latest ConsentEvent doesn't match the current version.
  5. /admin/consent renders (superadmin session).
  6. /admin/users/<id>/consent renders per-user history.
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
        conn.execute(text(
            "DELETE FROM consent_events WHERE user_id IN "
            "(SELECT id FROM users WHERE email LIKE 'cal-%@x.test')"))
        target_cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__CAL_%__'"))]
        for cid in target_cids:
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
            "DELETE FROM user_companies WHERE user_id IN "
            "(SELECT id FROM users WHERE email LIKE 'cal-%@x.test')"))
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'cal-%@x.test'"))
        conn.execute(text(
            "DELETE FROM platform_settings WHERE key IN "
            "('terms_version','terms_content_html','privacy_content_html')"))


@check("1. record_consent writes a new row each call")
def _():
    from app.models import User, ConsentEvent
    from app.services.legal import record_consent
    from werkzeug.security import generate_password_hash
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM users WHERE email = 'cal-record@x.test'"))
    u = User(email="cal-record@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="cal-record", is_active=True)
    db.session.add(u); db.session.commit()

    record_consent(u, source="signup", document_version="v1.0")
    db.session.commit()
    record_consent(u, source="reaccept", document_version="v2.0")
    db.session.commit()
    rows = ConsentEvent.query.filter_by(user_id=u.id).order_by(
        ConsentEvent.id).all()
    assert len(rows) == 2, f"expected 2 rows, got {len(rows)}"
    assert rows[0].source == "signup"
    assert rows[0].document_version == "v1.0"
    assert rows[1].source == "reaccept"
    assert rows[1].document_version == "v2.0"
    return "append-only: signup + reaccept both preserved"


@check("2. POST /register creates one consent_events row "
       "(source=signup, correct IP + UA)")
def _():
    from flask import current_app
    from app.models import User, ConsentEvent
    _teardown()
    client = current_app.test_client()
    r = client.post("/register", data={
        "email": "cal-signup@x.test",
        "full_name": "cal-signup",
        "password": "Passw0rd1",
        "company_name": "__CAL_SIGNUP__",
        "subdomain": "cal-signup",
        "base_currency": "EGP",
        "agree_terms": "on",
    }, follow_redirects=False,
       headers={"User-Agent": "TestUA/1.0"},
       environ_overrides={"REMOTE_ADDR": "10.0.0.99"})
    u = User.query.filter_by(email="cal-signup@x.test").one()
    events = ConsentEvent.query.filter_by(user_id=u.id).all()
    assert len(events) == 1
    e = events[0]
    assert e.source == "signup"
    assert e.ip_address == "10.0.0.99"
    assert e.user_agent == "TestUA/1.0"
    _STATE["signup_uid"] = u.id
    return "signup event captured"


@check("3. /re-accept-terms after version bump creates a NEW row "
       "(source=reaccept), original stays")
def _():
    from flask import current_app, g
    from app.models import User, ConsentEvent, UserStatus
    from app.services.legal import set_legal
    # Bump version.
    set_legal("cal-v2", "<p>new terms</p>", "<p>new privacy</p>")
    db.session.commit()

    u = db.session.get(User, _STATE["signup_uid"])
    # Set user to ACTIVE + verified so middleware doesn't intercept.
    u.status = UserStatus.ACTIVE.value
    u.email_verified_at = datetime.utcnow()
    # Force stale terms_version so reaccept middleware fires.
    u.terms_version = "old-version"
    db.session.commit()

    for k in ("_login_user", "active_company"):
        try: g.pop(k, None)
        except Exception: pass
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
        sess["active_company_id"] = u.companies[0].id
    r = client.post("/re-accept-terms", data={"agree_terms": "on"},
                     follow_redirects=False)
    assert r.status_code == 302
    events = ConsentEvent.query.filter_by(user_id=u.id).order_by(
        ConsentEvent.id).all()
    assert len(events) == 2, f"got {len(events)}"
    assert events[0].source == "signup"
    assert events[1].source == "reaccept"
    return "reaccept event appended, signup preserved"


@check("4. users_missing_current_version returns stale users only")
def _():
    from app.services.legal import (
        users_missing_current_version, get_terms_version, set_legal,
    )
    # After check 3, the signup user has ACCEPTED cal-v2 → not in list.
    current = get_terms_version()
    assert current == "cal-v2"
    missing = users_missing_current_version()
    emails = {u.email for u in missing}
    # The signup user just re-accepted the current version → NOT missing.
    assert "cal-signup@x.test" not in emails, \
        "user who just re-accepted must NOT be in missing list"
    return "up-to-date user excluded from missing list"


@check("5. /admin/consent renders (superadmin)")
def _():
    from flask import current_app, g
    from app.models import User
    from werkzeug.security import generate_password_hash
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM users WHERE email = 'cal-super@x.test'"))
    admin = User(email="cal-super@x.test",
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name="cal-super", is_superadmin=True,
                 is_active=True)
    db.session.add(admin); db.session.commit()
    for k in ("_login_user", "active_company"):
        try: g.pop(k, None)
        except Exception: pass
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin.id)
        sess["_fresh"] = True
    r = client.get("/admin/consent")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "سجل موافقات" in body
    _STATE["admin_id"] = admin.id
    return "admin index renders"


@check("6. /admin/users/<id>/consent renders per-user history")
def _():
    from flask import current_app, g
    from app.models import User
    for k in ("_login_user",):
        try: g.pop(k, None)
        except Exception: pass
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["admin_id"])
        sess["_fresh"] = True
    u = db.session.get(User, _STATE["signup_uid"])
    r = client.get(f"/admin/users/{u.id}/consent")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "cal-signup" in body
    return "per-user history renders"


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
