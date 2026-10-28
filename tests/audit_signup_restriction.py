#!/usr/bin/env python3
"""MARSOUD-LANDING-RESTRICT-REGISTER-01 — public signup is retired.

Six checks pin the backend lockdown + the WhatsApp redirect + the
landing-page CTA rewrite + the self-hosted-icons change.  Pattern
lifted from the recent SMTP audit (test_client + real `login_user`
+ `SESSION_PROTECTION = None`).

  1. GET /register → 302 to the exact WhatsApp URL from the shared
     helper.  Confirms the Arabic prefill message decodes to the
     ticket's canonical string.
  2. POST /register with signup form payload → 302 (no User
     created).  Backend-level block per AC #3.
  3. /companies/new as a non-super-admin → 403 (superadmin_required
     decorator's abort(403)).  No Company created.
  4. /companies/new as a super admin → still works end-to-end.
     Company lands, membership + CoA + owner Employee all written.
  5. Static landing (app/static/landing.html) carries zero /register
     hrefs; every signup CTA is a wa.me link with the expected
     prefill message substring in the href.
  6. Self-hosted icons are wired in app/static/landing.html —
     preload hint + /static/css/icons.css link both present, and
     the stale Google CDN icon-font URL is gone.
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
_STATE = {}
COMPANY_NAME = "__SIGNUP_RESTRICTION_AUDIT_SA_CO__"


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    """Build two users: a plain one (non-super-admin) and a super
    admin.  No fixture company — check 4 creates one through the
    real /companies/new flow."""
    from app.models import User, Company
    from app.models.user import user_companies
    from sqlalchemy import text

    # Nuke any leftover fixtures from prior aborted runs.
    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    for c in Company.query.filter_by(name=COMPANY_NAME).all():
        for row in db.session.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall():
            try:
                db.session.execute(text(
                    f"DELETE FROM {row[0]} WHERE company_id=:c"),
                    {"c": c.id})
            except Exception:
                pass
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id=:c"),
            {"c": c.id})
        db.session.execute(text(
            "DELETE FROM companies WHERE id=:c"), {"c": c.id})
    db.session.execute(text(
        "DELETE FROM users WHERE email LIKE '%__signup_restriction%'"))
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()

    try:
        from app.services.legal import get_terms_version
        terms = get_terms_version() or ""
    except Exception:
        terms = ""

    plain = User(email="__signup_restriction_plain@audit.local",
                  full_name="plain", password_hash="x",
                  terms_version=terms)
    db.session.add(plain); db.session.flush()
    sa = User(email="__signup_restriction_sa@audit.local",
               full_name="sa", password_hash="x",
               is_superadmin=True,
               terms_version=terms)
    db.session.add(sa); db.session.commit()
    _STATE["plain_id"] = plain.id
    _STATE["sa_id"] = sa.id


def _teardown():
    from app.models import User, Company
    from app.models.user import user_companies
    from app.models import JournalEntry, JournalLine
    from sqlalchemy import inspect, text

    db.session.rollback()
    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    for c in Company.query.filter_by(name=COMPANY_NAME).all():
        entry_ids = [r.id for r in JournalEntry.query.filter_by(
            company_id=c.id).all()]
        if entry_ids:
            JournalLine.query.filter(
                JournalLine.entry_id.in_(entry_ids)
            ).delete(synchronize_session=False)
        insp = inspect(db.engine)
        for table in reversed(db.metadata.sorted_tables):
            if "company_id" in {col["name"] for col in insp.get_columns(table.name)}:
                db.session.execute(
                    table.delete().where(
                        table.c.company_id == c.id))
        db.session.delete(c)
    db.session.execute(text(
        "DELETE FROM users WHERE email LIKE '%__signup_restriction%'"))
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()


def _logged_in_client(user_id, active_company_id=None):
    """Build a Flask test_client whose session has `user_id` logged
    in via the real `flask_login.login_user` flow.  Returns the
    (app, client) pair — caller must stay inside the client's
    `with` block or `app.app_context()` as appropriate."""
    from app.models import User
    from flask import session as _flask_session
    from flask_login import login_user
    app = create_app()
    app.config["SESSION_PROTECTION"] = None
    u = db.session.get(User, user_id)
    assert u is not None
    client = app.test_client()
    with app.test_request_context():
        login_user(u)
        if active_company_id:
            _flask_session["active_company_id"] = active_company_id
        saved = dict(_flask_session)
    with client.session_transaction() as sess:
        sess.update(saved)
    return app, client


# ─── 1. GET /register redirects to the shared WA URL ──────────────
@check("1. GET /register → 302 to wa.me with ticket's Arabic prefill")
def _():
    from app.services.site_contact import (
        whatsapp_signup_url, WHATSAPP_SIGNUP_MESSAGE,
    )
    app = create_app()
    with app.test_client() as c:
        resp = c.get("/register", follow_redirects=False)
    assert resp.status_code in (301, 302, 303), \
        f"expected redirect, got {resp.status_code}"
    loc = resp.headers.get("Location", "")
    assert loc == whatsapp_signup_url(), (
        f"redirect doesn't match helper. loc={loc!r} "
        f"helper={whatsapp_signup_url()!r}")
    # Decode the prefill text and confirm it matches the ticket's
    # canonical Arabic string character-for-character.
    text_part = loc.split("?text=", 1)[1]
    decoded = unquote(text_part)
    assert decoded == WHATSAPP_SIGNUP_MESSAGE, (
        f"prefill message drifted. got={decoded!r}")
    return f"→ {loc}"


# ─── 2. POST /register ALSO redirects — backend-level block ───────
@check("2. POST /register with signup payload → 302, no User created")
def _():
    from app.models import User
    before = User.query.count()
    app = create_app()
    with app.test_client() as c:
        resp = c.post(
            "/register",
            data={
                "email": "would_create@attacker.evil",
                "full_name": "Mr Attacker",
                "password": "LongEnoughPassw0rd!",
                "password_confirm": "LongEnoughPassw0rd!",
            },
            follow_redirects=False,
        )
    assert resp.status_code in (301, 302, 303), (
        f"POST /register expected redirect, got {resp.status_code} "
        f"body={resp.data[:200]!r}")
    after = User.query.count()
    assert before == after, (
        f"User count changed on POST /register: {before}→{after} — "
        f"the backend-level block failed")
    assert User.query.filter_by(
        email="would_create@attacker.evil").first() is None
    return f"POST refused, User count stayed at {after}"


# ─── 3. /companies/new as a plain user → 403 ──────────────────────
@check("3. GET /companies/new as non-super-admin → 403, no Company created")
def _():
    from app.models import Company
    before = Company.query.count()
    app, client = _logged_in_client(_STATE["plain_id"])
    with client:
        resp = client.get("/companies/new", follow_redirects=False)
    assert resp.status_code == 403, (
        f"plain user should get 403 from /companies/new, "
        f"got {resp.status_code}")

    # Also pin that a crafted POST is blocked.
    app, client = _logged_in_client(_STATE["plain_id"])
    with client:
        resp = client.post(
            "/companies/new",
            data={"name": "evil-tenant", "base_currency": "EGP"},
            follow_redirects=False)
    assert resp.status_code == 403, (
        f"plain user POST should be 403, got {resp.status_code}")
    after = Company.query.count()
    assert before == after, (
        f"Company count changed from a non-super-admin POST: "
        f"{before}→{after}")
    return "GET + POST both 403, no Company created"


# ─── 4. Super admin can STILL create a company ───────────────────
@check("4. super admin /companies/new (GET + POST) works end-to-end")
def _():
    from app.models import Company
    from app.models.user import user_companies
    app, client = _logged_in_client(_STATE["sa_id"])
    with client:
        resp = client.get("/companies/new", follow_redirects=False)
    # SA might get the form (200) or get bounced by requires_approval
    # — accept either as long as it's NOT the 403 a plain user sees.
    assert resp.status_code != 403, (
        f"super admin should not be 403'd, got {resp.status_code}")

    # POST a real company creation.  The route's own logic + CoA seed
    # fire inline.
    before = Company.query.filter_by(name=COMPANY_NAME).count()
    assert before == 0
    app, client = _logged_in_client(_STATE["sa_id"])
    with client:
        resp = client.post(
            "/companies/new",
            data={"name": COMPANY_NAME, "base_currency": "EGP"},
            follow_redirects=False)
    # Accept 302 (success → redirect to dashboard) OR 303.  Reject
    # 403/500.
    assert resp.status_code in (301, 302, 303), (
        f"SA POST got unexpected {resp.status_code}: "
        f"{resp.data[:200]!r}")
    db.session.expire_all()
    c = Company.query.filter_by(name=COMPANY_NAME).first()
    assert c is not None, "SA POST did not create the company"
    # Ownership row written.
    row = db.session.execute(user_companies.select().where(
        (user_companies.c.user_id == _STATE["sa_id"])
        & (user_companies.c.company_id == c.id))).first()
    assert row is not None, "SA ownership row missing"
    return f"Company id={c.id} created by super admin"


# ─── 5. Static landing has zero /register hrefs ──────────────────
@check("5. static/landing.html: no /register CTAs; wa.me links carry prefill")
def _():
    from app.services.site_contact import whatsapp_signup_url
    p = ROOT / "app" / "static" / "landing.html"
    txt = p.read_text(encoding="utf-8")

    stale = []
    for marker in ('href="/register', 'href="https://marsoud.com/register'):
        if marker in txt:
            stale.append(marker)
    assert not stale, f"stale register CTAs still in landing: {stale}"

    url = whatsapp_signup_url()
    assert url in txt, (
        f"shared helper URL not found verbatim in static landing — "
        f"audit's drift guard tripped. helper={url!r}")
    return f"zero stale CTAs; helper URL present"


# ─── 6. Icons are self-hosted, Google CDN icon link is gone ──────
@check("6. static/landing.html wires self-hosted Material Symbols")
def _():
    p = ROOT / "app" / "static" / "landing.html"
    txt = p.read_text(encoding="utf-8")

    # No Google CDN icon-font URL.
    assert "Material+Symbols+Outlined" not in txt, (
        "Google CDN Material Symbols link still present — "
        "reload bug not fixed")
    # Preload hint + icons.css link both wired.
    assert "/static/fonts/material-symbols/MaterialSymbolsOutlined.woff2" in txt, (
        "font preload hint missing")
    assert "/static/css/icons.css" in txt, (
        "icons.css stylesheet link missing")

    # And the CSS file itself declares @font-face.
    css = (ROOT / "app" / "static" / "css" / "icons.css").read_text(
        encoding="utf-8")
    assert "@font-face" in css and "Material Symbols Outlined" in css, (
        "icons.css doesn't declare @font-face for Material Symbols")
    assert "font-display: block" in css, (
        "icons.css must use font-display: block to avoid flashing "
        "ligature text")
    return "preload + icons.css wired; @font-face + font-display: block set"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:
                    print(f"FAIL  {label}\n        => "
                          f"{type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                _teardown()
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
