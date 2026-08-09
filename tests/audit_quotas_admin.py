#!/usr/bin/env python3
"""MARSOUD-SUPERADMIN-CONTROL-01 T5 (2026-08-08) — quotas admin UI.

Locks:
  · list_consumption returns sorted-by-pct desc with expected shape
  · upsert_quota writes + audits, refuses invalid input
  · BLOCK still blocks, ALLOW_NOTIFY doesn't, threshold sends bell
  · GET /admin/quotas renders 200 with a row per known quota type
  · POST /admin/quotas/plan/<id>/save creates a new row

Nine checks total.
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__QUOTA_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import (
        Company, Plan, Quota, User, UserStatus,
        QUOTA_USERS, QUOTA_AI_TOKENS_MONTH, QUOTA_STORAGE_BYTES,
        ENF_BLOCK,
    )
    from app.models.user import user_companies
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code="__quota_test__").first()
    if not plan:
        plan = Plan(code="__quota_test__", name="QT",
                    name_ar="QT", allowed_subitems=None)
        plan.set_modules(["accounting", "sales"])
        db.session.add(plan)
        db.session.flush()
        # Seed 3 quota rows so BLOCK/ALLOW_NOTIFY tests have a
        # starting point.
        for qt, amt in [(QUOTA_USERS, 2),
                        (QUOTA_AI_TOKENS_MONTH, 1000),
                        (QUOTA_STORAGE_BYTES, 1_000_000_000)]:
            db.session.add(Quota(plan_id=plan.id, quota_type=qt,
                                  included_amount=amt,
                                  enforcement_mode=ENF_BLOCK))
    db.session.commit()

    co = Company(name=f"{PREFIX}CO", base_currency="EGP",
                  subdomain="qt",
                  subscription_started_at=datetime.utcnow(),
                  subscription_expires_at=datetime(2999, 1, 1),
                  intended_plan_id=plan.id, plan_id=plan.id)
    db.session.add(co)
    db.session.flush()

    su = User(email=f"{PREFIX}sa@x.test", full_name="qt super",
              is_active=True, is_superadmin=True,
              status=UserStatus.ACTIVE.value,
              email_verified_at=datetime.utcnow(),
              terms_version="TEST",
              password_hash=generate_password_hash(
                  "x", method="pbkdf2:sha256"))
    owner = User(email=f"{PREFIX}owner@x.test", full_name="qt owner",
                  is_active=True, status=UserStatus.ACTIVE.value,
                  email_verified_at=datetime.utcnow(),
                  terms_version="TEST",
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"))
    db.session.add_all([su, owner])
    db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=co.id, role="owner"))
    db.session.commit()
    _STATE.update(plan_id=plan.id, company_id=co.id,
                   su_id=su.id, owner_id=owner.id)


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all()
    db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        # Clean rows tied to the fixture plan first (quotas +
        # QuotaNotificationSent + companies).
        cids = [r[0] for r in conn.execute(text(
            f"SELECT id FROM companies WHERE name LIKE '{PREFIX}%'"))]
        for cid in cids:
            for tbl in ("quota_notification_sent", "ai_token_usage",
                         "employee_ai_caps", "user_companies"):
                try:
                    conn.execute(text(
                        f"DELETE FROM {tbl} WHERE company_id = :c"),
                        {"c": cid})
                except Exception:
                    pass
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
        # Delete plan's quotas + the plan itself
        conn.execute(text(
            "DELETE FROM quotas WHERE plan_id IN "
            "(SELECT id FROM plans WHERE code = '__quota_test__')"))
        conn.execute(text(
            "DELETE FROM plans WHERE code = '__quota_test__'"))
        conn.execute(text(
            f"DELETE FROM users WHERE email LIKE '{PREFIX}%@x.test'"))


def _client_as(user_id):
    c = _STATE["app"].test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
    return c


# ─── Checks ─────────────────────────────────────────────────────────

@check("1. list_consumption returns sorted-desc rows with expected keys")
def _():
    _setup()
    from app.services.quotas import list_consumption
    rows = list_consumption()
    assert rows, "list_consumption returned empty"
    # Check shape of the first row
    r = rows[0]
    for k in ("company_id", "company_name", "plan_code",
              "plan_name_ar", "max_pct", "rows"):
        assert k in r, f"missing key {k}"
    assert isinstance(r["rows"], list) and r["rows"]
    for sub in r["rows"]:
        for k in ("quota_type", "current", "included", "pct",
                  "enforcement"):
            assert k in sub, f"row missing key {k}"
    # Sorted by max_pct desc
    pcts = [row["max_pct"] for row in rows]
    assert pcts == sorted(pcts, reverse=True), (
        f"consumption not sorted desc: {pcts}")
    return f"{len(rows)} companies listed, top max_pct={pcts[0]:.1f}"


@check("2. upsert_quota writes + emits an audit-log entry")
def _():
    _setup()
    from app.services.quotas import upsert_quota
    from app.models import PlatformAuditLog, Quota, QUOTA_USERS
    before = PlatformAuditLog.query.filter_by(
        action="quota_edit").count()
    row = upsert_quota(_STATE["plan_id"], QUOTA_USERS,
                        included_amount=15, enforcement_mode="BLOCK",
                        actor_id=_STATE["su_id"])
    assert row.included_amount == 15
    assert row.enforcement_mode == "BLOCK"
    after = PlatformAuditLog.query.filter_by(
        action="quota_edit").count()
    assert after - before == 1, (
        f"expected 1 audit log row; got {after - before}")
    return "row saved + 1 audit log entry"


@check("3. upsert_quota refuses unknown quota_type")
def _():
    _setup()
    from app.services.quotas import upsert_quota
    raised = False
    try:
        upsert_quota(_STATE["plan_id"], "nonsense",
                      included_amount=1, enforcement_mode="BLOCK",
                      actor_id=_STATE["su_id"])
    except ValueError as e:
        raised = "nonsense" in str(e)
    assert raised, "unknown quota_type should raise ValueError"
    return "typo in quota_type caught"


@check("4. upsert_quota refuses unknown enforcement_mode")
def _():
    _setup()
    from app.services.quotas import upsert_quota
    from app.models import QUOTA_USERS
    raised = False
    try:
        upsert_quota(_STATE["plan_id"], QUOTA_USERS,
                      included_amount=1, enforcement_mode="MAYBE",
                      actor_id=_STATE["su_id"])
    except ValueError as e:
        raised = "MAYBE" in str(e) or "غير معروف" in str(e)
    assert raised, "unknown enforcement_mode should raise"
    return "invalid mode caught"


@check("5. BLOCK enforcement still fires — user cap trip raises")
def _():
    _setup()
    from app.services.quotas import (
        check_quota, QuotaBlockedError, upsert_quota,
    )
    from app.models import Company, QUOTA_USERS
    co = db.session.get(Company, _STATE["company_id"])
    # Set plan users limit = 1 (already have 1 → next add should trip)
    upsert_quota(_STATE["plan_id"], QUOTA_USERS,
                  included_amount=1, enforcement_mode="BLOCK",
                  actor_id=_STATE["su_id"])
    raised = False
    try:
        check_quota(co, QUOTA_USERS, incoming=1)
    except QuotaBlockedError as e:
        raised = "الباقة" in str(e) or "المسموح" in str(e) or "الحد" in str(e)
    assert raised, "check_quota should have raised QuotaBlockedError"
    return "BLOCK trips at cap"


@check("6. ALLOW_NOTIFY does NOT block — same setup, different mode")
def _():
    _setup()
    from app.services.quotas import (
        check_quota, QuotaBlockedError, upsert_quota,
    )
    from app.models import Company, QUOTA_USERS
    co = db.session.get(Company, _STATE["company_id"])
    upsert_quota(_STATE["plan_id"], QUOTA_USERS,
                  included_amount=1, enforcement_mode="ALLOW_NOTIFY",
                  actor_id=_STATE["su_id"])
    # Should NOT raise — mock the notification path to keep the test hermetic.
    with patch("app.services.quotas._send_owner_notification"):
        check_quota(co, QUOTA_USERS, incoming=1)
    return "ALLOW_NOTIFY passes through"


@check("7. Threshold crossing fires both email AND bell notification")
def _():
    _setup()
    from app.services.quotas import (
        _notify_thresholds, upsert_quota,
    )
    from app.models import Company, QUOTA_USERS
    co = db.session.get(Company, _STATE["company_id"])
    upsert_quota(_STATE["plan_id"], QUOTA_USERS,
                  included_amount=10, enforcement_mode="ALLOW_NOTIFY",
                  actor_id=_STATE["su_id"])
    with patch("app.services.email.send_email") as m_email, \
            patch("app.services.opsflow_extras.notify_users") as m_bell:
        _notify_thresholds(co, QUOTA_USERS, current=9, limit=10)
    # Crossing 80% + 90% should fire dispatch(es). Each dispatch
    # calls both email + bell (bell inside _send_owner_notification).
    assert m_email.call_count >= 1, (
        f"expected at least 1 email; got {m_email.call_count}")
    assert m_bell.call_count >= 1, (
        f"expected at least 1 bell notify_users call; "
        f"got {m_bell.call_count}")
    return (f"{m_email.call_count} email(s) + "
            f"{m_bell.call_count} bell(s)")


@check("8. GET /admin/quotas renders 200 with all quota rows per plan")
def _():
    _setup()
    from app.models import KNOWN_QUOTA_TYPES
    c = _client_as(_STATE["su_id"])
    r = c.get("/admin/quotas")
    assert r.status_code == 200, (
        f"expected 200, got {r.status_code}")
    body = r.get_data(as_text=True)
    # All 4 known quota types render as rows for our fixture plan
    for qt in KNOWN_QUOTA_TYPES:
        assert qt in body, f"quota type {qt} not in body"
    # Header present
    assert "الحدود" in body or "Quotas" in body
    return f"page renders with {len(KNOWN_QUOTA_TYPES)} rows per plan"


@check("9. POST /admin/quotas/plan/<id>/save creates a row")
def _():
    _setup()
    from app.models import Quota, QUOTA_BRANCHES
    plan_id = _STATE["plan_id"]
    # Confirm branches row doesn't exist yet
    existing = Quota.query.filter_by(
        plan_id=plan_id, quota_type=QUOTA_BRANCHES).first()
    assert existing is None, "test setup expected no branches row"
    c = _client_as(_STATE["su_id"])
    r = c.post(f"/admin/quotas/plan/{plan_id}/save",
                data={
                    "quota_type": QUOTA_BRANCHES,
                    "included_amount": "3",
                    "enforcement_mode": "BLOCK",
                    "price_per_extra_unit": "",
                }, follow_redirects=False)
    assert r.status_code == 302, (
        f"expected redirect, got {r.status_code}")
    row = Quota.query.filter_by(
        plan_id=plan_id, quota_type=QUOTA_BRANCHES).first()
    assert row is not None, "quota row not created"
    assert row.included_amount == 3
    assert row.enforcement_mode == "BLOCK"
    return f"branches quota created id={row.id}"


def main():
    app = create_app()
    _STATE["app"] = app
    passed = failed = 0
    try:
        for label, fn in CHECKS:
            try:
                with app.app_context():
                    result = fn()
                print(f"PASS  {label}\n        ⇒ {result}")
                passed += 1
            except Exception as e:
                print(f"FAIL  {label}\n        ⇒ {type(e).__name__}: {e}")
                failed += 1
    finally:
        with app.app_context():
            _teardown()
        print("\n(cleaned up fixture data)")
    print(f"\n────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
