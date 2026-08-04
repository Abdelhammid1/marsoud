#!/usr/bin/env python3
"""MARSOUD-CATEGORY-VISIBILITY-01 (2026-08-04, pt 2 2026-08-05).

Raw materials bought for manufacturing were showing up on the POS cashier
screen. Visibility is decided per module — POS, manufacturing, vendor
bills, customer invoices.

pt 2, from review: the decision lives on the GROUP and every category
under it INHERITS, unless that category overrides — per module, not
all-or-nothing. The category columns are a tri-state where NULL means
"inherit"; resolution is COALESCE(category, group).

The load-bearing checks are 3 (the barcode hole the ticket names by hand —
hiding a category from the grid is worthless if the product is still
scannable), 2 (all switches on => byte-identical behaviour, which is how
"nothing disappears on deploy" is guaranteed rather than hoped for), and
13-15 (the inheritance contract).

Checks, mapped to the ticket's acceptance criteria:
  1.  Group columns NOT NULL, category columns nullable, nothing hidden (AC4)
  2.  All switches on => every entry point returns what it did before  (AC4)
  3.  Hide POS => gone from the grid, the tab, AND the barcode/SKU scan (AC1)
  4.  ...while still visible in manufacturing and vendor bills          (AC2)
  5.  A switch takes effect immediately, no cache                       (AC3)
  6.  The products catalog still lists everything                       (AC5)
  7.  A product with no category stays visible in all four
  8.  Hiding in one company does not affect another
  9.  The category screen renders every control
 10.  A new category starts out inheriting
 11.  Edit saves the tri-state, including back to inherit
 12.  Every module in the registry is actually wired to a call site
 13.  A category with no opinion inherits its group          (pt 2)
 14.  An override beats the group, in both directions        (pt 2)
 15.  Override is per module, not per category               (pt 2)
 16.  The auto-save endpoint persists and reports resolution (pt 2)
 17.  An inheriting control shows the RESOLVED value         (pt 2)
 18.  The auto-save endpoint refuses a role without products.manage
 19.  The group form still saves without JS
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
COMPANY_NAME = "__CAT_VIS_AUDIT__"
OTHER_NAME = "__CAT_VIS_OTHER__"
EMAIL = "cat-vis-audit@x.test"
VIEWER_EMAIL = "cat-vis-viewer@x.test"
_STATE = {}

ALL_FLAGS = ("visible_in_pos", "visible_in_manufacturing",
             "visible_in_vendor_bills", "visible_in_customer_invoices")


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _neutralise_session_cookie_domain(app):
    """A domain-scoped session cookie is never sent to the test client.

    Copied from tests/audit_portal_403.py (MARSOUD-SESSION-COOKIE-DEV-FIX).
    A production-style .env sets SESSION_COOKIE_DOMAIN=.marsoud.com, which
    scopes the cookie to that domain while the test client runs on
    localhost — so it is never sent back, every request answers as
    anonymous, and the run reports 302s that look like real failures.
    """
    domain = app.config.get("SESSION_COOKIE_DOMAIN")
    if domain:
        app.config["SESSION_COOKIE_DOMAIN"] = None
        print(f"NOTE  SESSION_COOKIE_DOMAIN={domain!r} overridden to None "
              f"for this run -- a domain-scoped cookie is never sent "
              f"to the localhost test client.")


# ─── Fixture ────────────────────────────────────────────────────────────
def _mk_product(cid, name, sku, category_id, barcode=None):
    """A tracked product with an active default variant — the shape POS,
    bills and BOMs all require."""
    from app.models import Product, ProductVariant
    from decimal import Decimal
    p = Product(company_id=cid, name=name, is_tracked=True, is_active=True,
                sku=sku, category_id=category_id,
                default_price=Decimal("10"))
    db.session.add(p); db.session.flush()
    v = ProductVariant(company_id=cid, product_id=p.id, sku=sku,
                       barcode=barcode, is_active=True,
                       unit_cost=Decimal("5"))
    db.session.add(v); db.session.flush()
    from app.services.units import ensure_base_unit
    ensure_base_unit(p)
    db.session.flush()
    return p, v


def _setup():
    from app.models import (
        Company, Plan, User, UserStatus, Warehouse, Vendor,
        ProductGroup, ProductCategory, user_companies,
    )
    from app.services.seed_coa import seed_default_coa
    from app.services.subsidiary import ensure_vendor_account
    from werkzeug.security import generate_password_hash

    for nm in (COMPANY_NAME, OTHER_NAME):
        ex = Company.query.filter_by(name=nm).first()
        if ex:
            _teardown_company(ex.id)

    plan = Plan.query.filter_by(is_active=True).first()
    c = Company(name=COMPANY_NAME, base_currency="SAR",
                intended_plan_id=plan.id if plan else None)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    wh = Warehouse(company_id=c.id, name="المخزن الرئيسي", code="MAIN")
    db.session.add(wh); db.session.flush()
    vendor = Vendor(company_id=c.id, name="مورد الاختبار")
    db.session.add(vendor); db.session.flush()
    ensure_vendor_account(vendor)

    grp = ProductGroup(company_id=c.id, name="مجموعة الاختبار")
    db.session.add(grp); db.session.flush()
    raw = ProductCategory(company_id=c.id, group_id=grp.id, name="مواد خام")
    sell = ProductCategory(company_id=c.id, group_id=grp.id, name="بضاعة")
    db.session.add_all([raw, sell]); db.session.flush()

    _, raw_v = _mk_product(c.id, "قماش قطن", "RAW-1", raw.id,
                           barcode="BC-RAW-1")
    _, sell_v = _mk_product(c.id, "قميص جاهز", "SELL-1", sell.id,
                            barcode="BC-SELL-1")
    # A product from before categories were mandatory.
    _, orphan_v = _mk_product(c.id, "صنف قديم", "NOCAT-1", None,
                              barcode="BC-NOCAT-1")

    try:
        from app.services.legal import get_terms_version
        terms_version = get_terms_version()
    except Exception:  # noqa: BLE001
        terms_version = None
    u = User(email=EMAIL,
             password_hash=generate_password_hash("x", method="pbkdf2:sha256"),
             full_name="CatVis Owner", is_active=True,
             status=UserStatus.ACTIVE.value, terms_version=terms_version)
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))

    # A second tenant, to prove the switch is company-scoped.
    o = Company(name=OTHER_NAME, base_currency="SAR",
                intended_plan_id=plan.id if plan else None)
    db.session.add(o); db.session.flush()
    seed_default_coa(o.id)
    ogrp = ProductGroup(company_id=o.id, name="مجموعة")
    db.session.add(ogrp); db.session.flush()
    ocat = ProductCategory(company_id=o.id, group_id=ogrp.id, name="مواد خام")
    db.session.add(ocat); db.session.flush()
    _mk_product(o.id, "قماش شركة تانية", "OTH-1", ocat.id)
    # The same owner in both, so the isolation check can actually switch
    # companies over HTTP — a non-member gets bounced before the route
    # runs, which would make check 8 pass for the wrong reason.
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=o.id, role="owner"))

    db.session.commit()
    _STATE.update(company_id=c.id, other_company_id=o.id,
                  warehouse_id=wh.id, vendor_id=vendor.id, user_id=u.id,
                  group_id=grp.id, other_group_id=ogrp.id,
                  raw_cat_id=raw.id, sell_cat_id=sell.id,
                  other_cat_id=ocat.id,
                  raw_variant_id=raw_v.id, sell_variant_id=sell_v.id,
                  orphan_variant_id=orphan_v.id)


def _teardown_company(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        je_ids = [r[0] for r in conn.execute(text(
            "SELECT id FROM journal_entries WHERE company_id = :c"),
            {"c": company_id}).fetchall()]
        if je_ids:
            _in = ",".join(str(i) for i in je_ids)
            conn.execute(text(
                f"DELETE FROM journal_lines WHERE entry_id IN ({_in})"))
        prod_ids = [r[0] for r in conn.execute(text(
            "SELECT id FROM products WHERE company_id = :c"),
            {"c": company_id}).fetchall()]
        if prod_ids:
            _in = ",".join(str(i) for i in prod_ids)
            conn.execute(text(
                f"DELETE FROM product_units WHERE product_id IN ({_in})"))
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text("DELETE FROM users WHERE email IN (:e, :v)"),
                     {"e": EMAIL, "v": VIEWER_EMAIL})
        for t in ("stock_balances", "stock_movements", "stock_lots"):
            conn.execute(text(
                f"DELETE FROM {t} WHERE variant_id NOT IN "
                "(SELECT id FROM product_variants)"))


def _teardown():
    from app.models import Company
    for nm in (COMPANY_NAME, OTHER_NAME):
        ex = Company.query.filter_by(name=nm).first()
        if ex:
            _teardown_company(ex.id)


def _reset_g():
    """Clear the per-context caches between test clients.

    `_login_user` is the one that matters and the one that is easy to
    miss: Flask-Login caches the resolved user on `g`, and every check in
    this suite runs inside ONE app context, so without clearing it a
    second client answers as the FIRST user who logged in. That silently
    turns a permission check into a check of whoever ran before it —
    exactly how a "bug" was nearly reported here that did not exist.
    """
    from flask import g
    for k in ("active_company", "_active_company", "active_company_id",
              "_login_user"):
        if hasattr(g, k):
            try:
                delattr(g, k)
            except Exception:  # noqa: BLE001
                pass


def _client(company_id=None):
    from flask import current_app
    _reset_g()
    c = current_app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["user_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = company_id or _STATE["company_id"]
    return c


def _set_flags(cat_id, **flags):
    from app.models import ProductCategory
    c = db.session.get(ProductCategory, cat_id)
    for k, v in flags.items():
        setattr(c, k, v)
    db.session.commit()


def _reset_flags():
    """Back to the shipped default: groups visible, categories inheriting.

    pt 2 — a category's neutral state is NULL, not True. Resetting them to
    True would make every check run against four explicit overrides and
    quietly stop testing inheritance at all.
    """
    from app.models import ProductGroup
    for cid_ in (_STATE["raw_cat_id"], _STATE["sell_cat_id"],
                 _STATE["other_cat_id"]):
        _set_flags(cid_, **{f: None for f in ALL_FLAGS})
    for gid in (_STATE["group_id"], _STATE["other_group_id"]):
        grp = db.session.get(ProductGroup, gid)
        for f in ALL_FLAGS:
            setattr(grp, f, True)
    db.session.commit()


# ─── What each module actually returns ──────────────────────────────────
def _pos_grid_skus():
    """Every SKU the cashier grid would render.

    The grid block only renders when at least one category is visible for
    POS (`{% if categories %}` in register.html). With every category
    hidden there is legitimately no grid, which is an EMPTY set — not a
    parse failure. Conflating the two made a real check blow up with a
    TypeError instead of asserting.
    """
    r = _client().get("/pos/")
    assert r.status_code == 200, f"/pos/ returned {r.status_code}"
    body = r.get_data(as_text=True)
    m = re.search(r"var data = (\{.*?\});", body, re.S)
    if not m:
        return set(), body
    import json
    data = json.loads(m.group(1))
    return {p["sku"] for items in data.values() for p in items}, body


def _pos_lookup(q):
    r = _client().post("/pos/lookup", json={"query": q})
    return r.status_code, r.get_json()


def _invoice_picker_names():
    r = _client().get("/products/api/list")
    return {p["name"] for p in (r.get_json() or [])}


def _bill_picker_skus():
    r = _client().get("/vendor-bills/api/inventory-targets")
    return {v["sku"] for v in (r.get_json() or {}).get("variants", [])}


def _bom_picker_skus():
    body = _client().get("/manufacturing/boms/new").get_data(as_text=True)
    return set(re.findall(r'<option value="\d+">[^<]*\(([^)]+)\)</option>',
                          body))


# ─── 1-2. defaults ──────────────────────────────────────────────────────
@check("1. the switches live on the GROUP; the category is a tri-state")
def _():
    """pt 2 — the decision moved up. The group always holds a real answer
    (NOT NULL, it is where inheritance bottoms out); the category may hold
    NULL, which is the only value that can mean "inherit"."""
    from app.models import ProductCategory, ProductGroup
    gcols = ProductGroup.__table__.c
    ccols = ProductCategory.__table__.c
    for f in ALL_FLAGS:
        assert f in gcols, f"{f} missing from product_groups"
        assert not gcols[f].nullable, \
            f"group.{f} must be NOT NULL — inheritance has to bottom out"
        assert f in ccols, f"{f} missing from product_categories"
        assert ccols[f].nullable, \
            f"category.{f} must be NULLABLE — NULL is how 'inherit' is said"

    # The whole database, not just the fixture: after the migration nothing
    # may resolve to hidden. This is the original ticket's "nothing
    # disappeared after deploy" criterion, re-proved through the change.
    from sqlalchemy import or_
    groups_off = db.session.query(ProductGroup).filter(or_(*[
        getattr(ProductGroup, f).is_(False) for f in ALL_FLAGS])).count()
    cats_off = db.session.query(ProductCategory).filter(or_(*[
        getattr(ProductCategory, f).is_(False) for f in ALL_FLAGS])).count()
    assert groups_off == 0, f"{groups_off} groups already have a switch off"
    assert cats_off == 0, f"{cats_off} categories already override to hidden"
    n_g = db.session.query(ProductGroup).count()
    n_c = db.session.query(ProductCategory).count()
    return (f"group NOT NULL, category nullable; {n_g} groups + {n_c} "
            "categories all resolve visible")


@check("2. all switches on ⇒ every entry point returns what it did before")
def _():
    """The default state must not merely behave the same, it must run the
    same query. product_visible_clause returns None when nothing is
    hidden, so the guarded queries are untouched."""
    from app.services.category_visibility import (
        product_visible_clause, MODULES,
    )
    _reset_flags()
    cid = _STATE["company_id"]
    for module in MODULES:
        assert product_visible_clause(cid, module) is None, (
            f"{module}: a clause is applied even though nothing is "
            "hidden — the default path is not a no-op")
    grid, _ = _pos_grid_skus()
    assert {"RAW-1", "SELL-1"} <= grid, f"POS grid: {grid}"
    assert "قماش قطن" in _invoice_picker_names()
    assert {"RAW-1", "SELL-1"} <= _bill_picker_skus()
    assert {"RAW-1", "SELL-1"} <= _bom_picker_skus()
    return "no clause applied; all 4 pickers list both products"


# ─── 3-4. THE BUG ───────────────────────────────────────────────────────
@check("3. hide POS ⇒ gone from grid, tab, AND barcode/SKU scan")
def _():
    _reset_flags()
    _set_flags(_STATE["raw_cat_id"], visible_in_pos=False)

    grid, body = _pos_grid_skus()
    assert "RAW-1" not in grid, "raw material still on the cashier grid"
    assert "SELL-1" in grid, "the other category was hidden too"
    assert "مواد خام" not in body, \
        "the hidden category still has a tab — it would open empty"

    # The hole the ticket names: scanning must not find it either.
    for q in ("BC-RAW-1", "RAW-1"):
        status, payload = _pos_lookup(q)
        assert status == 404, (
            f"scanning {q!r} returned {status} — a product hidden from "
            "POS is still reachable by barcode/SKU")
        assert "غير معروف" in (payload or {}).get("error", ""), payload
    # ...and a visible product still scans fine.
    status, payload = _pos_lookup("BC-SELL-1")
    assert status == 200, f"visible product no longer scans: {status}"
    return "grid, tab, barcode and SKU all closed; visible product unaffected"


@check("4. the same category still works in manufacturing + vendor bills")
def _():
    _reset_flags()
    _set_flags(_STATE["raw_cat_id"], visible_in_pos=False,
               visible_in_customer_invoices=False)
    assert "RAW-1" in _bom_picker_skus(), \
        "raw material vanished from the BOM picker"
    assert "RAW-1" in _bill_picker_skus(), \
        "raw material vanished from the vendor bill picker"
    assert "قماش قطن" not in _invoice_picker_names(), \
        "raw material still offered on a customer invoice"
    return "closed for POS+invoices, open for manufacturing+bills"


@check("5. a switch takes effect immediately (no cache)")
def _():
    _reset_flags()
    assert "قماش قطن" in _invoice_picker_names(), "precondition failed"
    _set_flags(_STATE["raw_cat_id"], visible_in_customer_invoices=False)
    assert "قماش قطن" not in _invoice_picker_names(), \
        "still listed right after being hidden — something is cached"
    _set_flags(_STATE["raw_cat_id"], visible_in_customer_invoices=True)
    assert "قماش قطن" in _invoice_picker_names(), \
        "still hidden right after being re-shown — something is cached"
    return "hide and re-show both visible on the next request"


# ─── 5-8. scope ─────────────────────────────────────────────────────────
@check("6. the products catalog still lists everything")
def _():
    _reset_flags()
    _set_flags(_STATE["raw_cat_id"], **{f: False for f in ALL_FLAGS})
    body = _client().get("/products/").get_data(as_text=True)
    assert "قماش قطن" in body, (
        "the admin catalog hides a product — it must show every product "
        "regardless of the module switches")
    src = (ROOT / "app/routes/products.py").read_text(encoding="utf-8")
    idx = src.index("def index(")
    assert "product_visible_clause" not in src[idx:idx + 1500], \
        "the catalog route has grown a visibility filter"
    return "catalog unfiltered even with all four switches off"


@check("7. a product with no category stays visible in all four")
def _():
    _reset_flags()
    _set_flags(_STATE["raw_cat_id"], **{f: False for f in ALL_FLAGS})
    grid, _ = _pos_grid_skus()
    # The POS grid keys by category, so an uncategorised product has no
    # tab to live in — that predates this ticket. What matters is that it
    # is not newly EXCLUDED by the filter: the other three still list it,
    # and it still scans.
    assert "صنف قديم" in _invoice_picker_names(), "invoice picker dropped it"
    assert "NOCAT-1" in _bill_picker_skus(), "bill picker dropped it"
    assert "NOCAT-1" in _bom_picker_skus(), "BOM picker dropped it"
    status, _p = _pos_lookup("BC-NOCAT-1")
    assert status == 200, f"uncategorised product stopped scanning: {status}"
    return "uncategorised product untouched by the filter"


@check("8. hiding a category in one company does not affect another")
def _():
    _reset_flags()
    _set_flags(_STATE["raw_cat_id"], **{f: False for f in ALL_FLAGS})
    from app.services.category_visibility import hidden_category_ids
    mine = hidden_category_ids(_STATE["company_id"], "pos")
    theirs = hidden_category_ids(_STATE["other_company_id"], "pos")
    assert _STATE["raw_cat_id"] in mine
    assert theirs == set(), f"other tenant sees hidden categories: {theirs}"
    # Parse, don't substring-match: jsonify escapes non-ASCII, so the raw
    # body carries ق... and an Arabic `in body` check always fails.
    r = _client(_STATE["other_company_id"]).get("/products/api/list")
    assert r.status_code == 200, f"other company got {r.status_code}"
    names = {p["name"] for p in (r.get_json() or [])}
    assert "قماش شركة تانية" in names, \
        f"the other company's product disappeared: {names}"
    return "scoped per company"


# ─── 9-11. the screen ───────────────────────────────────────────────────
@check("9. the category screen renders all four checkboxes")
def _():
    from app.services.category_visibility import MODULES
    body = _client().get("/products/hierarchy").get_data(as_text=True)
    assert body.count('name="visible_in_pos"') >= 2, (
        "expected the POS checkbox on both the edit row and the "
        "add-category form")
    for col, label in MODULES.values():
        assert f'name="{col}"' in body, f"{col} checkbox missing"
        assert label in body, f"label {label!r} missing"
    return f"{len(MODULES)} checkboxes on edit + create"


@check("10. a new category starts out inheriting from its group")
def _():
    """pt 2 — the create form no longer offers the four switches: the group
    already carries the decision, so a new category has nothing to choose."""
    from app.models import ProductCategory
    _client().post("/products/hierarchy/categories", data={
        "group_id": str(_STATE["group_id"]), "name": "فئة جديدة للاختبار",
    })
    c = ProductCategory.query.filter_by(
        company_id=_STATE["company_id"], name="فئة جديدة للاختبار").first()
    assert c is not None, "category was not created"
    try:
        for f in ALL_FLAGS:
            assert getattr(c, f) is None, \
                f"{f} was set to {getattr(c, f)!r}; a new category inherits"
        from app.services.category_visibility import effective_flag
        assert effective_flag(c, "pos") == (True, True)
    finally:
        db.session.delete(c)
        db.session.commit()
    return "all four NULL — inheriting, and resolving through the group"


@check("11. editing saves the tri-state, including back to inherit")
def _():
    from app.models import ProductCategory
    _reset_flags()
    cat_id = _STATE["raw_cat_id"]
    # A <select> always submits, so unlike the old checkboxes every field
    # is present. Override two, leave two inheriting.
    _client().post(f"/products/hierarchy/categories/{cat_id}/edit", data={
        "name": "مواد خام",
        "visible_in_pos": "0",
        "visible_in_customer_invoices": "0",
        "visible_in_manufacturing": "inherit",
        "visible_in_vendor_bills": "inherit",
    })
    db.session.expire_all()
    c = db.session.get(ProductCategory, cat_id)
    assert c.visible_in_pos is False, "override-hide did not save"
    assert c.visible_in_customer_invoices is False
    assert c.visible_in_manufacturing is None, "'inherit' saved as a value"
    assert c.visible_in_vendor_bills is None
    assert c.name == "مواد خام", "the name was lost while saving switches"

    # Back to inherit — the case a two-state control could never express.
    _client().post(f"/products/hierarchy/categories/{cat_id}/edit", data={
        "name": "مواد خام",
        **{f: "inherit" for f in ALL_FLAGS},
    })
    db.session.expire_all()
    c = db.session.get(ProductCategory, cat_id)
    for f in ALL_FLAGS:
        assert getattr(c, f) is None, f"{f} could not be returned to inherit"
    _reset_flags()
    return "override and inherit both persist; name preserved"


def _set_group(module_col, value):
    from app.models import ProductGroup
    grp = db.session.get(ProductGroup, _STATE["group_id"])
    setattr(grp, module_col, value)
    db.session.commit()


@check("13. a category with no opinion INHERITS its group")
def _():
    _reset_flags()
    # Group off ⇒ both categories under it go dark, without touching them.
    _set_group("visible_in_customer_invoices", False)
    names = _invoice_picker_names()
    assert "قماش قطن" not in names, "inheriting category ignored its group"
    assert "قميص جاهز" not in names, "the sibling ignored it too"
    # ...and back on.
    _set_group("visible_in_customer_invoices", True)
    assert "قماش قطن" in _invoice_picker_names(), \
        "turning the group back on did not restore its categories"
    return "group flip moves every inheriting category with it"


@check("14. a category override beats its group, in both directions")
def _():
    _reset_flags()
    # Group OFF, one category overrides back ON.
    _set_group("visible_in_customer_invoices", False)
    _set_flags(_STATE["sell_cat_id"], visible_in_customer_invoices=True)
    names = _invoice_picker_names()
    assert "قميص جاهز" in names, "override-show lost to a group that is off"
    assert "قماش قطن" not in names, "the inheriting sibling leaked through"

    # Group ON, one category overrides OFF.
    _set_group("visible_in_customer_invoices", True)
    _set_flags(_STATE["sell_cat_id"], visible_in_customer_invoices=None)
    _set_flags(_STATE["raw_cat_id"], visible_in_customer_invoices=False)
    names = _invoice_picker_names()
    assert "قماش قطن" not in names, "override-hide lost to a group that is on"
    assert "قميص جاهز" in names, "the inheriting sibling was dragged down"
    return "override wins over the group both ways"


@check("15. override is PER MODULE — inherit one, override another")
def _():
    """The review asked for per-module override, not all-or-nothing: a
    category can follow its group for POS while overriding invoices."""
    _reset_flags()
    _set_group("visible_in_pos", False)
    _set_flags(_STATE["raw_cat_id"], visible_in_customer_invoices=False)
    from app.services.category_visibility import effective_flag
    from app.models import ProductCategory
    cat = db.session.get(ProductCategory, _STATE["raw_cat_id"])
    pos_val, pos_inh = effective_flag(cat, "pos")
    inv_val, inv_inh = effective_flag(cat, "customer_invoices")
    assert (pos_val, pos_inh) == (False, True), \
        f"POS should be inherited-and-off, got {(pos_val, pos_inh)}"
    assert (inv_val, inv_inh) == (False, False), \
        f"invoices should be an explicit override, got {(inv_val, inv_inh)}"
    # And the untouched modules still inherit ON.
    assert effective_flag(cat, "manufacturing") == (True, True)
    grid, _ = _pos_grid_skus()
    assert "RAW-1" not in grid, "POS should follow the group here"
    assert "RAW-1" in _bom_picker_skus(), "manufacturing was not touched"
    return "POS inherited-off, invoices overridden-off, manufacturing on"


@check("16. the auto-save endpoint persists, and is guarded")
def _():
    from app.models import ProductCategory, ProductGroup
    _reset_flags()
    c = _client()

    # category → override to hidden
    r = c.post("/products/hierarchy/visibility", json={
        "level": "category", "id": _STATE["raw_cat_id"],
        "module": "customer_invoices", "value": False})
    assert r.status_code == 200, f"category save returned {r.status_code}"
    body = r.get_json()
    assert body["value"] is False and body["inherited"] is False, body
    db.session.expire_all()
    assert db.session.get(
        ProductCategory, _STATE["raw_cat_id"]).visible_in_customer_invoices is False

    # category → back to inherit
    r = c.post("/products/hierarchy/visibility", json={
        "level": "category", "id": _STATE["raw_cat_id"],
        "module": "customer_invoices", "value": None})
    assert r.get_json()["inherited"] is True, r.get_json()
    db.session.expire_all()
    assert db.session.get(
        ProductCategory, _STATE["raw_cat_id"]).visible_in_customer_invoices is None

    # group → off, and the reply reports every category's new resolution
    r = c.post("/products/hierarchy/visibility", json={
        "level": "group", "id": _STATE["group_id"],
        "module": "pos", "value": False})
    body = r.get_json()
    assert body["group_value"] is False, body
    assert body["categories"][str(_STATE["raw_cat_id"])] is False, body
    db.session.expire_all()
    assert db.session.get(ProductGroup, _STATE["group_id"]).visible_in_pos is False

    # a bad module is refused, not silently ignored
    assert c.post("/products/hierarchy/visibility", json={
        "level": "category", "id": _STATE["raw_cat_id"],
        "module": "nope", "value": False}).status_code == 400
    # cross-tenant
    assert c.post("/products/hierarchy/visibility", json={
        "level": "category", "id": _STATE["other_cat_id"],
        "module": "pos", "value": False}).status_code == 404
    _reset_flags()
    return "saves both levels, reports resolution, refuses bad input"


@check("17. the screen shows the RESOLVED value on an inheriting control")
def _():
    _reset_flags()
    body = _client().get("/products/hierarchy").get_data(as_text=True)
    assert "وراثة (ظاهرة)" in body, "inheriting control does not show its value"
    assert body.count('data-vis-level="group"') == len(ALL_FLAGS), \
        "the group is missing its switches"
    _set_group("visible_in_pos", False)
    body = _client().get("/products/hierarchy").get_data(as_text=True)
    assert "وراثة (مخفية)" in body, \
        "an inheriting control still claims visible after the group went off"
    _reset_flags()
    return "«وراثة» reports the effective answer, both ways"


@check("18. the auto-save endpoint refuses a role without products.manage")
def _():
    """The endpoint writes settings that hide products from the cashier, so
    it needs the same gate as the screen it serves. Uses a viewer — a real
    role that can see the app but not manage products."""
    from app.models import User, UserStatus, ProductGroup
    from app.models.user import user_companies
    from app.services.legal import get_terms_version
    from werkzeug.security import generate_password_hash
    from datetime import datetime as _dt

    _reset_flags()
    v = User(email=VIEWER_EMAIL,
             password_hash=generate_password_hash("x", method="pbkdf2:sha256"),
             full_name="CatVis Viewer", is_active=True,
             status=UserStatus.ACTIVE.value,
             terms_version=get_terms_version(),
             terms_accepted_at=_dt.utcnow())
    db.session.add(v)
    db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=v.id, company_id=_STATE["company_id"], role="viewer"))
    db.session.commit()
    try:
        _reset_g()
        c = _STATE["app"].test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(v.id)
            s["_fresh"] = True
            s["active_company_id"] = _STATE["company_id"]
        r = c.post("/products/hierarchy/visibility", json={
            "level": "group", "id": _STATE["group_id"],
            "module": "pos", "value": False})
        assert r.status_code != 200, \
            "a viewer was allowed to change product visibility"
        db.session.expire_all()
        assert db.session.get(
            ProductGroup, _STATE["group_id"]).visible_in_pos is True, \
            "the refused request still wrote to the group"
    finally:
        db.session.execute(
            db.text("DELETE FROM user_companies WHERE user_id = :u"),
            {"u": v.id})
        db.session.execute(db.text("DELETE FROM users WHERE email = :e"),
                            {"e": VIEWER_EMAIL})
        db.session.commit()
        _reset_g()
    return "viewer refused, nothing written"


@check("19. the group form still saves without JS (checkbox presence)")
def _():
    """The auto-save endpoint is the normal path, but the form must keep
    working on its own — and an unticked checkbox is not submitted at all,
    so this is the case a `.get()` read could never turn off."""
    from app.models import ProductGroup
    _reset_flags()
    gid = _STATE["group_id"]
    r = _client().post(f"/products/hierarchy/groups/{gid}/edit", data={
        "name": "مجموعة الاختبار",
        "visible_in_manufacturing": "1", "visible_in_vendor_bills": "1",
    })
    assert r.status_code in (301, 302), f"form POST returned {r.status_code}"
    assert "hierarchy" in r.headers.get("Location", ""), \
        f"bounced to {r.headers.get('Location')!r} — the owner was refused"
    db.session.expire_all()
    grp = db.session.get(ProductGroup, gid)
    assert grp.visible_in_manufacturing is True
    assert grp.visible_in_vendor_bills is True
    assert grp.visible_in_pos is False, "unticked POS was not turned off"
    assert grp.visible_in_customer_invoices is False
    assert grp.name == "مجموعة الاختبار", "the name was lost"
    _reset_flags()
    return "ticked saved, unticked cleared, name preserved"


@check("12. every module in the registry is wired to a real call site")
def _():
    """A switch nobody reads is worse than no switch. Each module key must
    appear in the source of the route file that serves it."""
    from app.services.category_visibility import MODULES
    wired = {
        "pos": "app/routes/pos.py",
        "customer_invoices": "app/routes/products.py",
        "vendor_bills": "app/routes/vendor_bills.py",
        "manufacturing": "app/routes/manufacturing.py",
    }
    missing = []
    for module in MODULES:
        path = wired.get(module)
        if not path:
            missing.append(f"{module}: no call site recorded in this test")
            continue
        src = (ROOT / path).read_text(encoding="utf-8")
        if f'"{module}"' not in src:
            missing.append(f"{module}: not referenced in {path}")
    assert not missing, "; ".join(missing)
    return f"all {len(MODULES)} modules wired"


def main():
    app = create_app()
    _neutralise_session_cookie_domain(app)
    _STATE["app"] = app
    passed = failed = 0
    with app.app_context():
        from tests._orphan_sweep import preflight
        preflight()
        _setup()
        try:
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        ⇒ {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}\n        ⇒ {type(e).__name__}: {e}")
                    failed += 1
        finally:
            _teardown()
            print("\n(cleaned up fixture companies)")
    print(f"\n────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
