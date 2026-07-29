#!/usr/bin/env python3
"""MARSOUD-EVALUATIONS-PRO-GATING (Abdelhamid 2026-07-29).

Batch 5 Ticket 6 audit. Evaluations feature must be Pro-plan only
+ HR-scope permission only.

Checks:
  1. plan_gating.action_module("evaluations.manage") returns
     "evaluations" (mapping registered).
  2. plan_allows("evaluations.manage", pro_company) returns True.
  3. plan_allows("evaluations.manage", starter_company) returns
     False.
  4. plan_allows("evaluations.manage", growth_company) returns
     False.
  5. permissions.P["evaluations.manage"] == {owner, admin,
     hr_manager} — no sales / PM / finance leakage.
  6. All 17 evaluations routes use @require_permission
     ("evaluations.manage") — none left on "users.manage".
  7. cli.PLAN_SEED — Pro plan modules include "evaluations",
     Starter + Growth do NOT.
  8. roles_seed.PERMISSION_CATALOG has "evaluations.manage" so
     the roles-admin UI renders a checkbox for it.
"""
import os
import sys
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


@check("1. plan_gating.action_module maps evaluations.manage → 'evaluations'")
def _():
    from app.services.plan_gating import action_module
    assert action_module("evaluations.manage") == "evaluations"
    assert action_module("evaluations.view") == "evaluations"
    return "prefix mapping registered"


@check("2. plan_allows(evaluations.manage, Pro) is True")
def _():
    from app.services.plan_gating import plan_allows
    class _Plan:
        def __init__(self, mods): self.modules = mods
    class _Co:
        def __init__(self, mods): self.subscription_plan = _Plan(mods)
    pro = _Co(["accounting", "sales", "evaluations", "hr"])
    assert plan_allows("evaluations.manage", pro) is True
    return "Pro passes the gate"


@check("3. plan_allows(evaluations.manage, Starter) is False")
def _():
    from app.services.plan_gating import plan_allows
    class _Plan:
        def __init__(self, mods): self.modules = mods
    class _Co:
        def __init__(self, mods): self.subscription_plan = _Plan(mods)
    starter = _Co(["accounting", "sales", "purchases", "reports"])
    assert plan_allows("evaluations.manage", starter) is False
    return "Starter blocked"


@check("4. plan_allows(evaluations.manage, Growth) is False")
def _():
    from app.services.plan_gating import plan_allows
    class _Plan:
        def __init__(self, mods): self.modules = mods
    class _Co:
        def __init__(self, mods): self.subscription_plan = _Plan(mods)
    growth = _Co(["accounting", "sales", "crm", "hr", "inventory", "pos"])
    assert plan_allows("evaluations.manage", growth) is False
    return "Growth blocked"


@check("5. permissions.P['evaluations.manage'] scoped to {owner, admin, hr_manager}")
def _():
    from app.services.permissions import P
    roles = P.get("evaluations.manage")
    assert roles is not None, "evaluations.manage missing from P dict"
    expected = {"owner", "admin", "hr_manager"}
    assert roles == expected, (
        f"unexpected role set for evaluations.manage: {roles}")
    return f"scoped correctly to {sorted(expected)}"


@check("6. All /evaluations routes use evaluations.manage — none on users.manage")
def _():
    routes_file = ROOT / "app" / "routes" / "evaluations.py"
    src = routes_file.read_text()
    assert "@require_permission(\"users.manage\")" not in src, \
        "still has stale users.manage decorators"
    count = src.count("@require_permission(\"evaluations.manage\")")
    assert count >= 17, \
        f"expected ≥17 evaluations.manage decorators, got {count}"
    return f"{count} routes gated on evaluations.manage"


@check("7. cli.PLAN_SEED — Pro includes 'evaluations'; Starter/Growth don't")
def _():
    from app.cli import PLAN_SEED
    by_code = {p["code"]: p for p in PLAN_SEED}
    assert "evaluations" in by_code["pro"]["modules"], \
        "Pro must include evaluations"
    assert "evaluations" not in by_code["starter"]["modules"], \
        "Starter must NOT include evaluations"
    assert "evaluations" not in by_code["growth"]["modules"], \
        "Growth must NOT include evaluations"
    return "PLAN_SEED correctly restricts to Pro"


@check("8. roles_seed.PERMISSION_CATALOG has evaluations.manage entry")
def _():
    from app.services.roles_seed import PERMISSION_CATALOG
    assert "evaluations.manage" in PERMISSION_CATALOG, \
        "evaluations.manage missing from PERMISSION_CATALOG"
    group, label, kind = PERMISSION_CATALOG["evaluations.manage"]
    assert group == "الموارد البشرية"
    assert "تقييم" in label
    return f"catalog entry: [{group}] {label}"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        for label, fn in CHECKS:
            try:
                res = fn()
                print(f"PASS  {label}  ⇒ {res}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                failed += 1
                import traceback; traceback.print_exc()
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
