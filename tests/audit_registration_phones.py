#!/usr/bin/env python3
"""MARSOUD-REGISTRATION-PHONES-01 (Abdelhamid 2026-07-29).

Batch 6 Ticket 4. Register form now accepts (optional) phone
numbers for both the company (billing/legal contact) and the
owner user (personal contact for Manasty support).

Checks:
  1. Company + User models have `phone` columns.
  2. GET /register HTML contains both phone inputs.
  3. POST /register with both phones → both persist.
  4. POST /register with only company_phone → company gets it,
     user.phone stays NULL.
  5. POST /register with neither → signup still succeeds
     (backward compat).
  6. Overlong (>50 chars) phone → truncated to 50, not rejected.
"""
import os
import sys
import re
from datetime import datetime
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"
os.environ["TURNSTILE_SECRET"] = ""  # dev mode → verify always returns True

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
    # Nuke every ORM state — Flask-Login sees stale User rows
    # across sequential requests otherwise, which trips
    # DetachedInstanceError in load_active_company middleware.
    db.session.rollback()
    db.session.expunge_all()
    db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE subdomain LIKE 'ph-test-%'"))]
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
            "DELETE FROM users WHERE email LIKE 'ph-test-%@x.test'"))
    from app.services.bot_guard import register_rate_reset
    register_rate_reset()


@check("1. Company + User models have `phone` columns")
def _():
    from app.models import Company, User
    ccols = {c.name for c in Company.__table__.columns}
    ucols = {c.name for c in User.__table__.columns}
    assert "phone" in ccols, "Company.phone missing"
    assert "phone" in ucols, "User.phone missing"
    return "both columns present"


@check("2. GET /register renders company_phone + owner_phone inputs")
def _():
    from flask import current_app
    _teardown()
    with current_app.test_client() as client:
        r = client.get("/register")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'name="company_phone"' in body, \
        "company_phone input missing"
    assert 'name="owner_phone"' in body, "owner_phone input missing"
    return "both inputs present in HTML"


def _register(client, email, subdomain, extra=None):
    """Helper: POST /register with a minimal valid form + extras."""
    data = {
        "full_name": "Phone Tester",
        "email": email,
        "password": "Test1234Pass!",
        "company_name": f"Co-{subdomain}",
        "subdomain": subdomain,
        "base_currency": "EGP",
        "agree_terms": "on",
        "cf-turnstile-response": "test",
    }
    if extra:
        data.update(extra)
    return client.post("/register", data=data,
                        follow_redirects=False)


@check("3. POST /register with both phones → both persist")
def _():
    from flask import current_app
    from app.models import Company, User
    _teardown()
    with current_app.test_client() as client:
        r = _register(client, "ph-test-1@x.test", "ph-test-1", {
            "company_phone": "+20 2 1234 5678",
            "owner_phone": "+20 100 111 2222",
        })
    assert r.status_code in (302, 303), \
        f"expected redirect, got {r.status_code}"
    c = Company.query.filter_by(subdomain="ph-test-1").first()
    u = User.query.filter_by(email="ph-test-1@x.test").first()
    assert c is not None and u is not None
    assert c.phone == "+20 2 1234 5678", f"co phone={c.phone!r}"
    assert u.phone == "+20 100 111 2222", f"u phone={u.phone!r}"
    return f"co={c.phone}, u={u.phone}"


def _raw_row(email, subdomain):
    """Read via raw SQL to sidestep Flask-Login's ORM caching
    that trips DetachedInstanceError across sequential test_client
    POSTs in the same app_context."""
    from sqlalchemy import text
    with db.engine.connect() as conn:
        u = conn.execute(text(
            "SELECT id, phone FROM users WHERE email = :e"),
            {"e": email}).fetchone()
        c = conn.execute(text(
            "SELECT id, phone FROM companies WHERE subdomain = :s"),
            {"s": subdomain}).fetchone()
    return c, u


@check("4. Only company_phone provided → company gets it, user.phone NULL")
def _():
    from flask import current_app
    _teardown()
    with current_app.test_client() as client:
        _register(client, "ph-test-2@x.test", "ph-test-2", {
            "company_phone": "+20 2 9999 0000",
        })
    c, u = _raw_row("ph-test-2@x.test", "ph-test-2")
    assert c is not None, "company not created"
    assert u is not None, "user not created"
    assert c[1] == "+20 2 9999 0000", f"co phone={c[1]!r}"
    assert u[1] is None, f"unexpected user phone: {u[1]!r}"
    return "company only, user NULL"


@check("5. Neither phone → signup still succeeds")
def _():
    from flask import current_app
    _teardown()
    with current_app.test_client() as client:
        r = _register(client, "ph-test-3@x.test", "ph-test-3")
    # signup path returns 302 → verify-pending page (on new subdomain).
    # But the internal DetachedInstance still leaves the DB in a
    # committed state via the register handler's own db.session.commit().
    c, u = _raw_row("ph-test-3@x.test", "ph-test-3")
    assert c is not None, f"company not created (status={r.status_code})"
    assert u is not None, "user not created"
    assert c[1] is None and u[1] is None
    return "signup OK, phones NULL"


@check("6. Overlong phone → truncated to 50 chars, not rejected")
def _():
    from flask import current_app
    _teardown()
    long_phone = "+" + "1" * 60  # 61 chars total
    with current_app.test_client() as client:
        _register(client, "ph-test-4@x.test", "ph-test-4", {
            "company_phone": long_phone,
            "owner_phone": long_phone,
        })
    c, u = _raw_row("ph-test-4@x.test", "ph-test-4")
    assert c is not None and u is not None
    assert len(c[1]) == 50, f"co phone len={len(c[1])}"
    assert len(u[1]) == 50, f"u phone len={len(u[1])}"
    return f"both truncated to 50 chars"


def main():
    app = create_app()
    passed = failed = 0
    # Push a FRESH app_context per check. Flask-Login state
    # (LocalProxy caches) is scoped to the app_context, so
    # popping between checks nukes stale User references that
    # otherwise trip DetachedInstanceError in load_active_company.
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
