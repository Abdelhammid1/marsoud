#!/usr/bin/env python3
"""MARSOUD-SUPERADMIN-CONTROL-01 T10 (2026-08-08) — Ctrl+K palette audit.

Ten checks covering the search service + JSON endpoint + the
regrouped sidebar markers on any admin page.

  1. nav_results("") returns every catalog item (20)
  2. nav_results('dash') includes superadmin.dashboard
  3. nav_results('الشركات') matches Arabic label substrings
  4. company_results(<fixture_name>) returns fixture with url
  5. company_results(<numeric_id>) finds by id
  6. company_results('') returns []
  7. user_results(<fixture_email_prefix>) returns fixture user
  8. search_all('x') has 'groups' shape with expected keys
  9. GET /admin/nav-search.json?q=x as super-admin → 200 JSON
 10. GET /admin/dashboard HTML has palette markers +
     2+ section headers from the regrouped sidebar
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
PREFIX = "__T10_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _p(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


# ─── Fixture ───────────────────────────────────────────────────
def _setup():
    """One super-admin + one owner + one fixture company. The
    company has a distinctive name (T10Acme) so company_results
    won't match anything else in the DB."""
    _teardown()
    from app.models import Company, Plan, User, UserStatus
    from app.models.user import user_companies
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code="__t10__").first()
    if not plan:
        plan = Plan(code="__t10__", name="T10", name_ar="T10",
                    allowed_subitems=None)
        plan.set_modules(["accounting"])
        db.session.add(plan); db.session.flush()

    c = Company(name=f"{PREFIX}T10Acme", base_currency="EGP",
                 subdomain="t10acme",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=(
                     datetime.utcnow() + timedelta(days=365)),
                 intended_plan_id=plan.id, plan_id=plan.id)
    db.session.add(c); db.session.flush()

    sa = User(
        email=f"{PREFIX}sa@x.test", full_name="super admin",
        is_active=True, is_superadmin=True,
        status=UserStatus.ACTIVE.value,
        email_verified_at=datetime.utcnow(),
        terms_version="TEST",
        password_hash=generate_password_hash(
            "x", method="pbkdf2:sha256"))
    db.session.add(sa); db.session.flush()

    owner = User(
        email=f"{PREFIX}owner@x.test", full_name="Zainab Ahmed",
        is_active=True,
        status=UserStatus.ACTIVE.value,
        email_verified_at=datetime.utcnow(),
        terms_version="TEST",
        password_hash=generate_password_hash(
            "x", method="pbkdf2:sha256"))
    db.session.add(owner); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner"))
    db.session.commit()

    _STATE.update(
        company_id=c.id, plan_id=plan.id,
        superadmin_id=sa.id, owner_id=owner.id,
    )


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__T10_%'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    try:
                        conn.execute(text(
                            f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                            {"c": cid})
                    except Exception:
                        pass
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE '__T10_%@x.test'"))
        pids = [r[0] for r in conn.execute(text(
            "SELECT id FROM plans WHERE code = '__t10__'"))]
        for pid in pids:
            conn.execute(text(
                "DELETE FROM quotas WHERE plan_id = :p"), {"p": pid})
        conn.execute(text(
            "DELETE FROM plans WHERE code = '__t10__'"))


# ─── Checks ────────────────────────────────────────────────────
@check("1. nav_results('') returns every catalog item")
def _():
    from app.services.nav_search import nav_results, NAV_CATALOG
    _setup()
    # Request enough to see the full catalog.
    rows = nav_results("", limit=100)
    assert len(rows) == len(NAV_CATALOG), \
        f"got {len(rows)}, expected {len(NAV_CATALOG)}"
    endpoints = {r["endpoint"] for r in rows}
    assert "superadmin.dashboard" in endpoints
    assert "superadmin.audit" in endpoints


@check("2. nav_results('dash') matches superadmin.dashboard")
def _():
    from app.services.nav_search import nav_results
    _setup()
    rows = nav_results("dash", limit=100)
    endpoints = {r["endpoint"] for r in rows}
    assert "superadmin.dashboard" in endpoints, endpoints
    # And nothing that doesn't contain 'dash' or match the label.
    for r in rows:
        assert ("dash" in r["endpoint"].lower()
                or "dash" in r["label"].lower()), r


@check("3. nav_results matches Arabic label substrings")
def _():
    from app.services.nav_search import nav_results
    _setup()
    # 'إعدادات' appears in "إعدادات الاشتراك" +
    # "إعدادات الذكاء الاصطناعي" — 2 items.
    rows = nav_results("إعدادات", limit=100)
    endpoints = {r["endpoint"] for r in rows}
    assert "superadmin.subscription_settings" in endpoints, endpoints
    assert "superadmin.ai_settings" in endpoints, endpoints


@check("4. company_results returns fixture with correct URL")
def _():
    from app.services.nav_search import company_results
    _setup()
    rows = company_results("T10Acme")
    assert rows, "no company matched"
    row = next(r for r in rows if r["label"].endswith("T10Acme"))
    assert row["url"] == f"/admin/companies/{_STATE['company_id']}", row


@check("5. company_results finds by numeric id")
def _():
    from app.services.nav_search import company_results
    _setup()
    cid = _STATE["company_id"]
    rows = company_results(str(cid))
    urls = {r["url"] for r in rows}
    assert f"/admin/companies/{cid}" in urls, urls


@check("6. company_results('') returns []")
def _():
    from app.services.nav_search import company_results
    _setup()
    assert company_results("") == []


@check("7. user_results returns fixture user by email prefix")
def _():
    from app.services.nav_search import user_results
    _setup()
    rows = user_results(f"{PREFIX}owner")
    emails = {r["hint"] for r in rows}
    assert f"{PREFIX}owner@x.test" in emails, emails


@check("8. search_all shape has expected groups + keys")
def _():
    from app.services.nav_search import search_all
    _setup()
    # 'T10Acme' matches the fixture company; 'dash' matches the
    # nav dashboard; own email prefix matches the user.
    out = search_all("T10")
    assert "q" in out and "groups" in out
    keys = {g["key"] for g in out["groups"]}
    assert "companies" in keys, keys
    # Every item across groups has label + url + icon.
    for g in out["groups"]:
        for it in g["items"]:
            assert "label" in it
            assert "url" in it
            assert "icon" in it


@check("9. GET /admin/nav-search.json?q=... as super-admin -> 200 JSON")
def _():
    _setup()
    app = _STATE["app"]
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(_STATE["superadmin_id"])
        s["_fresh"] = True

    r = client.get("/admin/nav-search.json?q=dash")
    assert r.status_code == 200, f"got {r.status_code}"
    payload = r.get_json()
    assert "groups" in payload, payload
    endpoints = {it["endpoint"]
                 for g in payload["groups"]
                 for it in g["items"]
                 if it.get("kind") == "nav"}
    assert "superadmin.dashboard" in endpoints, endpoints


@check("10. Admin page has palette markers + 2+ section headers")
def _():
    _setup()
    app = _STATE["app"]
    sa_id = _STATE["superadmin_id"]

    # Flask-Login memoizes current_user on g._login_user per
    # app-context. If the outer app.app_context() in main() is
    # kept alive across checks, a User loaded by an earlier check
    # can be reused here and be detached (its session was
    # remove()d in a prior _teardown). Force a re-load.
    from flask import g
    try:
        g.pop("_login_user", None)
    except (KeyError, AttributeError):
        pass
    db.session.expire_all()
    db.session.remove()

    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(sa_id)
        s["_fresh"] = True
    r = client.get("/admin/")
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)

    # Palette overlay elements.
    for marker in ('id="ck-palette"', 'id="ck-input"',
                    'id="ck-backdrop"', "Ctrl", "للبحث السريع"):
        assert marker in body, f"palette marker {marker!r} missing"

    # At least 2 section headers from the regrouped sidebar.
    section_hits = sum(1 for s in (
        "الشركات والمستخدمون",
        "الاشتراكات والفوترة",
        "الميزات والذكاء الاصطناعي",
        "الإشعارات والمحتوى",
        "المراقبة والسجلات",
    ) if s in body)
    assert section_hits >= 2, \
        f"only {section_hits} section headers rendered"


