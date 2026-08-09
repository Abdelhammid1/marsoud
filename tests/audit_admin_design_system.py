#!/usr/bin/env python3
"""MARSOUD-SUPERADMIN-CONTROL-01 T9 (2026-08-08) — design system audit.

Ten smoke checks. Nothing tries to test visuals (impossible in a
unit test) — instead we assert:
  · Every class name admin/*.html templates were already using
    is defined in admin/base.html's <style> (was silently
    unstyled before T9).
  · The dark-theme rgba relics are gone (were invisible on the
    light body).
  · Tailwind config carries the new navy / brand / sky / soft
    aliases so `text-navy-900` etc. render with a real color.
  · The 3 admin pages we swept render 200 as super-admin.
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
PREFIX = "__T9_"
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


BASE_HTML_PATH = ROOT / "app" / "templates" / "admin" / "base.html"


def _base_html():
    return BASE_HTML_PATH.read_text(encoding="utf-8")


# ─── Fixture ───────────────────────────────────────────────────
def _setup():
    """Fixture SA + one company for the route smoke checks."""
    _teardown()
    from app.models import Company, Plan, User, UserStatus
    from app.models.user import user_companies
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code="__t9__").first()
    if not plan:
        plan = Plan(code="__t9__", name="T9", name_ar="T9",
                    allowed_subitems=None,
                    price_monthly=99, price_yearly=990,
                    is_active=True)
        plan.set_modules(["accounting"])
        db.session.add(plan); db.session.flush()

    c = Company(name=f"{PREFIX}CO", base_currency="EGP",
                 subdomain="t9",
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
    db.session.execute(user_companies.insert().values(
        user_id=sa.id, company_id=c.id, role="owner"))
    db.session.commit()

    _STATE.update(company_id=c.id, plan_id=plan.id,
                   superadmin_id=sa.id)


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__T9_%'"))]
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
            "DELETE FROM users WHERE email LIKE '__T9_%@x.test'"))
        pids = [r[0] for r in conn.execute(text(
            "SELECT id FROM plans WHERE code = '__t9__'"))]
        for pid in pids:
            conn.execute(text(
                "DELETE FROM quotas WHERE plan_id = :p"), {"p": pid})
        conn.execute(text(
            "DELETE FROM plans WHERE code = '__t9__'"))


def _fresh_client():
    """Fresh test_client with the super-admin logged in. Clears
    Flask-Login cache + ORM identity map (same DetachedInstance
    workaround T6/T10/T3 needed for cross-check pollution)."""
    from flask import g
    try:
        g.pop("_login_user", None)
    except (KeyError, AttributeError, RuntimeError):
        pass
    db.session.expire_all()
    db.session.remove()
    app = _STATE["app"]
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(_STATE["superadmin_id"])
        s["_fresh"] = True
    return c


# ─── Checks ────────────────────────────────────────────────────
@check("1. Canonical CSS tokens still present in admin/base.html")
def _():
    html = _base_html()
    # `.btn` + `.badge` are matched via a "starts a rule" check so
    # a compound selector like `.btn, .btn-primary { … }` counts.
    for token in (".card", ".stat ", ".stat-muted",
                   ".input", ".select", ".btn-primary",
                   ".btn-secondary", ".btn-danger",
                   ".data-table", ".nav-link.active",
                   ".badge-ok", ".badge-bad", ".badge-neutral"):
        assert token in html, f"missing canonical token: {token!r}"
    # `.btn` and `.badge` can appear as `.btn,` or `.btn ` — accept either.
    assert (".btn," in html or ".btn " in html), "missing .btn base"
    assert (".badge," in html or ".badge " in html), "missing .badge base"


@check("2. T9-added badge + textarea aliases defined")
def _():
    html = _base_html()
    for token in (".badge-paid", ".badge-cancelled",
                   ".badge-overdue", ".badge-partial",
                   ".textarea"):
        assert token in html, f"missing T9 token: {token!r}"


@check("3. T9 color aliases + shadow in tailwind.config")
def _():
    html = _base_html()
    for token in ("navy:", "brand:", "sky:", "soft:"):
        assert token in html, f"missing T9 token in tailwind config: {token!r}"


@check("4. .stat-muted no longer uses rgba(255,255,255 (dark-theme relic gone)")
def _():
    html = _base_html()
    # Slice around .stat-muted block only.
    idx = html.find(".stat-muted")
    assert idx > 0, "no .stat-muted block"
    end = html.find("}", idx)
    block = html[idx:end + 1]
    assert "rgba(255,255,255" not in block, \
        f".stat-muted still uses dark-theme rgba:\n{block}"


@check("5. .input / .select no longer use rgba(255,255,255,0.06)")
def _():
    html = _base_html()
    idx = html.find(".input, .select")
    assert idx > 0
    end = html.find("}", idx)
    block = html[idx:end + 1]
    assert "rgba(255,255,255,0.06)" not in block, \
        f".input/.select still use dark-theme rgba:\n{block}"


@check("6. Bare .btn-primary includes padding (bare-use works)")
def _():
    html = _base_html()
    # Look for the shared block that inlines padding into btn-*.
    # After T9 the rule ".btn, .btn-primary, .btn-secondary, .btn-danger"
    # sets padding.
    idx = html.find(".btn, .btn-primary")
    assert idx > 0, "shared .btn base rule not found"
    end = html.find("}", idx)
    block = html[idx:end + 1]
    assert "padding" in block, f"padding missing in shared btn block:\n{block}"


@check("7. GET /admin/ renders 200 as super-admin")
def _():
    _setup()
    r = _fresh_client().get("/admin/")
    assert r.status_code == 200, r.status_code


@check("8. GET /admin/companies renders 200 as super-admin")
def _():
    _setup()
    r = _fresh_client().get("/admin/companies")
    assert r.status_code == 200, r.status_code


@check("9. GET /admin/plans renders 200 as super-admin")
def _():
    _setup()
    r = _fresh_client().get("/admin/plans")
    assert r.status_code == 200, r.status_code


@check("10. Company detail top stat tiles use text-slate-900 (T6-file swap)")
def _():
    _setup()
    r = _fresh_client().get(f"/admin/companies/{_STATE['company_id']}")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # The 3 stat tiles now use text-slate-900 (were text-white =
    # invisible on the light stat-muted background).
    assert 'text-2xl font-bold text-slate-900' in body, \
        "top stat tiles still not swapped to text-slate-900"


# ─── Runner ────────────────────────────────────────────────────
def main():
    app = create_app()
    _STATE["app"] = app
    passed = failed = 0
    failures = []
    with app.app_context():
        for label, fn in CHECKS:
            try:
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
    _p(f"audit_admin_design_system: {passed} passed, {failed} failed")
    if failures:
        for label, err in failures:
            _p(f"  - {label} :: {err}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
