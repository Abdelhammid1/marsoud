#!/usr/bin/env python3
"""MARSOUD-TERMS-CONSENT (Abdelhamid 2026-07-22).

Mandatory acceptance at signup + super-admin content editor + version-
bump forces re-prompt on next request.

Checks:
  1. /register without agree_terms → rejected, no user created.
  2. /register with agree_terms → user.terms_accepted_at set +
     user.terms_version equals current version.
  3. Super-admin POST /admin/legal persists version + HTML.
  4. Public /terms + /privacy render the stored HTML.
  5. Middleware: user with stored version != current gets redirected
     to /re-accept-terms on next request.
  6. Middleware: user with matching version is unaffected.
  7. POST /re-accept-terms records the new version and unblocks.
  8. Version bump → previously-accepted user is re-prompted.
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
        target_cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__TC_%__'"))]
        for cid in target_cids:
            conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"),
                          {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                                  {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"), {"c": cid})
        conn.execute(text(
            "DELETE FROM user_companies WHERE user_id IN "
            "(SELECT id FROM users WHERE email LIKE 'tc-%@x.test')"))
        conn.execute(text(
            "DELETE FROM employees WHERE email LIKE 'tc-%@x.test'"))
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'tc-%@x.test'"))
        conn.execute(text(
            "DELETE FROM platform_settings WHERE key IN "
            "('terms_version','terms_content_html','privacy_content_html')"))
        # Orphan sweep for stale company-scoped rows.
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id NOT IN "
                    "(SELECT id FROM companies)"))


def _mk_verified_user(email, cid=None):
    """Create a user with ACTIVE status + verified email so the other
    middlewares don't get in the way of this audit."""
    from app.models import User, UserStatus
    from werkzeug.security import generate_password_hash
    u = User(email=email,
             password_hash=generate_password_hash("Passw0rd1", method="pbkdf2:sha256"),
             full_name=email, is_active=True,
             is_superadmin=False,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow())
    db.session.add(u); db.session.flush()
    if cid:
        from app.models.user import user_companies
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=cid, role="owner"))
    db.session.commit()
    return u


def _login(uid, cid=None):
    from flask import current_app, g
    for k in ("_login_user", "active_company"):
        try: g.pop(k, None)
        except Exception: pass
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
        if cid:
            sess["active_company_id"] = cid
    return client


@check("1. /register without agree_terms → rejected, no user created")
def _():
    from flask import current_app
    from app.models import User, Company
    _teardown()
    client = current_app.test_client()
    r = client.post("/register", data={
        "email": "tc-noagree@x.test",
        "full_name": "No Agree",
        "password": "Passw0rd1",
        "company_name": "__TC_NOAGREE__",
        "subdomain": "tc-noagree",
        "base_currency": "EGP",
        # agree_terms NOT sent.
    }, follow_redirects=False)
    assert User.query.filter_by(email="tc-noagree@x.test").first() is None
    assert Company.query.filter_by(name="__TC_NOAGREE__").first() is None
    return "no user, no company"


@check("2. /register with agree_terms → consent audit trail written")
def _():
    from flask import current_app
    from app.models import User
    from app.services.legal import get_terms_version
    _teardown()
    client = current_app.test_client()
    r = client.post("/register", data={
        "email": "tc-signup@x.test",
        "full_name": "Signup",
        "password": "Passw0rd1",
        "company_name": "__TC_SIGNUP__",
        "subdomain": "tc-signup",
        "base_currency": "EGP",
        "agree_terms": "on",
    }, follow_redirects=False)
    u = User.query.filter_by(email="tc-signup@x.test").one()
    assert u.terms_accepted_at is not None
    assert u.terms_version == get_terms_version()
    return f"accepted at {u.terms_accepted_at.isoformat()} v={u.terms_version}"


