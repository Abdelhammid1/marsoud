#!/usr/bin/env python3
"""MARSOUD-PRIVACY-TERMS-UPDATE-01 (Abdelhamid 2026-08-01).

Batch 9 Ticket 3. The re-consent screen (`/re-accept-terms`)
used to dump the full HTML of the terms + privacy docs inline
in scrollable boxes. User wants a short notice + links to
/terms and /privacy + a checkbox instead. Consent recording
logic stays the same.

Checks:
  1. Rendered template does NOT contain the full terms HTML.
  2. Rendered template contains a link to /terms.
  3. Rendered template contains a link to /privacy.
  4. Rendered template has the updated Arabic checkbox copy.
  5. POST with the checkbox → user's terms_version updates.
  6. POST without the checkbox → refused with flash, no version
     bump.
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
    db.session.expunge_all()
    db.session.remove()
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'rct-%@x.test'"))
        conn.execute(text(
            "DELETE FROM platform_settings WHERE key = 'terms_content_html' "
            "AND value LIKE '<h1>RCT-TEST%'"))


def _seed_user(current_version="TEST-INITIAL"):
    from app.models import User, UserStatus
    from werkzeug.security import generate_password_hash
    u = User(email="rct-owner@x.test",
             password_hash=generate_password_hash(
                 "TestPass123!", method="pbkdf2:sha256"),
             full_name="rct-owner", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_accepted_at=datetime.utcnow(),
             terms_version=current_version)
    db.session.add(u); db.session.commit()
    return u


def _bump_terms_version(new_version="TEST-NEW-V2"):
    """Force the platform's current terms version to a new value
    so re-consent kicks in for the seeded user."""
    from app.services.legal import set_legal
    set_legal(version=new_version,
              terms_html="<h1>RCT-TEST unique marker</h1>"
              "<p>Full terms body — should NOT appear on the "
              "reaccept screen anymore.</p>",
              privacy_html="<h1>Privacy</h1>")


@check("1. Rendered template does NOT contain the full terms HTML")
def _():
    from flask import current_app
    _teardown()
    u = _seed_user(current_version="TEST-OLD")
    _bump_terms_version("TEST-NEW-V2")
    with current_app.test_client() as client:
        with client.session_transaction() as sess:
            sess.clear()
            sess["_user_id"] = str(u.id)
            sess["_fresh"] = True
        r = client.get("/re-accept-terms")
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)
    assert "RCT-TEST unique marker" not in body, \
        "reaccept still dumps full terms HTML inline"
    assert "Full terms body" not in body
    return "no full terms HTML embedded"


@check("2. Template contains a link to /terms")
def _():
    from flask import current_app, url_for
    _teardown()
    u = _seed_user(current_version="TEST-OLD")
    _bump_terms_version("TEST-NEW-V2")
    with current_app.test_client() as client:
        with client.session_transaction() as sess:
            sess.clear()
            sess["_user_id"] = str(u.id)
            sess["_fresh"] = True
        r = client.get("/re-accept-terms")
    body = r.get_data(as_text=True)
    with current_app.test_request_context():
        assert url_for('public.terms') in body, \
            "no link to /terms in reaccept template"
    return "/terms link present"


@check("3. Template contains a link to /privacy")
def _():
    from flask import current_app, url_for
    _teardown()
    u = _seed_user(current_version="TEST-OLD")
    _bump_terms_version("TEST-NEW-V2")
    with current_app.test_client() as client:
        with client.session_transaction() as sess:
            sess.clear()
            sess["_user_id"] = str(u.id)
            sess["_fresh"] = True
        r = client.get("/re-accept-terms")
    body = r.get_data(as_text=True)
    with current_app.test_request_context():
        assert url_for('public.privacy') in body, \
            "no link to /privacy in reaccept template"
    return "/privacy link present"


@check("4. Template has the updated Arabic checkbox copy")
def _():
    from flask import current_app
    _teardown()
    u = _seed_user(current_version="TEST-OLD")
    _bump_terms_version("TEST-NEW-V2")
    with current_app.test_client() as client:
        with client.session_transaction() as sess:
            sess.clear()
            sess["_user_id"] = str(u.id)
            sess["_fresh"] = True
        r = client.get("/re-accept-terms")
    body = r.get_data(as_text=True)
    assert "أُقرّ بأنني اطلعت" in body or "أقر بأنني اطلعت" in body, \
        "updated Arabic checkbox copy missing"
    assert 'name="agree_terms"' in body
    return "checkbox + Arabic copy present"


@check("5. POST with checkbox → terms_version updates")
def _():
    from flask import current_app
    from app.models import User
    _teardown()
    u = _seed_user(current_version="TEST-OLD")
    _bump_terms_version("TEST-NEW-V2")
    u_id = u.id
    with current_app.test_client() as client:
        with client.session_transaction() as sess:
            sess.clear()
            sess["_user_id"] = str(u_id)
            sess["_fresh"] = True
        r = client.post("/re-accept-terms",
                          data={"agree_terms": "on"},
                          follow_redirects=False)
    assert r.status_code in (302, 303)
    db.session.expire_all()
    fresh = db.session.get(User, u_id)
    assert fresh.terms_version == "TEST-NEW-V2", \
        f"terms_version={fresh.terms_version!r}, want TEST-NEW-V2"
    return f"user upgraded to {fresh.terms_version}"


@check("6. POST without checkbox → refused, no version bump")
def _():
    from flask import current_app
    from app.models import User
    _teardown()
    u = _seed_user(current_version="TEST-OLD")
    _bump_terms_version("TEST-NEW-V2")
    u_id = u.id
    with current_app.test_client() as client:
        with client.session_transaction() as sess:
            sess.clear()
            sess["_user_id"] = str(u_id)
            sess["_fresh"] = True
        r = client.post("/re-accept-terms",
                          data={},
                          follow_redirects=False)
    assert r.status_code in (302, 303)
    db.session.expire_all()
    fresh = db.session.get(User, u_id)
    assert fresh.terms_version == "TEST-OLD", \
        f"terms_version leaked without agreement: {fresh.terms_version}"
    return "no bump without agreement"


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
