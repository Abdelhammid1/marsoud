#!/usr/bin/env python3
"""MARSOUD-DASHBOARD-COVERAGE-01 — every top-level route has a tile.

The dashboard is the daily surface owners open first; the sidebar is
discovery. When a feature ships with only a sidebar row and no
dashboard tile, adoption suffers even when the code is perfect
(this is exactly why cash-custody / item-custody tiles were
retrofitted as separate branches). This audit locks that in: any
future ticket that adds a sidebar row without also adding a
dashboard tile fails here.

Checks:
  1. every top-level user-facing endpoint in SUB_ITEM_CATALOG is
     referenced by an <a class="op"> or <a class="q"> in
     dashboard/index.html
  2. dashboard_metrics(company_id) returns the 4 new ops keys
     (guards against a helper being dropped from the ** unpack)
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
COMPANY_NAME = "__DASHBOARD_TILES_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Endpoints deliberately excluded from the "must have a tile" set ──
# Any endpoint added here is a sub-page of a top-level feature, not a
# separate feature. Keep the rationale next to each entry so future
# reviewers understand why.
EXEMPT = {
    # Sub-pages that live inside a tiled feature — users navigate
    # into these from the parent, not from the dashboard.
    "hr.attendance",           # sub-page of hr.index
    "inventory.warehouses",    # sub-page of inventory.index
    "manufacturing.work_orders_index",  # sub-page of manufacturing.bom_new
    "manufacturing.reports",   # under manufacturing
    "pos.shifts",              # sub-page of pos.index
    "pos.history",             # sub-page of pos.index
    "reports.employees_index", # under /reports/*, covered by reports.index
    "refunds.report",          # under refunds
    "inventory.movements",     # sub-page of inventory.index
    "inventory.transfers",     # sub-page of inventory.index
    "leads.no_response",       # sub-page of leads.index (already tiled)
    "leads.index",             # already tiled as an ops metric
    # Admin/settings pages — the audit intentionally does not require
    # dashboard tiles for these.
    "settings_roles.index",
    "settings_api_tokens.index",
    "settings_activity.index",
    "settings_backup.index",
    "payment_methods.index",
    "companies.edit",
    "audit_log.index",
    # Deliberately-omitted sub-workflow endpoints per the ticket:
    # activities/contacts are opened FROM a lead detail page.
    "crm.activities_index",
    "crm.contacts_index",
    "crm.analytics",           # report-shaped, covered by reports.index tile
    # forecast.index is already deep-linked from the "فواتير جايّة
    # عليك" section-4 panel footer (a "الجدول الكامل →" link). A
    # separate tile in section 2 would duplicate that entry.
    "forecast.index",
    # Very low priority personal storage — sidebar entry is fine.
    "user_files.index",
    # Dashboard itself — not a route that needs a tile TO itself.
    "dashboard.index",
}


def _tiled_endpoints():
    """Read dashboard/index.html and return the set of endpoints
    referenced by url_for(...) inside either an <a class="op"> or
    an <a class="q"> tile. Matches only the tiles, not the KPI
    cards or the panel rows."""
    tmpl = (ROOT / "app" / "templates" / "dashboard" / "index.html")\
        .read_text(encoding="utf-8")
    tiled = set()
    # Split into logical tile blocks and pull the url_for target.
    tile_re = re.compile(
        r"""<a\s+class="(?:op|q)"[^>]*href="\{\{\s*url_for\(\s*['"]([^'"]+)['"]""",
        re.DOTALL)
    for m in tile_re.finditer(tmpl):
        tiled.add(m.group(1))
    return tiled


def _expected_endpoints():
    """Endpoints that MUST have a tile — SUB_ITEM_CATALOG minus the
    EXEMPT set. Uses the same source of truth the sidebar uses."""
    from app.services.plan_gating import SUB_ITEM_CATALOG
    out = set()
    for _section, entries in SUB_ITEM_CATALOG.items():
        for endpoint, _label, _icon in entries:
            if endpoint in EXEMPT:
                continue
            out.add(endpoint)
    return out


def _setup():
    from app.models import Company, User, Plan
    from app.services.subscription import activate_default_subscription
    from app.services.legal import get_terms_version
    from datetime import datetime, timedelta
    _teardown()
    co = Company(name=COMPANY_NAME, base_currency="EGP", vat_rate=0)
    db.session.add(co)
    db.session.flush()
    activate_default_subscription(co, plan_code=None)
    # Put the fixture on Pro so all ops helpers can query cleanly.
    pro = Plan.query.filter_by(code="pro").first()
    if pro:
        co.plan_id = pro.id
    co.subscription_expires_at = datetime.utcnow() + timedelta(days=7)
    db.session.commit()
    _STATE["co_id"] = co.id


def _teardown():
    from app.models import Company
    for co in Company.query.filter_by(name=COMPANY_NAME).all():
        db.session.delete(co)
    db.session.commit()


@check("Every top-level route in SUB_ITEM_CATALOG has a dashboard tile")
def _coverage():
    """A sidebar 'X.index' is considered covered if the dashboard has
    ANY tile with an endpoint under the same blueprint prefix ('X.').
    This mirrors how users think: clicking «قيد يدوي» (journals.new)
    IS opening the journals area — the sidebar entry doesn't need its
    own separate tile. It also mirrors endpoint_to_subitem's rollup
    in app/services/plan_gating.py."""
    expected = _expected_endpoints()
    tiled = _tiled_endpoints()
    tiled_prefixes = {ep.rsplit(".", 1)[0] for ep in tiled}
    missing = []
    for ep in sorted(expected):
        bp = ep.rsplit(".", 1)[0]
        if bp in tiled_prefixes:
            continue
        # Also allow the exact endpoint match (endpoints without a dot
        # or where the tile lands on the sidebar endpoint itself).
        if ep in tiled:
            continue
        missing.append(ep)
    assert not missing, (
        f"{len(missing)} top-level route(s) have no dashboard tile:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd a tile to app/templates/dashboard/index.html "
        + "(any endpoint under the same blueprint counts), "
        + "or add the endpoint to the EXEMPT set in this file "
        + "with a comment explaining why.")
    return f"{len(expected)} expected endpoints covered by blueprint match"


@check("dashboard_metrics() carries the 4 new ops keys")
def _metrics_present():
    from app.services.reports import dashboard_metrics
    m = dashboard_metrics(_STATE["co_id"], period="month")
    ops = m.get("ops", {})
    required = [
        "hr_employees_active", "hr_expiring_contracts",
        "vendors_count", "vendors_with_balance",
        "products_count", "products_missing_price",
        "projects_open", "projects_overdue",
    ]
    missing = [k for k in required if k not in ops]
    assert not missing, (
        f"dashboard_metrics ops is missing keys: {missing}. "
        f"Check the ** unpacks in reports.py::dashboard_metrics.")
    # Values should be non-negative integers (helpers return 0 on
    # exception, so this is a light sanity check).
    for k in required:
        v = ops[k]
        assert isinstance(v, int) and v >= 0, (
            f"ops[{k!r}] should be a non-negative int; got {v!r}")
    return f"all {len(required)} keys present with sane values"


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