@check("3. Super-admin POST /admin/legal persists version + HTML")
def _():
    from flask import current_app
    from app.services.legal import get_terms_version, get_terms_html, get_privacy_html
    from werkzeug.security import generate_password_hash
    from app.models import User
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE email = 'tc-super@x.test'"))
    admin = User(email="tc-super@x.test",
                 password_hash=generate_password_hash("x", method="pbkdf2:sha256"),
                 full_name="tc-super", is_superadmin=True, is_active=True)
    db.session.add(admin); db.session.commit()

    client = _login(admin.id)
    r = client.post("/admin/legal", data={
        "terms_version": "v2.0-test",
        "terms_html": "<p>Test terms body ✅</p>",
        "privacy_html": "<p>Test privacy body 🔒</p>",
    }, follow_redirects=False)
    assert r.status_code == 302, f"POST → {r.status_code}"
    assert get_terms_version() == "v2.0-test"
    assert "Test terms body" in get_terms_html()
    assert "Test privacy body" in get_privacy_html()
    _STATE["admin_id"] = admin.id
    return "version + HTML persisted"


@check("4. Public /terms + /privacy render the stored HTML")
def _():
    from flask import current_app
    client = current_app.test_client()
    r = client.get("/terms")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Test terms body" in body
    assert "v2.0-test" in body
    r = client.get("/privacy")
    assert r.status_code == 200
    assert "Test privacy body" in r.get_data(as_text=True)
    return "both pages render the seeded HTML"


@check("5. User with stale terms_version → redirected to /re-accept-terms")
def _():
    from flask import current_app
    from app.models import Company
    from app.services.subscription import activate_default_subscription
    from app.services.seed_coa import seed_default_coa
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM companies WHERE name = '__TC_STALE__'"))
    c = Company(name="__TC_STALE__", base_currency="EGP", subdomain="tc-stale")
    activate_default_subscription(c)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = _mk_verified_user("tc-stale@x.test", cid=c.id)
    u.terms_version = "v0.9-old"       # stale
    u.terms_accepted_at = datetime.utcnow()
    db.session.commit()

    client = _login(u.id, cid=c.id)
    r = client.get("/home", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers.get("Location", "")
    assert "re-accept-terms" in loc, f"expected reaccept, got {loc}"
    _STATE["stale_uid"] = u.id
    _STATE["stale_cid"] = c.id
    return "stale-version user redirected"


@check("6. User with matching current version is unaffected")
def _():
    from flask import current_app
    from app.models import User
    from app.services.legal import get_terms_version
    u = db.session.get(User, _STATE["stale_uid"])
    u.terms_version = get_terms_version()   # up-to-date now
    db.session.commit()
    client = _login(u.id, cid=_STATE["stale_cid"])
    r = client.get("/home", follow_redirects=False)
    if r.status_code == 302:
        loc = r.headers.get("Location", "")
        assert "re-accept-terms" not in loc, \
            f"up-to-date user still redirected to {loc}"
    return "current-version user passes"


@check("7. POST /re-accept-terms updates version + unblocks")
def _():
    from flask import current_app
    from app.models import User
    from app.services.legal import get_terms_version
    u = db.session.get(User, _STATE["stale_uid"])
    u.terms_version = "still-old"
    db.session.commit()

    client = _login(u.id, cid=_STATE["stale_cid"])
    r = client.post("/re-accept-terms", data={
        "agree_terms": "on",
    }, follow_redirects=False)
    assert r.status_code == 302
    db.session.expire_all()
    u = db.session.get(User, u.id)
    assert u.terms_version == get_terms_version(), \
        f"version = {u.terms_version}"
    return "accepted the new version"


@check("8. Version bump → previously-accepted user is re-prompted")
def _():
    from flask import current_app
    from app.services.legal import set_legal
    from app.models import User
    # Bump the version.
    set_legal("v3.0-newer", "<p>newer terms</p>", "<p>newer privacy</p>")
    db.session.commit()
    u = db.session.get(User, _STATE["stale_uid"])
    client = _login(u.id, cid=_STATE["stale_cid"])
    r = client.get("/home", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers.get("Location", "")
    assert "re-accept-terms" in loc, f"got {loc}"
    return "version bump re-prompts"


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
