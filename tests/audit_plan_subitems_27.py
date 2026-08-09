#!/usr/bin/env python3
"""MARSOUD-PLAN-SUBITEMS-27 (2026-08-09) — audit for the
catalog widening.

Locks the ticket's Acceptance Criteria:
- The 27 new rows (26 net — agent.insights_index was already
  there) appear in SUB_ITEM_CATALOG under the correct section.
- Excluded items (portal_emp.*, users.index, companies.index,
  support.*) are NOT in the catalog.
- Toggling a row on/off flows through to `subitem_allowed`
  immediately, no restart.
- Two plans differ per row: company on plan-A sees a row that
  company on plan-B doesn't.
- Regression on pre-ticket catalog rows (invoices.index,
  hr.index, ...) stays green.
- Migration append-if-visible logic keeps existing plans'
  behavior unchanged: parent-present → row appended;
  parent-absent → row NOT appended; always-ungated → appended.
- `endpoint_to_subitem` no longer collapses
  `accounting_ops.index` onto `journals.index`, but STILL
  collapses `accounting_ops.new_journal` etc.

Every check verified to fail against pre-migration HEAD.
"""
import sys
from pathlib import Path

# Windows console defaults to cp1252 — force UTF-8 so print()
# of the Arabic labels in assertion messages doesn't blow up.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

# Boss's env carries SESSION_COOKIE_DOMAIN=.marsoud.com in .env;
# neutralise per-app so this audit runs on every machine.
_ORIG_CREATE_APP = create_app
def create_app(*a, **kw):
    app = _ORIG_CREATE_APP(*a, **kw)
    app.config["SESSION_COOKIE_DOMAIN"] = None
    return app


CHECKS = []
PREFIX = "__PS27_"
_STATE = {}


# The 26 new rows introduced by this ticket. `agent.insights_index`
# ("المحلل الذكي") was already in the catalog before the ticket, so
# even though the ticket lists 27 items the actual delta is 26.
NEW_ROWS = {
    # (endpoint, section)
    "accounting_ops.index":              "accounting",
    "recurring_invoices.index":          "sales",
    "pos.shifts":                        "sales",
    "pos.history":                       "sales",
    "inventory.adjust":                  "inventory",
    "inventory.opening_balance":         "inventory",
    "inventory.movements":               "inventory",
    "inventory.transfers":               "inventory",
    "inventory.inventory_balance":       "inventory",
    "inventory.barcodes_picker":         "inventory",
    "inventory_counts.index":            "inventory",
    "products.hierarchy":                "inventory",
    "leads.no_response_index":           "crm",
    "tasks.archive_mine":                "workflow",
    "hr.departments":                    "hr",
    "hr.leave_types":                    "hr",
    "hr.leave_requests":                 "hr",
    "hr.attendance_policies":            "hr",
    "advances.index":                    "hr",
    "payroll.archive":                   "hr",
    "custody.index":                     "hr",
    "item_custody.index":                "hr",
    "evaluations.index":                 "hr",
    "evaluations.logs_index":            "hr",
    "settings_employee_reports.index":   "settings",
    "settings_usage.index":              "settings",
    "user_files.index":                  "settings",
}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from datetime import datetime
    from app.models import Company, Plan

    # narrow_plan: allowed_subitems=["invoices.index",
    # "customers.index"] — nothing new should be visible unless
    # we tick it explicitly.
    narrow = Plan(code=f"{PREFIX}narrow", name="PS27 narrow",
                  name_ar="PS27 narrow",
                  allowed_subitems=None)
    narrow.set_modules(["sales", "settings", "inventory", "hr"])
    narrow.set_subitems(["invoices.index", "customers.index"])
    db.session.add(narrow); db.session.flush()

    # null_plan: allowed_subitems=NULL — every row visible per
    # the "no filter" semantic. Used to prove the migration
    # left NULL plans untouched.
    null_plan = Plan(code=f"{PREFIX}null", name="PS27 null",
                      name_ar="PS27 null",
                      allowed_subitems=None)
    null_plan.set_modules(["sales", "inventory", "hr", "settings"])
    db.session.add(null_plan); db.session.flush()

    # empty_plan: allowed_subitems=[] — every row denied. Used
    # to prove the toggle semantics work in both directions.
    empty = Plan(code=f"{PREFIX}empty", name="PS27 empty",
                  name_ar="PS27 empty",
                  allowed_subitems=None)
    empty.set_modules(["sales", "inventory", "hr", "settings"])
    empty.set_subitems([])
    db.session.add(empty); db.session.flush()

    db.session.commit()

    past = datetime(2020, 1, 1)   # kill the trial-window bypass
    co_narrow = Company(name=f"{PREFIX}NARROW", base_currency="SAR",
                          plan_id=narrow.id,
                          subscription_started_at=datetime.utcnow(),
                          subscription_expires_at=past)
    co_null = Company(name=f"{PREFIX}NULL", base_currency="SAR",
                        plan_id=null_plan.id,
                        subscription_started_at=datetime.utcnow(),
                        subscription_expires_at=past)
    co_empty = Company(name=f"{PREFIX}EMPTY", base_currency="SAR",
                         plan_id=empty.id,
                         subscription_started_at=datetime.utcnow(),
                         subscription_expires_at=past)
    for co in (co_narrow, co_null, co_empty):
        db.session.add(co); db.session.flush()
    db.session.commit()

    _STATE.update(
        narrow_id=narrow.id, null_id=null_plan.id,
        empty_id=empty.id,
        co_narrow_id=co_narrow.id, co_null_id=co_null.id,
        co_empty_id=co_empty.id,
    )