# ─── Runner ────────────────────────────────────────────────────
def main():
    app = create_app()
    _STATE["app"] = app
    passed = failed = 0
    failures = []
    # Composer checks call url_for() which needs a request
    # context; route-smoke checks (test_client.get) push their
    # own. Wrap the direct-call checks only — an OUTER
    # test_request_context around a client.get can leak Flask-
    # Login's cached current_user across checks, which detaches
    # on the next _teardown's session.remove().
    NEEDS_REQ_CTX = {label for label, _ in CHECKS if not label.startswith(("9.", "10."))}

    with app.app_context():
        for label, fn in CHECKS:
            try:
                if label in NEEDS_REQ_CTX:
                    with app.test_request_context("/admin/"):
                        fn()
                else:
                    fn()
                passed += 1
                _p(f"  [OK] {label}")
            except AssertionError as e:
                failed += 1
                failures.append((label, str(e)))
                _p(f"  [FAIL] {label}: {e}")
            except Exception as e:
                failed += 1
                failures.append((label, f"{type(e).__name__}: {e}"))
                _p(f"  [ERROR] {label}: {type(e).__name__}: {e}")
        _teardown()
    _p("")
    _p(f"audit_nav_ctrlk: {passed} passed, {failed} failed")
    if failures:
        for label, err in failures:
            _p(f"  - {label} :: {err}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
