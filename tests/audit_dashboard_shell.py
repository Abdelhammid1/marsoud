#!/usr/bin/env python3
"""MARSOUD-DASH-SHELL (2026-08-04) — dashboard + app-shell cleanup.

Six items off the dashboard ticket:
  1. The المحاسب الذكي top-bar button is replaced by a user menu
     (حسابي / تقاريري / تسجيل الخروج).
  2. المحاسب الذكي moves into the sidebar.
  3. «+ شركة جديدة» and the email/logout footer leave the sidebar.
  4. The period filter pills stop being glued together.
  5. The ✍️ قيد يدوي quick action goes (9 tiles in an 8-column grid).
  6. Pages that exist as routes but were missing from the sidebar are
     added — التصنيفات والفئات and four inventory screens.

Checks:
  1. The user menu renders with all three entries; logout is a POST.
  2. The old agent button and slide-over panel are gone.
  3. المحاسب الذكي is in the sidebar and reaches a working page.
  4. The sidebar footer is gone — no «+ شركة جديدة», no logout link.
  5. The period pills are styled by a selector that matches the markup.
  6. Quick actions: no قيد يدوي, and the row fits the 8-column grid.
  7. The five previously-unreachable pages are in the sidebar.
  8. Every new sidebar row has a permission_map entry.
  9. Every new sidebar row actually resolves and returns 200.
 10. The sidebar gate goes through endpoint_to_subitem, so the new rows
     are not silently hidden for plan-gated tenants.
"""
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
COMPANY_NAME = "__DASH_SHELL__"
EMAIL = "__dashshell@audit.local"
_STATE = {}