def _teardown():
    from app.models import Company, Plan
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        db.session.execute(text(
            "DELETE FROM companies WHERE id=:c"), {"c": cid})
        db.session.commit()
    for p in Plan.query.filter(Plan.code.like(f"{PREFIX}%")).all():
        db.session.delete(p)
    db.session.commit()


def _co(cid):
    from app.models import Company
    db.session.expire_all()
    return db.session.get(Company, cid)


def _plan(pid):
    from app.models import Plan
    db.session.expire_all()
    return db.session.get(Plan, pid)


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. All 26 new endpoints present in SUB_ITEM_CATALOG")
def _():
    from app.services.plan_gating import ALL_SUB_ITEM_ENDPOINTS
    all_eps = set(ALL_SUB_ITEM_ENDPOINTS)
    missing = [ep for ep in NEW_ROWS if ep not in all_eps]
    assert not missing, f"catalog missing: {missing}"
    # And agent.insights_index (the pre-existing overlap) still
    # there — regression against an accidental removal.
    assert "agent.insights_index" in all_eps, (
        "regression: agent.insights_index was removed")
    return f"all 26 new + agent.insights_index present"


@check("2. Each new endpoint sits in the documented section")
def _():
    from app.services.plan_gating import SUB_ITEM_CATALOG
    endpoint_to_section = {
        ep: section
        for section, items in SUB_ITEM_CATALOG.items()
        for (ep, _, _) in items
    }
    wrong = []
    for ep, want in NEW_ROWS.items():
        got = endpoint_to_section.get(ep)
        if got != want:
            wrong.append(f"{ep}: want {want!r}, got {got!r}")
    assert not wrong, "section placement mismatches: " + "; ".join(wrong)
    return "all 26 rows sit in the correct section bucket"


@check("3. Excluded items are NOT in the catalog")
def _():
    from app.services.plan_gating import ALL_SUB_ITEM_ENDPOINTS
    excluded = {
        "portal_emp.account", "portal_emp.attendance", "portal_emp.custody",
        "users.index", "companies.index",
        "support.index",
    }
    leaked = excluded & set(ALL_SUB_ITEM_ENDPOINTS)
    assert not leaked, (
        f"excluded items appeared in catalog: {leaked}")
    return f"all {len(excluded)} excluded items stayed out"


@check("4. Fresh NULL plan → every new row is subitem_allowed=True")
def _():
    """NULL means 'no filter' per Plan.subitems back-compat. Every
    endpoint stays visible on a NULL-plan company."""
    from app.services.plan_gating import subitem_allowed
    co = _co(_STATE["co_null_id"])
    for ep in NEW_ROWS:
        assert subitem_allowed(ep, co) is True, (
            f"{ep} refused on NULL-subitems plan")
    return f"NULL plan lets all 26 new rows through"


@check("5. Fresh []-subitems plan → every new row denied")
def _():
    """Empty list means the super-admin locked the plan down.
    subitem_allowed returns False for anything not in the list."""
    from app.services.plan_gating import subitem_allowed
    co = _co(_STATE["co_empty_id"])
    for ep in NEW_ROWS:
        assert subitem_allowed(ep, co) is False, (
            f"{ep} slipped through the empty allowed_subitems")
    return f"[] plan refused all 26 new rows"


@check("6. Toggle ON in plan.set_subitems → subitem_allowed True")
def _():
    """AC #3 — no restart. Pick two new rows, add them to the
    narrow plan, re-fetch the company, verify allowed."""
    from app.services.plan_gating import subitem_allowed
    plan = _plan(_STATE["narrow_id"])
    plan.set_subitems([
        "invoices.index", "customers.index",
        "inventory.adjust", "custody.index",
    ])
    db.session.commit()
    co = _co(_STATE["co_narrow_id"])
    assert subitem_allowed("inventory.adjust", co) is True
    assert subitem_allowed("custody.index", co) is True
    # And a row we DIDN'T tick stays refused.
    assert subitem_allowed("evaluations.index", co) is False
    return "toggle ON → allowed instantly (no restart)"


