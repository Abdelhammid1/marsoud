#!/usr/bin/env python3
"""MARSOUD-PLAN-BUNDLE-FIXES-01 — plan matrix + trial enforcement.

Two bugs this audit locks down:

1. **Plan modules match the marketing image.** Starter has the
   entry-tier modules; Growth adds CRM but NOT HR (image says HR
   is Pro-only); Pro has HR + employee_reports + manufacturing.
   Regression guard because it's very easy to accidentally add a
   module to the wrong tier in `PLAN_SEED`.

2. **Trial no longer overrides a picked plan.** Before this ticket,
   a company inside its trial window got full-feature access
   regardless of which plan they picked at /choose-plan. That
   meant "I picked Starter" had zero effect for 14 days. Now the
   moment `intended_plan_id` is set, gating uses that plan's
   modules even inside the trial window.

Checks:
  1. Starter modules == marketing image
  2. Growth modules == marketing image (no hr)
  3. Pro modules == marketing image
  4. In-trial company with intended=Starter cannot use hr.view/hr.index
  5. In-trial company with intended=Pro CAN use hr.view/hr.index
  6. In-trial company with no pick at all still gets full access
  7. sync_plans_from_seed heals drift when a module is removed
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
COMPANY_NAME = "__PLAN_BUNDLES_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── image reference ─────────────────────────────────────────────────
# What the marketing plan-comparison matrix says. Extras that live
# outside that table (evaluations / cash_custody / insights) are
# checked separately as "Pro-and-only-Pro".
STARTER_MODULES = {
    "accounting", "sales", "purchases", "reports",
    "agent", "inventory", "pos",
}
GROWTH_ADDS = {"crm"}                 # on top of Starter
PRO_ADDS = {"hr", "employee_reports", "manufacturing"}
PRO_EXTRAS = {"evaluations", "cash_custody", "insights"}  # not in image


def _setup():
    from app.models import Plan, Company
    from app.services.subscription import activate_default_subscription
    _teardown()

    # Boot shim already ran sync_plans_from_seed via create_app; the
    # three canonical plans are guaranteed present here. We store IDs
    # rather than instances because each check runs inside its own
    # app_context and detached instances trip DetachedInstanceError.
    for code in ("starter", "growth", "pro"):
        row = Plan.query.filter_by(code=code).first()
        assert row is not None, (
            f"PLAN_SEED did not populate '{code}' — check the "
            f"boot shim in app/__init__.py")
        _STATE[f"{code}_id"] = row.id

    # Fixture company for the trial-enforcement checks. Trial is set
    # explicitly so we're not sensitive to trial_days platform setting.
    co = Company(name=COMPANY_NAME, base_currency="EGP", vat_rate=0)
    db.session.add(co)
    db.session.flush()
    activate_default_subscription(co, plan_code=None)  # both plan ids NULL
    co.subscription_expires_at = datetime.utcnow() + timedelta(days=7)
    db.session.commit()
    _STATE["co_id"] = co.id


def _teardown():
    from app.models import Company
    for co in Company.query.filter_by(name=COMPANY_NAME).all():
        db.session.delete(co)
    db.session.commit()


def _co():
    from app.models import Company
    return db.session.get(Company, _STATE["co_id"])


def _plan(code):
    from app.models import Plan
    return db.session.get(Plan, _STATE[f"{code}_id"])


def _set_intended(code_or_none):
    """Point the fixture at a specific plan via intended_plan_id.
    plan_id stays NULL — that's the whole point (proves that a mere
    /choose-plan pick is enough to trigger gating, even before
    super-admin promotes it to plan_id)."""
    co = _co()
    co.intended_plan_id = _plan(code_or_none).id if code_or_none else None
    db.session.commit()


@check("Starter modules match the marketing image")
def _starter_matches():
    got = set(_plan("starter").modules or [])
    assert got == STARTER_MODULES, (
        f"Starter modules drifted from image:\n"
        f"  expected: {sorted(STARTER_MODULES)}\n"
        f"  got:      {sorted(got)}")
    return f"{len(got)} modules exactly"


@check("Growth = Starter + CRM (NO hr / employee_reports / manufacturing)")
def _growth_matches():
    got = set(_plan("growth").modules or [])
    expected = STARTER_MODULES | GROWTH_ADDS
    forbidden = PRO_ADDS
    assert got == expected, (
        f"Growth modules drifted from image:\n"
        f"  expected: {sorted(expected)}\n"
        f"  got:      {sorted(got)}")
    leaks = got & forbidden
    assert not leaks, f"Growth leaks Pro-only modules: {sorted(leaks)}"
    return f"{len(got)} modules, no Pro leaks"


@check("Pro has HR + employee_reports + manufacturing + all Pro extras")
def _pro_matches():
    got = set(_plan("pro").modules or [])
    required = STARTER_MODULES | GROWTH_ADDS | PRO_ADDS
    missing_required = required - got
    missing_extras = PRO_EXTRAS - got
    assert not missing_required, (
        f"Pro is missing image-table modules: {sorted(missing_required)}")
    assert not missing_extras, (
        f"Pro is missing off-image extras (evaluations/cash_custody/"
        f"insights): {sorted(missing_extras)}")
    return f"{len(got)} modules; image-required + extras present"


@check("In-trial Starter pick refuses hr — coarse module gate wins")
def _starter_enforced_in_trial():
    from app.services.plan_gating import plan_allows
    from app.services.permissions import has_permission
    from app.models import User
    _set_intended("starter")
    co = _co()
    # The main fix. plan_allows had NO trial bypass but only checked
    # `subscription_plan` (plan_id). With `intended_plan_id`-only
    # picks (the default state after /choose-plan), it was returning
    # True for everything for the trial's duration.
    assert plan_allows("hr.view", co) is False, (
        "plan_allows should refuse hr.view for an in-trial Starter — "
        "intended_plan_id fallback isn't working")
    assert plan_allows("payroll.run", co) is False, (
        "hr-prefixed permissions should also be refused")
    assert plan_allows("manufacturing.view", co) is False, (
        "Pro-only manufacturing should also be refused")
    # Sanity check the positive side of the gate still fires.
    assert plan_allows("invoices.view", co) is True, (
        "starter should still allow sales.invoices.view")
    assert plan_allows("pos.use", co) is True, (
        "starter should still allow pos.use")
    return "hr/payroll/manufacturing blocked; sales/pos allowed"


@check("In-trial Pro pick allows hr — no false-positive gate")
def _pro_allows_in_trial():
    from app.services.plan_gating import plan_allows
    _set_intended("pro")
    co = _co()
    assert plan_allows("hr.view", co) is True, (
        "plan_allows should allow hr.view for an in-trial Pro pick")
    assert plan_allows("payroll.run", co) is True, (
        "plan_allows should allow payroll.run for an in-trial Pro pick")
    assert plan_allows("manufacturing.view", co) is True, (
        "plan_allows should allow manufacturing.view for Pro")
    assert plan_allows("custody.manage", co) is True, (
        "Pro should also carry the cash_custody module (drift check)")
    return "hr + payroll + manufacturing + custody allowed under Pro"


@check("No pick at all — pre-/choose-plan onboarding still open")
def _no_pick_open():
    from app.services.plan_gating import plan_allows, subitem_allowed
    _set_intended(None)                    # both plan_id + intended_plan_id NULL
    co = _co()
    assert plan_allows("hr.view", co) is True, (
        "with no plan picked, plan_allows should return True "
        "(pre-onboarding back-compat)")
    assert subitem_allowed("hr.index", co) is True, (
        "with no plan picked, subitem_allowed should show every "
        "sub-item (the /choose-plan redirect handles the UX bounds)")
    return "unpicked signup still sees full sidebar"


@check("sync_plans_from_seed heals drift when a module is removed")
def _drift_heal():
    from app.cli import sync_plans_from_seed
    pro = _plan("pro")
    original = list(pro.modules or [])
    # Deliberately drift the DB: strip cash_custody (matches the exact
    # incident that motivated the drift auto-heal).
    stripped = [m for m in original if m != "cash_custody"]
    pro.set_modules(stripped)
    db.session.commit()
    assert "cash_custody" not in (_plan("pro").modules or [])

    summary = sync_plans_from_seed()
    assert "cash_custody" in (_plan("pro").modules or []), (
        "sync_plans_from_seed did not restore cash_custody")
    assert "pro" in summary["updated"], (
        f"summary should name 'pro' as updated; got {summary['updated']}")

    # Re-run: should now be a no-op (idempotent).
    summary2 = sync_plans_from_seed()
    assert "pro" not in summary2["updated"], (
        f"second run should be no-op for pro; got {summary2['updated']}")
    return "drift healed on first sync; second sync is no-op"


def main():
    app = create_app()
    _STATE["app"] = app
    with app.app_context():
        _setup()
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
        print("\n(cleaned up fixture company)")
    print(f"\n────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
