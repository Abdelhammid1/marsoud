#!/usr/bin/env python3
"""MARSOUD-SUPERADMIN-CONTROL-01 T6 (2026-08-08) — Company 360° audit.

Covers every composer in app/services/company_360.py plus the
end-to-end route render at /admin/companies/<id>.

Twelve checks:

  1. subscription_snapshot on active company → state='active'
  2. subscription_snapshot on expired company → state ∈ {'grace','read_only'}
  3. usage_snapshot returns 4 rows with the expected keys
  4. usage_snapshot marks UNLIMITED enforcement as unlimited=True, pct=None
  5. usage_snapshot color follows the 70/90 thresholds
  6. ai_usage_row returns zeros not None when no AiTokenUsage rows exist
  7. owners_of returns only role='owner' users; ignores members / admins
  8. module_matrix — effective flips when a kill-switch flips
  9. errors_preview respects the limit kwarg (order by created_at desc)
 10. errors_preview filters to this company only (sibling seed absent)
 11. GET /admin/companies/<id> as super-admin renders 200 with every
     new card's emoji marker present in HTML
 12. Route stays 200 when a composer raises (monkey-patched to throw)
"""
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
PREFIX = "__C360_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _p(msg):
    """ASCII-safe print (Windows cp1252 can't render ✓/✗)."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


# ─── Fixture ───────────────────────────────────────────────────
def _setup(*, expires_in_days=365, quota_users_included=10,
            quota_users_mode="BLOCK", extra_owner=False,
            extra_member=False):
    _teardown()
    from app.models import (
        Company, Plan, User, UserStatus, Quota, ENF_BLOCK,
        QUOTA_USERS,
    )
    from app.models.user import user_companies
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code="__c360__").first()
    if not plan:
        plan = Plan(code="__c360__", name="C360", name_ar="C360",
                    allowed_subitems=None)
        # Broad module set so module_matrix has both "in-plan" and
        # "out-of-plan" rows.
        plan.set_modules(["accounting", "sales", "reports"])
        db.session.add(plan); db.session.flush()

    c = Company(
        name=f"{PREFIX}CO", base_currency="EGP", subdomain="c360",
        subscription_started_at=datetime.utcnow(),
        subscription_expires_at=(
            datetime.utcnow() + timedelta(days=expires_in_days)),
        subscription_frequency="MONTHLY",
        intended_plan_id=plan.id, plan_id=plan.id,
    )
    db.session.add(c); db.session.flush()

    # Super-admin (for the route smoke test in check 11).
    sa = User(
        email=f"{PREFIX}sa@x.test", full_name="super admin",
        is_active=True, is_superadmin=True,
        status=UserStatus.ACTIVE.value,
        email_verified_at=datetime.utcnow(),
        terms_version="TEST",
        password_hash=generate_password_hash(
            "x", method="pbkdf2:sha256"))
    db.session.add(sa); db.session.flush()

    # Owner of the fixture company.
    owner = User(
        email=f"{PREFIX}owner@x.test", full_name="owner",
        is_active=True,
        status=UserStatus.ACTIVE.value,
        email_verified_at=datetime.utcnow(),
        terms_version="TEST",
        password_hash=generate_password_hash(
            "x", method="pbkdf2:sha256"))
    db.session.add(owner); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner"))

    if extra_owner:
        o2 = User(
            email=f"{PREFIX}owner2@x.test", full_name="owner2",
            is_active=True,
            status=UserStatus.ACTIVE.value,
            email_verified_at=datetime.utcnow(),
            terms_version="TEST",
            password_hash=generate_password_hash(
                "x", method="pbkdf2:sha256"))
        db.session.add(o2); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=o2.id, company_id=c.id, role="owner"))

    if extra_member:
        m = User(
            email=f"{PREFIX}member@x.test", full_name="member",
            is_active=True,
            status=UserStatus.ACTIVE.value,
            email_verified_at=datetime.utcnow(),
            terms_version="TEST",
            password_hash=generate_password_hash(
                "x", method="pbkdf2:sha256"))
        db.session.add(m); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=m.id, company_id=c.id, role="team_member"))

    # Quota rows.
    if quota_users_included is not None:
        q = Quota(
            plan_id=plan.id, quota_type=QUOTA_USERS,
            included_amount=quota_users_included,
            enforcement_mode=quota_users_mode)
        db.session.add(q)
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
            "SELECT id FROM companies WHERE name LIKE '__C360_%'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            conn.execute(text(
                "DELETE FROM platform_errors WHERE company_id = :c"),
                {"c": cid})
            conn.execute(text(
                "DELETE FROM ai_token_usage WHERE company_id = :c"),
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
            "DELETE FROM users WHERE email LIKE '__C360_%@x.test'"))
        # SQLite doesn't enforce ondelete=CASCADE unless PRAGMA is
        # on, so wipe child rows on the fixture plan explicitly.
        pids = [r[0] for r in conn.execute(text(
            "SELECT id FROM plans WHERE code = '__c360__'"))]
        for pid in pids:
            conn.execute(text(
                "DELETE FROM quotas WHERE plan_id = :p"), {"p": pid})
        conn.execute(text(
            "DELETE FROM plans WHERE code = '__c360__'"))
        # SQLite reuses primary keys when there is no AUTOINCREMENT;
        # a freshly-inserted __c360__ plan can land on an id that
        # still has orphan quota rows from a prior aborted run. Sweep
        # them here so the next INSERT INTO quotas doesn't clash.
        conn.execute(text(
            "DELETE FROM quotas WHERE plan_id NOT IN "
            "(SELECT id FROM plans)"))
        # Kill switches for canonical modules — leave alone; the
        # test creates/removes rows explicitly per check.
        conn.execute(text(
            "DELETE FROM feature_flags WHERE module_key IN "
            "('accounting', 'sales', 'reports', 'crm')"))


# ─── Checks ────────────────────────────────────────────────────
@check("1. subscription_snapshot on active company")
def _():
    from app.models import Company
    from app.services.company_360 import subscription_snapshot
    _setup(expires_in_days=45)
    c = db.session.get(Company, _STATE["company_id"])
    snap = subscription_snapshot(c)
    assert snap["state"] == "active", f"state={snap['state']!r}"
    assert snap["days_remaining"] is not None
    assert snap["days_remaining"] > 30, snap["days_remaining"]
    assert isinstance(snap["outstanding_saas_count"], int)


@check("2. subscription_snapshot on expired company -> grace/read_only")
def _():
    from app.models import Company
    from app.services.company_360 import subscription_snapshot
    _setup(expires_in_days=-3)   # expired 3 days ago
    c = db.session.get(Company, _STATE["company_id"])
    snap = subscription_snapshot(c)
    assert snap["state"] in ("grace", "read_only"), snap["state"]
    # Grace snapshot: days_remaining is negative (days into grace).
    if snap["state"] == "grace":
        assert snap["days_remaining"] <= 0, snap["days_remaining"]


@check("3. usage_snapshot returns 4 rows with expected keys")
def _():
    from app.models import Company
    from app.services.company_360 import usage_snapshot
    _setup()
    c = db.session.get(Company, _STATE["company_id"])
    cards = usage_snapshot(c)
    assert len(cards) == 4, f"expected 4 rows, got {len(cards)}"
    required = {"quota_type", "label_ar", "current", "included",
                "pct", "color", "enforcement_mode", "unlimited",
                "unset"}
    for row in cards:
        missing = required - set(row.keys())
        assert not missing, f"missing keys: {missing} in {row}"


@check("4. usage_snapshot marks UNLIMITED as unlimited=True, pct=None")
def _():
    from app.models import Company, QUOTA_USERS
    from app.services.company_360 import usage_snapshot
    _setup(quota_users_included=9999, quota_users_mode="UNLIMITED")
    c = db.session.get(Company, _STATE["company_id"])
    cards = usage_snapshot(c)
    users_row = next(r for r in cards if r["quota_type"] == QUOTA_USERS)
    assert users_row["unlimited"] is True, users_row
    assert users_row["pct"] is None, users_row
    assert users_row["color"] == "gray", users_row


@check("5. usage_snapshot color follows 70/90 thresholds")
def _():
    from app.models import Company, Quota, QUOTA_USERS
    from app.models.user import user_companies
    from app.services.company_360 import usage_snapshot
    from werkzeug.security import generate_password_hash
    from app.models import User, UserStatus

    # Cap users=5, seed 4 additional members → 5 total → 100%.
    _setup(quota_users_included=5, quota_users_mode="BLOCK")
    for i in range(4):
        u = User(
            email=f"{PREFIX}m{i}@x.test", full_name=f"m{i}",
            is_active=True,
            status=UserStatus.ACTIVE.value,
            email_verified_at=datetime.utcnow(),
            terms_version="TEST",
            password_hash=generate_password_hash(
                "x", method="pbkdf2:sha256"))
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=_STATE["company_id"],
            role="team_member"))
    db.session.commit()

    c = db.session.get(Company, _STATE["company_id"])
    cards = usage_snapshot(c)
    users_row = next(r for r in cards if r["quota_type"] == QUOTA_USERS)
    assert users_row["current"] == 5, users_row
    assert users_row["pct"] == 100.0, users_row
    assert users_row["color"] == "red", users_row


@check("6. ai_usage_row returns zeros not None when no rows exist")
def _():
    from app.models import Company
    from app.services.company_360 import ai_usage_row
    _setup()
    c = db.session.get(Company, _STATE["company_id"])
    row = ai_usage_row(c)
    for key in ("input_tokens", "output_tokens", "total_tokens",
                 "total_calls", "monthly_used"):
        assert row[key] == 0, f"{key}={row[key]!r} (expected 0)"
    assert row["est_cost_usd"] == 0.0
    assert row["last_used_at"] is None


@check("7. owners_of returns owners only")
def _():
    from app.models import Company
    from app.services.company_360 import owners_of
    _setup(extra_owner=True, extra_member=True)
    c = db.session.get(Company, _STATE["company_id"])
    owners = owners_of(c)
    assert len(owners) == 2, f"expected 2 owners, got {len(owners)}"
    emails = {o.email for o in owners}
    assert f"{PREFIX}owner@x.test" in emails
    assert f"{PREFIX}owner2@x.test" in emails
    assert f"{PREFIX}member@x.test" not in emails


@check("8. module_matrix — effective flips when kill-switch flips")
def _():
    from app.models import Company
    from app.services.company_360 import module_matrix
    from app.services.feature_flags import set_module
    _setup()
    c = db.session.get(Company, _STATE["company_id"])

    # Baseline: 'accounting' is in the plan, kill-switch not set
    # → effective=True.
    rows = module_matrix(c)
    acc = next(r for r in rows if r["code"] == "accounting")
    assert acc["in_plan"] is True
    assert acc["effective"] is True

    # Kill 'accounting' → effective flips to False + reason set.
    set_module("accounting", enabled=False,
               reason="maintenance", actor_id=_STATE["superadmin_id"])
    rows = module_matrix(c)
    acc = next(r for r in rows if r["code"] == "accounting")
    assert acc["kill_switch_enabled"] is False, acc
    assert acc["effective"] is False, acc
    assert acc["disabled_reason"] == "maintenance", acc


@check("9. errors_preview respects limit + orders desc")
def _():
    from app.models import Company, PlatformError
    from app.services.company_360 import errors_preview
    _setup()
    now = datetime.utcnow()
    for i in range(15):
        db.session.add(PlatformError(
            company_id=_STATE["company_id"],
            route=f"/x/{i}", method="GET", status_code=500,
            message=f"boom-{i}",
            created_at=now - timedelta(minutes=i)))
    db.session.commit()

    c = db.session.get(Company, _STATE["company_id"])
    rows = errors_preview(c, limit=5)
    assert len(rows) == 5, f"expected 5, got {len(rows)}"
    # Newest first.
    for a, b in zip(rows, rows[1:]):
        assert a.created_at >= b.created_at


@check("10. errors_preview filters to THIS company only")
def _():
    from app.models import Company, PlatformError
    from app.services.company_360 import errors_preview
    _setup()

    # Sibling company, sibling error.
    from app.models import Plan
    sib_plan = Plan.query.filter_by(code="__c360__").one()
    sib = Company(
        name=f"{PREFIX}SIB", base_currency="EGP", subdomain="sib",
        subscription_started_at=datetime.utcnow(),
        subscription_expires_at=(
            datetime.utcnow() + timedelta(days=30)),
        intended_plan_id=sib_plan.id, plan_id=sib_plan.id)
    db.session.add(sib); db.session.flush()
    db.session.add(PlatformError(
        company_id=sib.id, route="/other", method="GET",
        status_code=500, message="sibling-error"))
    db.session.add(PlatformError(
        company_id=_STATE["company_id"], route="/mine",
        method="POST", status_code=502, message="my-error"))
    db.session.commit()

    c = db.session.get(Company, _STATE["company_id"])
    rows = errors_preview(c, limit=100)
    messages = {r.message for r in rows}
    assert "my-error" in messages
    assert "sibling-error" not in messages, messages


@check("11. GET /admin/companies/<id> renders 200 with every card marker")
def _():
    from app.models import User
    _setup()
    app = _STATE["app"]
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(_STATE["superadmin_id"])
        s["_fresh"] = True

    r = client.get(f"/admin/companies/{_STATE['company_id']}")
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)
    for marker in ("💳", "📊", "🤖", "🧩", "🛑", "👑"):
        assert marker in body, \
            f"marker {marker!r} missing from company_detail HTML"


@check("12. _safe wrapper: a raising composer leaves other cards intact")
def _():
    """Direct check on the route function: with subscription_snapshot
    patched to raise, the view still returns a 200 render_template
    call whose context has subscription=None and usage_cards populated.
    Avoids test-client cross-check pollution (Flask-Login caching
    current_user with a stale session ref from earlier checks)."""
    from unittest.mock import patch
    from flask import Flask
    _setup()
    app = _STATE["app"]

    def boom(_):
        raise RuntimeError("intentional")

    with patch("app.services.company_360.subscription_snapshot", boom):
        with app.test_request_context(
                f"/admin/companies/{_STATE['company_id']}"):
            # Bypass the login_required + superadmin_required
            # decorators by calling the underlying implementation
            # via the app context. Simpler: use the view function
            # directly through its endpoint mapping.
            from flask_login import login_user
            from app.models import User
            sa = db.session.get(User, _STATE["superadmin_id"])
            login_user(sa)

            from app.routes.superadmin import company_detail
            # company_detail is decorated; call the wrapped view
            # function directly to skip login checks.
            wrapped = app.view_functions["superadmin.company_detail"]
            # Extract the inner function past @login_required +
            # @superadmin_required (both use functools.wraps).
            captured = {}

            def spy(template, **ctx):
                captured["template"] = template
                captured["ctx"] = ctx
                return "<html>OK</html>"

            with patch("app.routes.superadmin.render_template", spy):
                resp = wrapped(company_id=_STATE["company_id"])

    assert "ctx" in captured, "render_template was not called"
    ctx = captured["ctx"]
    assert ctx.get("subscription") is None, \
        f"subscription should be None after boom, got {ctx.get('subscription')!r}"
    assert ctx.get("usage_cards"), \
        "usage_cards should still be populated (other composer)"
    assert len(ctx["usage_cards"]) == 4


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
    _p(f"audit_company_360: {passed} passed, {failed} failed")
    if failures:
        for label, err in failures:
            _p(f"  - {label} :: {err}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
