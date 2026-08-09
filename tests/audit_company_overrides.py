#!/usr/bin/env python3
"""MARSOUD-SUPERADMIN-CONTROL-01 T4 (2026-08-08) — per-tenant
feature grant/deny overrides.

Locks the ticket's Acceptance Criteria:
- GRANT opens a module immediately with no restart
- DENY closes it even when the plan has it
- Temporary overrides expire on their own
- Reason is required (DB + service both refuse empty)
- Every write hits platform_audit_logs
- Global FeatureFlag beats override GRANT
- Central /admin/overrides page renders + accepts POSTs

Checks:
  1. Model — table, unique + CHECK constraints, reason NOT NULL
  2. GRANT opens the module immediately (Starter + HR)
  3. DENY closes even when plan has it
  4. Past expires_at treated as absent
  5. Global FeatureFlag OFF beats GRANT
  6. Empty reason refused (service + DB)
  7. Unknown feature_code refused
  8. Every write lands in platform_audit_logs (3 rows)
  9. /admin/overrides GET renders 200 with empty state
  10. /admin/overrides POST creates a row + redirects
  11. /admin/overrides/<id>/revoke deletes + audits
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
PREFIX = "__OVR_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import (
        Company, Plan, User, UserStatus,
    )
    from app.models.user import user_companies
    from app.services.roles_seed import ensure_roles_ready_for_company
    from werkzeug.security import generate_password_hash

    # Two plans: Starter (no HR) + Pro (has cash_custody, hr).
    starter = Plan.query.filter_by(code="__ovr_starter__").first()
    if not starter:
        starter = Plan(code="__ovr_starter__", name="Starter",
                        name_ar="Starter", allowed_subitems=None)
        starter.set_modules([
            "accounting", "sales", "purchases", "reports",
            "agent", "inventory", "pos",
        ])
        db.session.add(starter); db.session.flush()

    pro = Plan.query.filter_by(code="__ovr_pro__").first()
    if not pro:
        pro = Plan(code="__ovr_pro__", name="Pro", name_ar="Pro",
                    allowed_subitems=None)
        pro.set_modules([
            "accounting", "sales", "purchases", "reports", "agent",
            "inventory", "pos", "crm", "hr", "manufacturing",
            "cash_custody",
        ])
        db.session.add(pro); db.session.flush()

    starter_co = Company(name=f"{PREFIX}STARTER_CO", base_currency="EGP",
                          subdomain="ovrs",
                          subscription_started_at=datetime.utcnow(),
                          subscription_expires_at=datetime(2020, 1, 1),
                          intended_plan_id=starter.id,
                          plan_id=starter.id)
    pro_co = Company(name=f"{PREFIX}PRO_CO", base_currency="EGP",
                      subdomain="ovrp",
                      subscription_started_at=datetime.utcnow(),
                      subscription_expires_at=datetime(2020, 1, 1),
                      intended_plan_id=pro.id, plan_id=pro.id)
    db.session.add_all([starter_co, pro_co]); db.session.flush()

    su = User(email=f"{PREFIX}sa@x.test", full_name="ovr super",
              is_active=True, is_superadmin=True,
              status=UserStatus.ACTIVE.value,
              email_verified_at=datetime.utcnow(),
              terms_version="TEST",
              password_hash=generate_password_hash(
                  "x", method="pbkdf2:sha256"))
    owner = User(email=f"{PREFIX}owner@x.test", full_name="ovr owner",
                  is_active=True, status=UserStatus.ACTIVE.value,
                  email_verified_at=datetime.utcnow(),
                  terms_version="TEST",
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"))
    db.session.add_all([su, owner]); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=starter_co.id, role="owner"))
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=pro_co.id, role="owner"))
    db.session.commit()
    ensure_roles_ready_for_company(starter_co.id)
    ensure_roles_ready_for_company(pro_co.id)

    _STATE.update(
        starter_co_id=starter_co.id, pro_co_id=pro_co.id,
        su_id=su.id, owner_id=owner.id,
    )


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            f"SELECT id FROM companies WHERE name LIKE '{PREFIX}%'"))]
        for cid in cids:
            # Delete overrides first (FK)
            conn.execute(text(
                "DELETE FROM company_feature_overrides "
                "WHERE company_id = :c"), {"c": cid})
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    try:
                        conn.execute(text(
                            f"DELETE FROM {tbl.name} "
                            f"WHERE company_id = :c"), {"c": cid})
                    except Exception:
                        pass
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            f"DELETE FROM users WHERE email LIKE '{PREFIX}%@x.test'"))
        conn.execute(text(
            "DELETE FROM plans WHERE code LIKE '__ovr%'"))


def _fake_authed(user_id, is_superadmin=False):
    class U:
        pass
    U.is_authenticated = True
    U.is_superadmin = is_superadmin
    U.id = user_id
    return U


def _co(company_id):
    from app.models import Company
    return db.session.get(Company, company_id)


def _client_as(user_id):
    c = _STATE["app"].test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
    return c


# ─── Checks ─────────────────────────────────────────────────────────

@check("1. Model — table + constraints exist")
def _():
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    assert "company_feature_overrides" in insp.get_table_names(), (
        "migration didn't create the table")
    cols = {c["name"] for c in insp.get_columns(
        "company_feature_overrides")}
    for col in ("id", "company_id", "feature_code", "mode",
                "reason", "expires_at", "created_by_id",
                "created_at"):
        assert col in cols, f"column {col} missing"
    uqs = insp.get_unique_constraints("company_feature_overrides")
    uq_cols = {tuple(u["column_names"]) for u in uqs}
    # MARSOUD-SUBITEM-OVERRIDES (2026-08-09) widened the unique
    # from (company_id, feature_code) to (company_id, scope,
    # feature_code) so a subitem row can coexist with a module
    # row on the same feature name. Accept either shape — this
    # test predates the widening and shouldn't false-red once
    # the migration lands.
    assert (
        ("company_id", "feature_code") in uq_cols
        or ("company_id", "scope", "feature_code") in uq_cols
    ), (f"neither the old nor the widened unique present: "
        f"{uq_cols}")
    # reason NOT NULL
    reason_col = next(c for c in insp.get_columns(
        "company_feature_overrides") if c["name"] == "reason")
    assert reason_col["nullable"] is False, (
        "reason must be NOT NULL — ticket says السبب إجباري")
    return "table + unique + NOT NULL reason all in place"


@check("2. GRANT opens a module immediately — no restart needed")
def _():
    _setup()
    from unittest.mock import patch
    from app.services.access import can_access
    from app.services.company_overrides import upsert_override
    co = _co(_STATE["starter_co_id"])
    u = _fake_authed(_STATE["owner_id"])
    # Starter has no hr — request denied at step 3 (plan_module)
    # BEFORE step 5's role check even runs, so no permission mock
    # needed for the pre-override side. For the post-override side
    # we do need has_permission to return True (fake user has no
    # real role); mock it.
    allowed, reason = can_access("hr.index", u, co)
    assert not allowed and reason == "plan_module", (
        f"pre-override should be plan_module denial; got {reason}")
    upsert_override(co.id, "hr", "GRANT", "audit test grant",
                     actor_id=_STATE["su_id"])
    with patch("app.services.permissions.has_permission",
               return_value=True):
        allowed, reason = can_access("hr.index", u, co)
    assert allowed and reason is None, (
        f"GRANT override should have allowed hr.index; got {reason}")
    return "Starter+HR: DENY → GRANT flip in one call, no restart"


@check("3. DENY closes even when plan includes the module")
def _():
    _setup()
    from unittest.mock import patch
    from app.services.access import (
        can_access, REASON_COMPANY_DENIED,
    )
    from app.services.company_overrides import upsert_override
    co = _co(_STATE["pro_co_id"])
    u = _fake_authed(_STATE["owner_id"])
    # Pro has cash_custody — plan says yes. Fake user has no real
    # role → has_permission would fail step 5 without a mock; mock
    # it since this check is about the DENY branch, not role check.
    with patch("app.services.permissions.has_permission",
               return_value=True):
        allowed, _ = can_access("custody.index", u, co)
        assert allowed, "Pro plan has cash_custody; should be allowed"
        upsert_override(co.id, "cash_custody", "DENY",
                         "audit test deny — billing dispute",
                         actor_id=_STATE["su_id"])
        allowed, reason = can_access("custody.index", u, co)
    assert not allowed and reason == REASON_COMPANY_DENIED, (
        f"DENY override should have closed custody; "
        f"got ({allowed}, {reason})")
    return "Pro + cash_custody DENY overrides plan → REASON_COMPANY_DENIED"


@check("4. Past expires_at treated as absent (row survives)")
def _():
    _setup()
    from unittest.mock import patch
    from app.services.access import can_access
    from app.services.company_overrides import (
        upsert_override, get_override, _invalidate_for_company,
    )
    from app.models import CompanyFeatureOverride
    co = _co(_STATE["starter_co_id"])
    u = _fake_authed(_STATE["owner_id"])
    upsert_override(co.id, "hr", "GRANT", "trial", actor_id=_STATE["su_id"],
                     expires_at=datetime.utcnow() + timedelta(days=7))
    with patch("app.services.permissions.has_permission",
               return_value=True):
        allowed, _ = can_access("hr.index", u, co)
    assert allowed, "unexpired GRANT should allow"
    # Age the row: set expires_at to yesterday.
    row = CompanyFeatureOverride.query.filter_by(
        company_id=co.id, feature_code="hr").first()
    row.expires_at = datetime.utcnow() - timedelta(days=1)
    db.session.commit()
    _invalidate_for_company(co.id)   # bust cache
    # Now the override should be treated as absent → back to plan.
    allowed, reason = can_access("hr.index", u, co)
    assert not allowed and reason == "plan_module", (
        f"expired GRANT should revert to plan denial; got {reason}")
    assert get_override(co.id, "hr") is None, (
        "get_override should return None for expired rows")
    # Row still exists — it's for the audit trail
    persisted = CompanyFeatureOverride.query.filter_by(
        company_id=co.id, feature_code="hr").first()
    assert persisted is not None, "expired row should survive for audit"
    return "expired row ignored by resolver, kept in DB"


@check("5. Global FeatureFlag OFF beats GRANT override")
def _():
    _setup()
    from app.services.access import (
        can_access, REASON_PLATFORM_DISABLED,
    )
    from app.services.company_overrides import upsert_override
    from app.services.feature_flags import set_module
    from app.models import FeatureFlag
    co = _co(_STATE["pro_co_id"])
    u = _fake_authed(_STATE["owner_id"])
    # Grant custody + turn OFF the global flag for cash_custody
    upsert_override(co.id, "cash_custody", "GRANT", "shouldn't win",
                     actor_id=_STATE["su_id"])
    set_module("cash_custody", False, "maintenance",
                _STATE["su_id"])
    try:
        allowed, reason = can_access("custody.index", u, co)
        assert not allowed and reason == REASON_PLATFORM_DISABLED, (
            f"platform flag should beat GRANT; got ({allowed}, {reason})")
    finally:
        # Reset the flag so subsequent checks don't inherit it.
        set_module("cash_custody", True, None, _STATE["su_id"])
        FeatureFlag.query.filter_by(module_key="cash_custody").delete()
        db.session.commit()
    return "platform_disabled > GRANT"


@check("6. Empty reason refused — service raises ValueError")
def _():
    _setup()
    from app.services.company_overrides import upsert_override
    co = _co(_STATE["starter_co_id"])
    raised = False
    try:
        upsert_override(co.id, "hr", "GRANT", "",
                         actor_id=_STATE["su_id"])
    except ValueError as e:
        raised = "السبب إجباري" in str(e)
    assert raised, "upsert should refuse empty reason with Arabic message"
    # Also try whitespace-only
    raised2 = False
    try:
        upsert_override(co.id, "hr", "GRANT", "   \n",
                         actor_id=_STATE["su_id"])
    except ValueError:
        raised2 = True
    assert raised2, "upsert should refuse whitespace-only reason"
    return "empty + whitespace-only both refused"


@check("7. Unknown feature_code refused")
def _():
    _setup()
    from app.services.company_overrides import upsert_override
    co = _co(_STATE["starter_co_id"])
    raised = False
    try:
        upsert_override(co.id, "nonexistent_module", "GRANT",
                         "test", actor_id=_STATE["su_id"])
    except ValueError as e:
        raised = "غير موجود" in str(e)
    assert raised, "unknown feature_code should be refused"
    return "typo in feature_code caught at service layer"


@check("8. Every write lands in platform_audit_logs")
def _():
    _setup()
    from app.services.company_overrides import (
        upsert_override, revoke_override,
    )
    from app.models import PlatformAuditLog
    co = _co(_STATE["starter_co_id"])
    before = PlatformAuditLog.query.filter(
        PlatformAuditLog.action.like("override_%")
    ).count()
    r1 = upsert_override(co.id, "hr", "GRANT", "grant test",
                          actor_id=_STATE["su_id"])
    r2 = upsert_override(co.id, "manufacturing", "DENY", "deny test",
                          actor_id=_STATE["su_id"])
    revoke_override(r1.id, actor_id=_STATE["su_id"])
    after = PlatformAuditLog.query.filter(
        PlatformAuditLog.action.like("override_%")
    ).count()
    assert after - before == 3, (
        f"expected 3 audit rows (grant + deny + revoke); "
        f"got {after - before}")
    return "3 platform_audit_log rows written"


@check("9. GET /admin/overrides renders 200 + empty state")
def _():
    _setup()
    c = _client_as(_STATE["su_id"])
    r = c.get("/admin/overrides")
    assert r.status_code == 200, (
        f"expected 200, got {r.status_code}: {r.get_data(as_text=True)[:200]}")
    body = r.get_data(as_text=True)
    assert "استثناءات الشركات" in body, "page title missing"
    assert "لا توجد استثناءات" in body, (
        "empty state text missing — no rows yet, expected empty message")
    return "page renders 200 with empty state"


@check("10. POST /admin/overrides creates a row + redirects")
def _():
    _setup()
    from app.models import CompanyFeatureOverride
    c = _client_as(_STATE["su_id"])
    r = c.post("/admin/overrides", data={
        "company_id": str(_STATE["starter_co_id"]),
        "feature_code": "hr",
        "mode": "GRANT",
        "reason": "audit http create",
        "expires_at": "",
    }, follow_redirects=False)
    assert r.status_code == 302, (
        f"expected redirect after create, got {r.status_code}")
    row = CompanyFeatureOverride.query.filter_by(
        company_id=_STATE["starter_co_id"], feature_code="hr").first()
    assert row is not None, "row not created via HTTP POST"
    assert row.mode == "GRANT"
    assert row.reason == "audit http create"
    return f"POST created row id={row.id}"


@check("11. POST /admin/overrides/<id>/revoke deletes + audits")
def _():
    _setup()
    from app.services.company_overrides import upsert_override
    from app.models import CompanyFeatureOverride, PlatformAuditLog
    row = upsert_override(_STATE["starter_co_id"], "hr", "GRANT",
                           "to revoke", actor_id=_STATE["su_id"])
    row_id = row.id
    c = _client_as(_STATE["su_id"])
    r = c.post(f"/admin/overrides/{row_id}/revoke",
                follow_redirects=False)
    assert r.status_code == 302
    assert db.session.get(CompanyFeatureOverride, row_id) is None, (
        "row should be deleted after revoke")
    log = PlatformAuditLog.query.filter_by(
        action="override_revoke").order_by(
            PlatformAuditLog.id.desc()).first()
    assert log is not None, "revoke should have written an audit row"
    assert "hr" in (log.details or "")
    return "HTTP revoke deletes + logs"


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
        print("\n(cleaned up fixture companies)")
    print(f"\n────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
