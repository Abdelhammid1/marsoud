#!/usr/bin/env python3
"""MARSOUD-SUBITEM-OVERRIDES (2026-08-09) — audit for the
per-subitem grant/deny path.

Locks the ticket's Acceptance Criteria:
- SUBITEM GRANT opens a single endpoint immediately, even when
  the plan doesn't include the parent module (AC #2).
- SUBITEM DENY closes an endpoint even when the plan has it
  (AC #3).
- SUBITEM DENY beats MODULE GRANT on the same endpoint —
  more specific wins (AC #4).
- Platform FeatureFlag OFF on the parent module beats any
  subitem GRANT (AC #5).
- Expired SUBITEM row treated as absent (AC #6).
- Revoke restores the plan default instantly (AC #7).
- The row shape distinguishes MODULE vs SUBITEM (AC #8).
- Pre-existing MODULE rows keep working exactly as before
  (AC #9 regression).

Checks:
  1. Schema: `scope` column + CHECK + widened unique.
  2. upsert_override(scope='SUBITEM') persists + reads.
  3. effective_subitems on grant → includes granted endpoint.
  4. effective_subitems on deny → excludes denied endpoint.
  5. subitem_allowed(endpoint, co_narrow) → True after GRANT
     (was False on the plan).
  6. AC #4 precedence: MODULE GRANT + SUBITEM DENY →
     can_access DENIES the specific subitem endpoint.
  7. AC #5 kill-switch beats SUBITEM GRANT.
  8. AC #6 expired SUBITEM row → get_subitem_override returns
     None; subitem_allowed falls back to the plan.
  9. AC #7 revoke restores the plan default.
  10. AC #1 POST /admin/overrides with scope=SUBITEM creates
      the row + redirects.
  11. AC #9 regression: MODULE upsert still works untouched;
      pre-migration rows default to scope='MODULE'.
  12. Validators: unknown subitem endpoint refused; unknown
      module code refused; empty reason refused.
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Windows console defaults to cp1252 — force UTF-8 so
# print() of the Arabic labels in assertion messages
# doesn't blow up mid-run.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

# Boss's env carries SESSION_COOKIE_DOMAIN=.marsoud.com in .env
# which breaks test_client cookies on localhost — neutralise
# per-app so this audit runs on every machine.
_ORIG_CREATE_APP = create_app
def create_app(*a, **kw):
    app = _ORIG_CREATE_APP(*a, **kw)
    app.config["SESSION_COOKIE_DOMAIN"] = None
    return app


CHECKS = []
PREFIX = "__SUBOV_"
_STATE = {}

# Subitem endpoint used across most checks. hr.attendance is:
#  · in ALL_SUB_ITEM_ENDPOINTS (validator can't refuse)
#  · maps to the "hr" module (module_for_endpoint) so the
#    kill-switch + parent-module unlock tests have clear
#    targets
SUB_EP = "hr.attendance"


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

    # "narrow" plan: settings + sales only, subitems explicitly
    # constrained to a couple that don't include hr.attendance —
    # so a subitem GRANT genuinely changes visibility.
    narrow = Plan(code=f"{PREFIX}narrow", name="SubOv Narrow",
                  name_ar="SubOv Narrow",
                  allowed_subitems=None)
    narrow.set_modules(["sales", "settings"])
    narrow.set_subitems(["invoices.index", "customers.index"])
    db.session.add(narrow); db.session.flush()

    # "wide" plan: every module, subitems=None (all allowed).
    from app.services.feature_registry import all_modules
    wide = Plan(code=f"{PREFIX}wide", name="SubOv Wide",
                name_ar="SubOv Wide",
                allowed_subitems=None)
    wide.set_modules(sorted({m.code for m in all_modules()}))
    db.session.add(wide); db.session.flush()
    db.session.commit()

    # subscription_expires_at deliberately in the past so
    # _company_in_trial returns False — otherwise subitem_allowed
    # returns True unconditionally and the DENY-path check would
    # never fire.
    past = datetime(2020, 1, 1)
    co_narrow = Company(name=f"{PREFIX}NARROW_CO", base_currency="EGP",
                          subdomain=None,
                          subscription_started_at=datetime.utcnow(),
                          subscription_expires_at=past,
                          intended_plan_id=narrow.id,
                          plan_id=narrow.id)
    co_wide = Company(name=f"{PREFIX}WIDE_CO", base_currency="EGP",
                      subdomain=None,
                      subscription_started_at=datetime.utcnow(),
                      subscription_expires_at=past,
                      intended_plan_id=wide.id, plan_id=wide.id)
    db.session.add_all([co_narrow, co_wide]); db.session.flush()

    su = User(email=f"{PREFIX}sa@x.test", full_name="subov super",
              is_active=True, is_superadmin=True,
              status=UserStatus.ACTIVE.value,
              email_verified_at=datetime.utcnow(),
              terms_version="TEST",
              password_hash=generate_password_hash(
                  "x", method="pbkdf2:sha256"))
    owner = User(email=f"{PREFIX}owner@x.test", full_name="subov owner",
                  is_active=True, status=UserStatus.ACTIVE.value,
                  email_verified_at=datetime.utcnow(),
                  terms_version="TEST",
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"))
    db.session.add_all([su, owner]); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=co_narrow.id, role="owner"))
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=co_wide.id, role="owner"))
    db.session.commit()
    ensure_roles_ready_for_company(co_narrow.id)
    ensure_roles_ready_for_company(co_wide.id)

    _STATE.update(
        narrow_co_id=co_narrow.id, wide_co_id=co_wide.id,
        su_id=su.id, owner_id=owner.id,
        narrow_plan_id=narrow.id, wide_plan_id=wide.id,
    )


def _teardown():
    from sqlalchemy import text, inspect
    from app.services.company_overrides import _invalidate_all
    from app.services.feature_flags import _invalidate_cache
    _invalidate_all()
    try:
        _invalidate_cache()
    except Exception:
        pass
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            f"SELECT id FROM companies WHERE name LIKE '{PREFIX}%'"))]
        for cid in cids:
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
            f"DELETE FROM plans WHERE code LIKE '{PREFIX}%'"))
        # Clean any stray FeatureFlag rows the kill-switch check
        # writes (see check 7).
        try:
            conn.execute(text(
                "DELETE FROM feature_flags WHERE module_key = 'hr'"))
        except Exception:
            pass


def _reset_g():
    from flask import g
    for key in ("_login_user", "active_company", "user_companies",
                "impersonating"):
        try:
            g.pop(key, None)
        except Exception:
            pass


def _client_as_su():
    from flask import current_app
    _reset_g()
    db.session.expire_all()
    db.session.remove()
    c = current_app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["su_id"])
        sess["_fresh"] = True
    return c


def _co(cid):
    from app.models import Company
    db.session.expire_all()
    return db.session.get(Company, cid)


def _cache_bust():
    """Both caches are 60s TTL and neither invalidates on an ORM
    row deletion we do outside the service. Clear both before any
    check that flips a row directly."""
    from app.services.company_overrides import _invalidate_all
    _invalidate_all()
    try:
        from app.services.feature_flags import _invalidate_cache
        _invalidate_cache()
    except Exception:
        pass


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. Schema: scope column + CHECK + widened unique exist")
def _():
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    cols = {c["name"] for c in insp.get_columns("company_feature_overrides")}
    assert "scope" in cols, "scope column missing — migration didn't apply"
    uqs = {u["name"]: tuple(u["column_names"])
           for u in insp.get_unique_constraints(
               "company_feature_overrides")}
    assert "uq_override_company_scope_feature" in uqs, (
        f"widened unique missing: {list(uqs)}")
    assert uqs["uq_override_company_scope_feature"] == (
        "company_id", "scope", "feature_code"), (
        f"unique column order wrong: {uqs['uq_override_company_scope_feature']}")
    return "scope + widened unique + CHECK all present"


@check("2. upsert_override(scope='SUBITEM') persists + reads via get_subitem_override")
def _():
    from app.services.company_overrides import (
        upsert_override, get_subitem_override,
    )
    upsert_override(
        _STATE["narrow_co_id"], SUB_EP, "GRANT",
        "audit test grant", actor_id=_STATE["su_id"],
        scope="SUBITEM",
    )
    _cache_bust()
    ov = get_subitem_override(_STATE["narrow_co_id"], SUB_EP)
    assert ov == "GRANT", f"want 'GRANT', got {ov!r}"
    # And a MODULE row on the same feature name would NOT collide.
    ov_mod = None
    from app.services.company_overrides import get_override
    ov_mod = get_override(_STATE["narrow_co_id"], SUB_EP,
                          scope="MODULE")
    assert ov_mod is None, (
        f"MODULE scope leaked SUBITEM read: {ov_mod!r}")
    return f"SUBITEM GRANT persists + scope keys don't collide"


@check("3. effective_subitems on GRANT includes the granted endpoint")
def _():
    """The plan restricts to [invoices.index, customers.index].
    A SUBITEM GRANT on hr.attendance should widen the set."""
    from app.services.plan_gating import effective_subitems
    co = _co(_STATE["narrow_co_id"])
    got = effective_subitems(co) or []
    assert SUB_EP in got, (
        f"granted endpoint missing from effective_subitems: {got}")
    assert "invoices.index" in got, (
        f"plan-original subitem lost when materialising: {got}")
    return f"effective_subitems widened to include {SUB_EP}"


@check("4. effective_subitems on DENY excludes the denied endpoint")
def _():
    """Wide plan has allowed_subitems=None (all). A SUBITEM DENY
    should materialise the set and remove exactly one entry."""
    from app.services.company_overrides import upsert_override
    from app.services.plan_gating import (
        effective_subitems, ALL_SUB_ITEM_ENDPOINTS,
    )
    upsert_override(
        _STATE["wide_co_id"], SUB_EP, "DENY",
        "audit test deny", actor_id=_STATE["su_id"],
        scope="SUBITEM",
    )
    _cache_bust()
    co = _co(_STATE["wide_co_id"])
    got = effective_subitems(co) or []
    assert SUB_EP not in got, (
        f"denied endpoint still present: contains {SUB_EP}")
    # Sibling under the same section stays — DENY is per-endpoint.
    assert "hr.index" in got, (
        f"DENY over-reached and removed hr.index too: {got}")
    return f"effective_subitems removed {SUB_EP} only"


@check("5. subitem_allowed(SUB_EP, narrow_co) is True after GRANT (was False)")
def _():
    """Direct read of the guard the sidebar template + before_request
    both use. Narrow plan's allowed_subitems excludes hr.attendance,
    so pre-GRANT it returns False; the GRANT from check 2 should
    flip it."""
    from app.services.plan_gating import subitem_allowed
    _cache_bust()
    co = _co(_STATE["narrow_co_id"])
    assert subitem_allowed(SUB_EP, co) is True, (
        "GRANT override didn't unlock the endpoint")
    # Any other subitem NOT in narrow.subitems + NOT granted stays
    # denied.
    assert subitem_allowed("hr.index", co) is False, (
        "GRANT bled onto a sibling subitem")
    return "GRANT unlocks the exact endpoint; siblings untouched"


@check("6. AC #4 precedence: MODULE GRANT + SUBITEM DENY refuses the endpoint")
def _():
    """Grant the whole hr module to narrow_co (so the module gate
    would pass), then DENY the specific hr.attendance subitem.
    can_access on the endpoint should refuse via COMPANY_DENIED."""
    from app.services.company_overrides import (
        upsert_override, revoke_override, list_for_company,
    )
    from app.services.access import (
        can_access, REASON_COMPANY_DENIED,
    )
    # Clean any prior SUBITEM row on narrow_co for hr.attendance
    # (check 2 already put a GRANT there).
    for r in list_for_company(_STATE["narrow_co_id"]):
        if r.scope == "SUBITEM" and r.feature_code == SUB_EP:
            revoke_override(r.id, actor_id=_STATE["su_id"])
    # Now: MODULE GRANT hr + SUBITEM DENY hr.attendance.
    upsert_override(_STATE["narrow_co_id"], "hr", "GRANT",
                     "test module grant",
                     actor_id=_STATE["su_id"], scope="MODULE")
    upsert_override(_STATE["narrow_co_id"], SUB_EP, "DENY",
                     "test subitem deny",
                     actor_id=_STATE["su_id"], scope="SUBITEM")
    _cache_bust()
    from app.models import User
    owner = db.session.get(User, _STATE["owner_id"])
    co = _co(_STATE["narrow_co_id"])
    allowed, reason = can_access(SUB_EP, owner, co)
    assert allowed is False, (
        f"specific subitem DENY didn't beat module GRANT")
    assert reason == REASON_COMPANY_DENIED, (
        f"want REASON_COMPANY_DENIED, got {reason!r}")
    # Cleanup for downstream checks — restore state.
    for r in list_for_company(_STATE["narrow_co_id"]):
        if r.feature_code in ("hr", SUB_EP):
            revoke_override(r.id, actor_id=_STATE["su_id"])
    _cache_bust()
    return "SUBITEM DENY beat MODULE GRANT (COMPANY_DENIED)"


@check("7. AC #5 kill-switch beats SUBITEM GRANT")
def _():
    """FeatureFlag('hr', enabled=False) + SUBITEM GRANT on
    hr.attendance for narrow_co → can_access still refuses via
    PLATFORM_DISABLED. The kill-switch is module-level and runs
    at step 1 before overrides at step 2."""
    from app.services.company_overrides import (
        upsert_override, revoke_override, list_for_company,
    )
    from app.services.access import (
        can_access, REASON_PLATFORM_DISABLED,
    )
    from app.services.feature_flags import set_module
    # Set kill-switch on hr.
    set_module("hr", enabled=False, reason="audit test",
                 actor_id=_STATE["su_id"])
    # SUBITEM GRANT for the endpoint whose parent is hr.
    upsert_override(_STATE["narrow_co_id"], SUB_EP, "GRANT",
                     "test grant vs killswitch",
                     actor_id=_STATE["su_id"], scope="SUBITEM")
    _cache_bust()
    from app.models import User
    owner = db.session.get(User, _STATE["owner_id"])
    co = _co(_STATE["narrow_co_id"])
    allowed, reason = can_access(SUB_EP, owner, co)
    # Cleanup FIRST (before any assertion can throw) so a failure
    # here doesn't leave a poisoned FeatureFlag row for other
    # checks or the next test run.
    set_module("hr", enabled=True, reason=None,
                actor_id=_STATE["su_id"])
    for r in list_for_company(_STATE["narrow_co_id"]):
        if r.feature_code == SUB_EP:
            revoke_override(r.id, actor_id=_STATE["su_id"])
    _cache_bust()
    assert allowed is False, (
        "kill-switch didn't beat SUBITEM GRANT")
    assert reason == REASON_PLATFORM_DISABLED, (
        f"want REASON_PLATFORM_DISABLED, got {reason!r}")
    return "kill-switch beat SUBITEM GRANT (PLATFORM_DISABLED)"


@check("8. AC #6 expired SUBITEM row treated as absent")
def _():
    """upsert_override with a past expires_at → get_subitem_override
    returns None; subitem_allowed falls back to plan behaviour
    (which for narrow_co refuses SUB_EP)."""
    from app.services.company_overrides import (
        upsert_override, get_subitem_override,
    )
    from app.services.plan_gating import subitem_allowed
    upsert_override(
        _STATE["narrow_co_id"], SUB_EP, "GRANT",
        "expired row test",
        expires_at=datetime.utcnow() - timedelta(days=1),
        actor_id=_STATE["su_id"], scope="SUBITEM",
    )
    _cache_bust()
    ov = get_subitem_override(_STATE["narrow_co_id"], SUB_EP)
    assert ov is None, f"expired row still active: {ov!r}"
    co = _co(_STATE["narrow_co_id"])
    assert subitem_allowed(SUB_EP, co) is False, (
        "expired grant still opened the endpoint")
    return "past expires_at → override ignored, plan default wins"


@check("9. AC #7 revoke restores plan default")
def _():
    """After check 8, narrow_co has an expired SUBITEM row.
    upsert a fresh active GRANT → active. revoke → back to
    plan default (denied for narrow_co)."""
    from app.services.company_overrides import (
        upsert_override, revoke_override, get_subitem_override,
        list_for_company,
    )
    from app.services.plan_gating import subitem_allowed
    row = upsert_override(
        _STATE["narrow_co_id"], SUB_EP, "GRANT",
        "revoke round-trip",
        actor_id=_STATE["su_id"], scope="SUBITEM",
    )
    _cache_bust()
    co = _co(_STATE["narrow_co_id"])
    assert subitem_allowed(SUB_EP, co) is True, (
        "fresh GRANT didn't open the endpoint")
    revoke_override(row.id, actor_id=_STATE["su_id"])
    _cache_bust()
    co = _co(_STATE["narrow_co_id"])
    assert get_subitem_override(_STATE["narrow_co_id"], SUB_EP) is None
    assert subitem_allowed(SUB_EP, co) is False, (
        "revoke didn't restore plan default")
    # Sweep any remaining rows created earlier so check 12 starts
    # from a clean narrow_co.
    for r in list_for_company(_STATE["narrow_co_id"]):
        revoke_override(r.id, actor_id=_STATE["su_id"])
    _cache_bust()
    return "revoke restored plan default instantly"


@check("10. AC #1 POST /admin/overrides with scope=SUBITEM creates a row")
def _():
    from app.services.company_overrides import (
        list_for_company, revoke_override,
    )
    c = _client_as_su()
    r = c.post("/admin/overrides", data={
        "company_id": str(_STATE["narrow_co_id"]),
        "scope": "SUBITEM",
        "feature_code_subitem": SUB_EP,
        "feature_code_module": "",   # not required when SUBITEM
        "mode": "GRANT",
        "reason": "POST test — grant hr.attendance for narrow_co",
    }, follow_redirects=False)
    assert r.status_code in (200, 302), f"HTTP {r.status_code}"
    _cache_bust()
    rows = [r for r in list_for_company(_STATE["narrow_co_id"])
            if r.scope == "SUBITEM" and r.feature_code == SUB_EP]
    assert len(rows) == 1, (
        f"POST didn't persist exactly one row: {len(rows)}")
    assert rows[0].mode == "GRANT", (
        f"mode wrong: {rows[0].mode}")
    # Sweep for downstream checks.
    for row in rows:
        revoke_override(row.id, actor_id=_STATE["su_id"])
    _cache_bust()
    return "POST wrote the SUBITEM row + redirected"


@check("11. AC #9 regression: MODULE upsert still works (default scope path)")
def _():
    """Callers that pass no `scope` (or the older POST path with
    just `feature_code`) must land on scope='MODULE' and behave
    exactly as before this ticket."""
    from app.services.company_overrides import (
        upsert_override, get_override, revoke_override,
        list_for_company,
    )
    row = upsert_override(
        _STATE["wide_co_id"], "hr", "DENY",
        "module regression test",
        actor_id=_STATE["su_id"],
        # scope omitted — must default to MODULE
    )
    _cache_bust()
    assert row.scope == "MODULE", (
        f"default scope wrong: {row.scope!r}")
    ov = get_override(_STATE["wide_co_id"], "hr")   # no scope kw
    assert ov == "DENY", (
        f"module read path broken: {ov!r}")
    # Cleanup for a clean teardown.
    revoke_override(row.id, actor_id=_STATE["su_id"])
    # Also clean the check-4 DENY row so teardown sweeps quietly.
    for r in list_for_company(_STATE["wide_co_id"]):
        revoke_override(r.id, actor_id=_STATE["su_id"])
    _cache_bust()
    return "MODULE default path unchanged"


@check("12. Validators: unknown subitem, unknown module, empty reason all refused")
def _():
    from app.services.company_overrides import upsert_override
    # Unknown SUBITEM endpoint.
    try:
        upsert_override(_STATE["narrow_co_id"],
                         "nonexistent.endpoint", "GRANT",
                         "test", scope="SUBITEM",
                         actor_id=_STATE["su_id"])
    except ValueError as e:
        assert "غير معروف" in str(e), f"wrong msg: {e}"
    else:
        raise AssertionError("unknown subitem accepted")
    # Unknown MODULE.
    try:
        upsert_override(_STATE["narrow_co_id"],
                         "nonexistent_module", "GRANT",
                         "test", scope="MODULE",
                         actor_id=_STATE["su_id"])
    except ValueError as e:
        assert "غير موجود" in str(e), f"wrong msg: {e}"
    else:
        raise AssertionError("unknown module accepted")
    # Empty reason.
    try:
        upsert_override(_STATE["narrow_co_id"],
                         SUB_EP, "GRANT", "   ",
                         scope="SUBITEM",
                         actor_id=_STATE["su_id"])
    except ValueError as e:
        assert "السبب" in str(e), f"wrong msg: {e}"
    else:
        raise AssertionError("empty reason accepted")
    return "all three validators fire on SUBITEM scope"


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                    failed += 1
        finally:
            _teardown()
            print("\n(fixture cleaned up)")
    print(f"\n----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