NEW_NAV = [
    ("products.hierarchy", "التصنيفات والفئات", "products.manage"),
    ("inventory.adjust", "تسوية جرد", "inventory.manage"),
    ("inventory.opening_balance", "رصيد افتتاحي مخزون", "inventory.manage"),
    ("inventory.inventory_balance", "رصيد المخزون", "inventory.view"),
    ("inventory.barcodes_picker", "طباعة الباركود", "inventory.manage"),
]


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import Company, User, Plan
    from app.services.seed_coa import seed_default_coa
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.roles import set_membership_role
    from app.services.legal import get_terms_version
    from app.services.plan_gating import plan_allows

    _teardown()
    co = Company(name=COMPANY_NAME, base_currency="EGP", vat_rate=0)
    db.session.add(co)
    db.session.flush()
    # The plan matters more than usual here. We deliberately pick one
    # with a NON-NULL allowed_subitems that includes products.index and
    # inventory.index, and leave the subscription window unset — that is
    # the STRICT gating path (no trial bypass), which is exactly the
    # configuration under which the new sidebar rows were invisible
    # before the endpoint_to_subitem fix. A plan with subitems=NULL, or a
    # company inside its trial, would let everything through and the
    # suite would pass without testing anything.
    chosen = None
    for pl in Plan.query.order_by(Plan.id).all():
        co.plan_id = pl.id
        co.intended_plan_id = pl.id
        db.session.flush()
        si = pl.subitems
        if (si is not None
                and "products.index" in si and "inventory.index" in si
                and plan_allows("inventory.view", co)
                and plan_allows("journals.create", co)):
            chosen = pl
            break
    assert chosen is not None, (
        "no plan has a non-NULL allowed_subitems covering products + "
        "inventory — this suite needs one to test the gating path")
    db.session.commit()
    _STATE["plan"] = chosen.name
    seed_default_coa(co.id)
    ensure_roles_ready_for_company(co.id)

    u = User(email=EMAIL, full_name="Dash Shell Owner", is_active=True,
             terms_version=get_terms_version(),
             terms_accepted_at=datetime.utcnow())
    u.set_password("Passw0rd!audit1")
    db.session.add(u)
    db.session.flush()
    db.session.commit()
    set_membership_role(u.id, co.id, "owner")
    _STATE["cid"] = co.id
    _STATE["uid"] = u.id


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    for co in Company.query.filter_by(name=COMPANY_NAME).all():
        cid = co.id
        db.session.execute(text(
            "DELETE FROM journal_lines WHERE entry_id IN "
            "(SELECT id FROM journal_entries WHERE company_id=:c)"), {"c": cid})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(
                        text(f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        try:
            db.session.execute(text(
                "DELETE FROM role_permissions WHERE role_id IN "
                "(SELECT id FROM roles WHERE company_id=:c)"), {"c": cid})
            db.session.execute(text("DELETE FROM roles WHERE company_id=:c"),
                               {"c": cid})
        except Exception:
            db.session.rollback()
        db.session.execute(text("DELETE FROM companies WHERE id=:c"),
                           {"c": cid})
        db.session.commit()
    u = User.query.filter_by(email=EMAIL).first()
    if u:
        db.session.delete(u)
        db.session.commit()


def _client():
    c = _STATE["app"].test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(_STATE["uid"])
        s["_fresh"] = True
        s["active_company_id"] = _STATE["cid"]
    return c


def _home():
    r = _client().get("/home")
    assert r.status_code == 200, f"/home returned {r.status_code}"
    return r.get_data(as_text=True)


def _base_src():
    """base.html with Jinja comments stripped.

    The comments recording each removal necessarily name the thing that
    was removed, so a raw grep for "شركة جديدة" or "show_agent" matches
    the note explaining their absence.
    """
    raw = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    return re.sub(r"\{#[\s\S]*?#\}", "", raw)


def _dash_src():
    return (ROOT / "app/templates/dashboard/index.html").read_text(
        encoding="utf-8")


# ─── 1-2. the user menu replaces the agent button ───────────────────────
@check("1. the user menu renders with حسابي / تقاريري / تسجيل الخروج")
def _():
    html = _home()
    assert 'id="user-menu"' in html, "the user menu is not rendered"
    assert "تسجيل الخروج" in html, "logout is missing from the menu"
    # The email belongs in the menu now that the sidebar footer is gone.
    assert EMAIL in html, "the user's email is not shown anywhere"
    # Logout must be a POST form, not a GET link — the sidebar used to
    # use a bare <a>, which is CSRF-prone and pre-fetchable.
    assert re.search(
        r'<form method="POST" action="[^"]*logout[^"]*"', html), \
        "logout is not a POST form"
    assert not re.search(r'<a href="[^"]*/logout"', html), \
        "a GET logout link is still present"
    return "menu present, logout is a POST"


@check("2. the agent button and its slide-over panel are gone")
def _():
    html = _home()
    src = _base_src()
    assert 'id="agent-panel"' not in html, "the slide-over panel still renders"
    assert "toggleAgent" not in html, "toggleAgent() is still wired up"
    assert "agent-panel" not in src, "the panel markup is still in base.html"
    assert "toggleAgent" not in src, "the toggle function is still defined"
    assert "sendAgent" not in src, "the panel's fetch JS is still in base.html"
    # And show_agent, which existed only to gate them.
    assert "show_agent" not in src, "the now-unused show_agent flag remains"
    return "button, panel and ~45 lines of dead JS removed"


@check("3. المحاسب الذكي is in the sidebar and its page works")
def _():
    html = _home()
    assert "المحاسب الذكي" in html, "المحاسب الذكي is not in the shell"
    assert "/agent/" in html, "no link to the agent page"
    r = _client().get("/agent/")
    assert r.status_code == 200, f"/agent/ returned {r.status_code}"
    return "sidebar row → /agent/ 200"


# ─── 3-4. sidebar footer ────────────────────────────────────────────────
@check("4. the sidebar footer is gone (no «+ شركة جديدة», no logout link)")
def _():
    html = _home()
    assert "+ شركة جديدة" not in html, "«+ شركة جديدة» is still rendered"
    src = _base_src()
    assert "شركة جديدة" not in src, "the new-company link is still in base.html"
    # The old footer's invisible-text bug must not survive either.
    assert 'class="text-white font-semibold leading-tight"' not in src, \
        "the white-on-near-white name block is still there"
    return "footer removed"


# ─── 5. the period pills ────────────────────────────────────────────────
@check("5. the period pills are styled by a selector that matches the markup")
def _():
    src = _dash_src()
    # The markup renders <a>, so the rules must target <a>.
    assert re.search(r"#dash-root \.period a[,{]", src), \
        "no rule targets `.period a` — the pills get no padding or radius"
    assert re.search(r"#dash-root \.period a\.on[,{]", src), \
        "no rule targets `.period a.on` — the active pill never paints"
    # And the markup itself is still <a> (if it ever becomes <button>,
    # this check should be revisited rather than silently passing).
    assert re.search(r'<div class="period"[\s\S]{0,400}?<a href=', src), \
        "the pill markup is no longer <a> — re-check the selectors"
    html = _home()
    assert 'class="on"' in html or "on\"" in html, \
        "no pill carries the active class"
    return "`.period a` and `.period a.on` both styled"


# ─── 6. quick actions ───────────────────────────────────────────────────
@check("6. quick actions: no قيد يدوي, and they fit the 8-column grid")
def _():
    src = _dash_src()
    quick = src.split('<div class="quick">', 1)[1].split("</div>", 1)[0] \
        if '<div class="quick">' in src else ""
    assert quick, "could not locate the quick-actions block"
    # Strip Jinja comments first — the comment recording the removal
    # names the tile, and would match a naive search.
    quick_code = re.sub(r"\{#[\s\S]*?#\}", "", quick)
    assert "قيد يدوي" not in quick_code, "the قيد يدوي tile is still there"
    assert "journals.new" not in quick_code, \
        "the manual-journal tile is still there"
    tiles = re.findall(r'<a class="q"', quick_code)
    cols = re.search(r"#dash-root \.quick\{[^}]*repeat\((\d+),", src)
    assert cols, "could not read the grid column count"
    n_cols = int(cols.group(1))
    assert len(tiles) <= n_cols, (
        f"{len(tiles)} tiles in a {n_cols}-column grid — one wraps onto a "
        "row of its own, which is what the ticket reported")
    return f"{len(tiles)} tiles, {n_cols}-column grid"


# ─── 7-9. the missing sidebar pages ─────────────────────────────────────
@check("7. the five previously-unreachable pages are in the sidebar")
def _():
    html = _home()
    missing = [lbl for _ep, lbl, _p in NEW_NAV if lbl not in html]
    assert not missing, f"still absent from the sidebar: {missing}"
    return " · ".join(lbl for _e, lbl, _p in NEW_NAV)


@check("8. every new sidebar row has a permission_map entry")
def _():
    """A row with no entry leaves `req` as None, so the link shows to
    every logged-in user — including roles the route then 403s."""
    src = _base_src()
    pmap = src.split("{% set permission_map = {", 1)[1].split("} %}", 1)[0]
    for ep, _lbl, perm in NEW_NAV:
        assert f"'{ep}'" in pmap, f"{ep} has no permission_map entry"
        assert re.search(rf"'{re.escape(ep)}'\s*:\s*'{re.escape(perm)}'", pmap), \
            f"{ep} should map to {perm}"
    return f"{len(NEW_NAV)} rows mapped"


@check("9. every new sidebar row resolves and returns 200")
def _():
    from flask import url_for
    c = _client()
    out = []
    with _STATE["app"].test_request_context():
        urls = {ep: url_for(ep) for ep, _l, _p in NEW_NAV}
    for ep, url in urls.items():
        r = c.get(url)
        assert r.status_code == 200, f"{ep} ({url}) returned {r.status_code}"
        out.append(ep.split(".")[-1])
    return " · ".join(out)


@check("10. the sidebar gate goes through endpoint_to_subitem")
def _():
    """Otherwise every endpoint absent from SUB_ITEM_CATALOG — which is
    all five new rows — is silently hidden for tenants past their
    subscription window with a non-NULL allowed_subitems, even though the
    page loads fine for them."""
    import inspect as _inspect
    import app as app_pkg
    src = _inspect.getsource(app_pkg.create_app)
    assert "endpoint_to_subitem(endpoint)" in src, (
        "the template's subitem gate still passes the RAW endpoint, so "
        "the new sidebar rows will not render for plan-gated tenants")

    # And it behaves: the new endpoints must map onto a catalog sub-item
    # their plan already carries, rather than onto nothing.
    from app.services.plan_gating import (
        endpoint_to_subitem, ALL_SUB_ITEM_ENDPOINTS, SUB_ITEM_CATALOG,
    )
    mapped = {}
    for ep, _l, _p in NEW_NAV:
        assert ep not in ALL_SUB_ITEM_ENDPOINTS, (
            f"{ep} was added to SUB_ITEM_CATALOG — that 403s every "
            "existing tenant until a super-admin re-saves each plan")
        si = endpoint_to_subitem(ep)
        assert si, f"{ep} maps to no sub-item at all"
        assert si in ALL_SUB_ITEM_ENDPOINTS, \
            f"{ep} maps to {si}, which is not in the catalog"
        mapped[ep.split(".")[-1]] = si

    # The proof it matters: this fixture runs on a plan with a non-NULL
    # allowed_subitems and NO active subscription window, i.e. strict
    # gating. Under the old raw-endpoint gate every row below would be
    # hidden, because none of them is in the catalog.
    from app.models import Company, Plan
    from app.services.plan_gating import subitem_allowed
    co = db.session.get(Company, _STATE["cid"])
    plan = db.session.get(Plan, co.plan_id)
    assert plan is not None and plan.subitems is not None, \
        "fixture plan must have a non-NULL allowed_subitems"
    for ep, lbl, _p in NEW_NAV:
        assert not subitem_allowed(ep, co), (
            f"{ep} passes the RAW gate, so this check proves nothing — "
            "pick a more restrictive fixture plan")
        assert subitem_allowed(endpoint_to_subitem(ep), co), \
            f"{ep} is blocked even through its sub-item {endpoint_to_subitem(ep)}"
    return (f"plan={_STATE['plan']}, strict gating; "
            + "; ".join(f"{k}→{v}" for k, v in mapped.items()))


@check("11. no Jinja comment leaks into the rendered shell")
def _():
    """Caught in the browser, not by this suite: a comment written as

        {# ... no {# #} comments in here ... #}

    ends at the FIRST `#}`, because Jinja comments do not nest. The rest
    spilled into the sidebar as visible text while the page still
    returned 200, so every existing check passed. A leaked comment always
    leaves its closing delimiter behind, which is a cheap thing to
    assert."""
    html = _home()
    assert "#}" not in html, (
        "a Jinja comment leaked into the page — text after a stray '#}' "
        "is rendering to the user")
    assert "{#" not in html, "an unopened Jinja comment is rendering"
    # The tag we prefix internal notes with must not reach visible text.
    # <style>, <script> and <!-- --> are excluded: CSS, JS and HTML
    # comments legitimately carry these tags and are never displayed.
    body = re.sub(r"<(style|script)\b[\s\S]*?</\1>", "", html)
    body = re.sub(r"<!--[\s\S]*?-->", "", body)
    assert "MARSOUD-" not in body, \
        "an internal MARSOUD-* note is visible in the rendered page"
    return "no comment fragments in the rendered shell"


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
            except Exception as e:  # noqa: BLE001
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
