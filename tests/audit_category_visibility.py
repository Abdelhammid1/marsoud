#!/usr/bin/env python3
"""MARSOUD-CATEGORY-VISIBILITY-01 (2026-08-04).

Raw materials bought for manufacturing were showing up on the POS cashier
screen. Visibility is now per CATEGORY, with four independent switches:
POS, manufacturing, vendor bills, customer invoices.

The load-bearing checks are 3 (the barcode hole the ticket names by hand —
hiding a category from the grid is worthless if the product is still
scannable) and 2 (all switches on ⇒ byte-identical behaviour, which is how
"nothing disappears on deploy" is guaranteed rather than hoped for).

Checks, mapped to the ticket's acceptance criteria:
  1.  The four columns exist, default True, nothing is hidden today  (AC4)
  2.  All switches on ⇒ every entry point returns what it did before (AC4)
  3.  Hide POS ⇒ gone from the grid, the tab, AND the barcode/SKU scan (AC1)
  4.  ...while still visible in manufacturing and vendor bills          (AC2)
  5.  A switch takes effect immediately, no cache                       (AC3)
  6.  The products catalog still lists everything                       (AC5)
  7.  A product with no category stays visible in all four
  8.  Hiding in one company does not affect another
  9.  The category screen renders all four checkboxes
 10.  Create saves the switches
 11.  Edit saves them, INCLUDING unticking (the absent-field case)
 12.  Every module in the registry is actually wired to a call site
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
                  group_id=grp.id, raw_cat_id=raw.id, sell_cat_id=sell.id,
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
        conn.execute(text("DELETE FROM users WHERE email = :e"),
                     {"e": EMAIL})
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
    from flask import g
    for k in ("active_company", "_active_company", "active_company_id"):
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
    for cid_ in (_STATE["raw_cat_id"], _STATE["sell_cat_id"],
                 _STATE["other_cat_id"]):
        _set_flags(cid_, **{f: True for f in ALL_FLAGS})


# ─── What each module actually returns ──────────────────────────────────
def _pos_grid_skus():
    """Every SKU the cashier grid would render."""
    body = _client().get("/pos/").get_data(as_text=True)
    m = re.search(r"var data = (\{.*?\});", body, re.S)
    if not m:
        return None, body
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
@check("1. the four switches exist, default True, nothing hidden today")
def _():
    from app.models import ProductCategory
    cols = ProductCategory.__table__.c
    for f in ALL_FLAGS:
        assert f in cols, f"{f} missing from product_categories"
        assert not cols[f].nullable, f"{f} must be NOT NULL"
    # Every category in the WHOLE database — the real dev data, not just
    # the fixture — must be visible everywhere. This is the ticket's
    # "nothing disappeared after deploy" criterion.
    from sqlalchemy import text, or_
    hidden = db.session.query(ProductCategory).filter(or_(*[
        getattr(ProductCategory, f).is_(False) for f in ALL_FLAGS
    ])).count()
    assert hidden == 0, \
        f"{hidden} categories already have a switch off after migration"
    total = db.session.query(ProductCategory).count()
    return f"4 NOT NULL columns; all {total} categories visible everywhere"


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
    assert grid is not None, "could not parse the POS grid payload"
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


@check("10. creating a category saves the switches")
def _():
    from app.models import ProductCategory
    _client().post("/products/hierarchy/categories", data={
        "group_id": str(_STATE["group_id"]), "name": "فئة جديدة للاختبار",
        # Only two ticked — the other two are simply absent, as a browser
        # would send them.
        "visible_in_manufacturing": "1", "visible_in_vendor_bills": "1",
    })
    c = ProductCategory.query.filter_by(
        company_id=_STATE["company_id"], name="فئة جديدة للاختبار").first()
    assert c is not None, "category was not created"
    try:
        assert c.visible_in_manufacturing is True
        assert c.visible_in_vendor_bills is True
        assert c.visible_in_pos is False, "unticked POS was saved as True"
        assert c.visible_in_customer_invoices is False
    finally:
        db.session.delete(c)
        db.session.commit()
    return "ticked saved True, absent saved False"


@check("11. editing saves the switches, including UNTICKING")
def _():
    from app.models import ProductCategory
    _reset_flags()
    cat_id = _STATE["raw_cat_id"]
    # Untick everything: a browser omits all four fields entirely.
    _client().post(f"/products/hierarchy/categories/{cat_id}/edit",
                   data={"name": "مواد خام"})
    db.session.expire_all()
    c = db.session.get(ProductCategory, cat_id)
    for f in ALL_FLAGS:
        assert getattr(c, f) is False, (
            f"{f} stayed True after unticking — the route reads .get() "
            "instead of presence, so a box can never be turned off")
    # Tick two back on.
    _client().post(f"/products/hierarchy/categories/{cat_id}/edit",
                   data={"name": "مواد خام",
                         "visible_in_manufacturing": "1",
                         "visible_in_vendor_bills": "1"})
    db.session.expire_all()
    c = db.session.get(ProductCategory, cat_id)
    assert c.visible_in_manufacturing is True
    assert c.visible_in_pos is False
    assert c.name == "مواد خام", "the name was lost while saving switches"
    _reset_flags()
    return "unticking persists; re-ticking persists; name preserved"


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