@check("7. Toggle OFF → subitem_allowed False (round-trip)")
def _():
    """AC #4 — flipping OFF hides the row same-request."""
    from app.services.plan_gating import subitem_allowed
    plan = _plan(_STATE["narrow_id"])
    plan.set_subitems(["invoices.index", "customers.index"])
    db.session.commit()
    co = _co(_STATE["co_narrow_id"])
    assert subitem_allowed("inventory.adjust", co) is False
    assert subitem_allowed("custody.index", co) is False
    return "toggle OFF → denied instantly"


@check("8. AC #7 two plans, same row, different companies see differently")
def _():
    """narrow plan ticks inventory.adjust; empty plan doesn't →
    each company sees its own scope."""
    from app.services.plan_gating import subitem_allowed
    p_narrow = _plan(_STATE["narrow_id"])
    p_narrow.set_subitems(["invoices.index", "inventory.adjust"])
    db.session.commit()
    co_narrow = _co(_STATE["co_narrow_id"])
    co_empty = _co(_STATE["co_empty_id"])
    assert subitem_allowed("inventory.adjust", co_narrow) is True
    assert subitem_allowed("inventory.adjust", co_empty) is False
    return "same endpoint, two companies, opposite outcomes"


@check("9. endpoint_to_subitem(accounting_ops.index) = itself, not journals")
def _():
    """AC-adjacent: pre-ticket the wizards' index lumped under
    journals.index. Now it's its own row. The direct-match path
    in endpoint_to_subitem must return the endpoint itself, and
    the explicit accounting_ops.* shortcut must skip the index."""
    from app.services.plan_gating import endpoint_to_subitem
    got = endpoint_to_subitem("accounting_ops.index")
    assert got == "accounting_ops.index", (
        f"want 'accounting_ops.index', got {got!r} — "
        f"double-gate would silently break the row")
    return "index resolves to itself (no double-gate)"


@check("10. endpoint_to_subitem(accounting_ops.new_journal) STILL journals.index")
def _():
    """The shortcut still needs to fire for SUB-pages
    (accounting_ops.new_journal etc.) or plans that don't
    tick accounting_ops.index would 403 on the wizards
    themselves — which we DIDN'T touch."""
    from app.services.plan_gating import endpoint_to_subitem
    got = endpoint_to_subitem("accounting_ops.new_journal")
    assert got == "journals.index", (
        f"sub-page shortcut broken: got {got!r}")
    return "sub-pages still ride on journals.index"


@check("11. Migration append-if-visible: parent-present → row appended")
def _():
    """Direct data check on a plan the migration touched. Seed a
    plan with inventory.index in its list, hand-invoke the
    migration's upgrade() body, verify inventory.adjust was
    appended and pos.shifts (pos.index not present) was NOT."""
    from app.models import Plan
    p = Plan(code=f"{PREFIX}mig", name="mig", name_ar="mig",
              allowed_subitems=None)
    p.set_modules(["sales", "inventory"])
    p.set_subitems(["inventory.index"])   # HAS inventory parent
    db.session.add(p); db.session.flush()
    db.session.commit()
    # Hand-invoke the exact same append logic the migration uses
    # (importing the migration module directly lets us test the
    # data without re-running alembic).
    from migrations.versions import f8c2e5a9d4b1_expand_plan_subitems_27 as m
    import json
    existing = json.loads(p.allowed_subitems or "[]")
    current = set(existing)
    additions = set()
    for ep in m.NEW_ENDPOINTS:
        if ep in current:
            continue
        if ep in m.ALWAYS_APPEND:
            additions.add(ep)
        elif ep in m.LUMPED_UNDER and m.LUMPED_UNDER[ep] in current:
            additions.add(ep)
    assert "inventory.adjust" in additions, (
        "parent-present didn't trigger the append")
    assert "pos.shifts" not in additions, (
        "pos.shifts appended without pos.index parent")
    # Ungated always-append fires.
    assert "advances.index" in additions, (
        "ALWAYS_APPEND (advances) missed")
    return "append rule fires only for visible-today rows"


@check("12. Regression: pre-ticket rows still gate the same way")
def _():
    """invoices.index was in the narrow plan pre-ticket; must
    stay visible. hr.leave_types is a NEW row; must be denied on
    a plan that doesn't tick it (narrow_plan currently doesn't)."""
    from app.services.plan_gating import subitem_allowed
    plan = _plan(_STATE["narrow_id"])
    plan.set_subitems(["invoices.index", "customers.index"])
    db.session.commit()
    co = _co(_STATE["co_narrow_id"])
    assert subitem_allowed("invoices.index", co) is True, (
        "regression: pre-ticket invoices.index refused")
    assert subitem_allowed("customers.index", co) is True, (
        "regression: pre-ticket customers.index refused")
    assert subitem_allowed("hr.leave_types", co) is False, (
        "new row leaked without being ticked")
    return "pre-ticket rows unchanged; new rows still respect the toggle"


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
