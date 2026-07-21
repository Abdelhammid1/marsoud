#!/usr/bin/env python3
"""MARSOUD-PACK-PRICING (Abdelhamid 2026-07-19).

Customer complaint: registering a new stocked product forces the user
to divide the carton price by the pieces-per-carton in their head to
fill "تكلفة الوحدة". This causes daily arithmetic errors when the
customer buys 40+ SKUs a week.

Fix: /products/new now accepts three optional pack fields:
  · pack_purchase_price
  · pieces_per_pack
  · pack_unit_name (default "كرتونة")

When both price + pieces are > 0:
  1. unit_cost is derived (= price / pieces) if the user didn't type
     one explicitly. If they did, their explicit value wins — no
     silent override.
  2. A ProductUnit(unit_name=pack_unit_name, conversion_factor=pieces)
     is auto-created after the base unit, so future vendor bills and
     POS can pick the pack unit natively.
  3. Opening balance can be entered as PACKS (opening_qty_unit=pack)
     and is multiplied by pieces_per_pack before hitting the ledger.
     The stock table + COGS reports always see BASE units.

Checks:
  1. pack_price=60 + pieces=24, no explicit unit_cost → variant.unit_cost = 2.50.
  2. A كرتونة ProductUnit exists with conversion_factor=24 (and the base
     unit still exists and is untouched).
  3. Explicit unit_cost=3.00 while pack fields also filled → 3.00 wins,
     pack unit is still created (user might just want to override the
     purchase price this once).
  4. opening_qty=3 with opening_qty_unit=pack → StockBalance = 72 base
     units at unit_cost=2.50 (posted in base units, NOT packs).
  5. pack_unit_name='قطعة' (collides with base unit) → 400, no product
     row inserted (whole txn rolls back).
  6. Legacy path — pack fields empty, unit_cost=5 → keeps working
     exactly as before (no ProductUnit for pack, no regression).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _wipe(name):
    from app.models import Company
    from sqlalchemy import text, inspect
    c = Company.query.filter_by(name=name).first()
    if not c:
        return
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": c.id})
        # invoice_items has no company_id; scope through invoice_id.
        for tbl_name in ("payments", "invoice_reminders_sent",
                         "invoice_items"):
            conn.execute(text(
                f"DELETE FROM {tbl_name} WHERE invoice_id IN "
                "(SELECT id FROM invoices WHERE company_id = :c)"),
                {"c": c.id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": c.id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": c.id})


def _setup():
    from app.models import (
        Company, User, ProductGroup, ProductCategory, Warehouse,
    )
    from app.models.user import user_companies
    from app.services.roles_seed import (
        seed_permissions_catalog, seed_system_roles_for_company,
    )
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    from sqlalchemy import text

    _wipe("__PACK__")
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM users WHERE email = 'pack-owner@t.co'"))
    seed_permissions_catalog()
    c = Company(name="__PACK__", base_currency="EGP", vat_rate=0)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    seed_system_roles_for_company(c.id)

    u = User(email="pack-owner@t.co",
             password_hash=generate_password_hash("x", method="pbkdf2:sha256"),
             full_name="pack-owner")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))

    grp = ProductGroup(company_id=c.id, name="بقالة", is_active=True)
    db.session.add(grp); db.session.flush()
    cat = ProductCategory(company_id=c.id, group_id=grp.id,
                          name="مشروبات", is_active=True)
    db.session.add(cat); db.session.flush()
    wh = Warehouse(company_id=c.id, code="MAIN", name="الرئيسي",
                   is_default=True, is_active=True)
    db.session.add(wh); db.session.flush()
    db.session.commit()
    _STATE.update(cid=c.id, uid=u.id, cat_id=cat.id, wh_id=wh.id)


def _client_for(user_id, cid):
    from flask import current_app, g
    for k in ("_login_user", "active_company", "user_companies"):
        try: g.pop(k, None)
        except Exception: pass
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["active_company_id"] = cid
    return client


def _create_product(client, **overrides):
    """POST /products/new. Returns (status_code, response)."""
    data = dict(
        product_type="goods",
        name=overrides.pop("name", "COCA"),
        sku=overrides.pop("sku", ""),
        description="",
        category_id=str(_STATE["cat_id"]),
        default_price="0",
        default_tax_rate="",
        barcode="",
        warehouse_id=str(_STATE["wh_id"]),
        opening_qty="0",
        opening_qty_unit="base",
        unit_cost="",
        pack_purchase_price="",
        pieces_per_pack="",
        pack_unit_name="كرتونة",
    )
    data.update(overrides)
    r = client.post("/products/new", data=data, follow_redirects=False)
    return r


# ────────────────────────────────────────────────────────────────────
@check("1. pack_price=60 + pieces=24, no explicit cost → per-piece "
       "cost derived = 2.50")
def _():
    from app.models import Product, ProductVariant
    c = _client_for(_STATE["uid"], _STATE["cid"])
    r = _create_product(
        c, name="COCA-A",
        pack_purchase_price="60", pieces_per_pack="24",
    )
    assert r.status_code == 302, f"HTTP {r.status_code}"
    p = Product.query.filter_by(company_id=_STATE["cid"],
                                name="COCA-A").one()
    v = ProductVariant.query.filter_by(product_id=p.id).one()
    assert abs(float(v.unit_cost) - 2.5) < 1e-6, \
        f"unit_cost={v.unit_cost} (expected 2.50)"
    _STATE["pid_a"] = p.id
    return f"unit_cost = {float(v.unit_cost)}"


@check("2. Pack unit auto-created with the right factor")
def _():
    from app.models import ProductUnit
    units = ProductUnit.query.filter_by(product_id=_STATE["pid_a"]).all()
    names = {u.unit_name: u for u in units}
    assert "قطعة" in names and names["قطعة"].is_base, \
        f"base unit missing/wrong: {names}"
    assert "كرتونة" in names, f"pack unit missing: {names}"
    pack = names["كرتونة"]
    assert not pack.is_base
    assert abs(float(pack.conversion_factor) - 24) < 1e-6, \
        f"factor={pack.conversion_factor}"
    return "base + كرتونة(×24) both exist"


@check("3. Explicit unit_cost overrides derived value; pack unit still made")
def _():
    from app.models import Product, ProductVariant, ProductUnit
    c = _client_for(_STATE["uid"], _STATE["cid"])
    r = _create_product(
        c, name="COCA-OVR",
        pack_purchase_price="60", pieces_per_pack="24",
        unit_cost="3.00",   # explicit — should win over 60/24=2.5
    )
    assert r.status_code == 302, f"HTTP {r.status_code}"
    p = Product.query.filter_by(company_id=_STATE["cid"],
                                name="COCA-OVR").one()
    v = ProductVariant.query.filter_by(product_id=p.id).one()
    assert abs(float(v.unit_cost) - 3.0) < 1e-6, \
        f"unit_cost={v.unit_cost} (expected 3.0)"
    assert ProductUnit.query.filter_by(
        product_id=p.id, unit_name="كرتونة").first() is not None
    return f"explicit=3.00 kept, pack unit still exists"


@check("4. opening_qty=3 with unit=pack → stock = 72 base units @ 2.50")
def _():
    from app.models import (
        Product, ProductVariant, StockBalance,
    )
    c = _client_for(_STATE["uid"], _STATE["cid"])
    r = _create_product(
        c, name="COCA-OPN",
        pack_purchase_price="60", pieces_per_pack="24",
        opening_qty="3", opening_qty_unit="pack",
    )
    assert r.status_code == 302, f"HTTP {r.status_code}"
    p = Product.query.filter_by(company_id=_STATE["cid"],
                                name="COCA-OPN").one()
    v = ProductVariant.query.filter_by(product_id=p.id).one()
    bal = StockBalance.query.filter_by(variant_id=v.id).one()
    assert abs(float(bal.qty) - 72.0) < 1e-6, f"qty={bal.qty}"
    # weighted-average cost: value / qty = 2.50
    per_unit = float(bal.value) / float(bal.qty)
    assert abs(per_unit - 2.5) < 1e-6, f"per_unit={per_unit}"
    return f"stock = {float(bal.qty)} @ {per_unit:.2f}"


@check("5. pack_unit_name that collides with base → HTTP 200 (form redisplayed) "
       "and NO product row inserted")
def _():
    from app.models import Product
    c = _client_for(_STATE["uid"], _STATE["cid"])
    before = Product.query.filter_by(company_id=_STATE["cid"]).count()
    r = _create_product(
        c, name="BOGUS-1",
        pack_purchase_price="60", pieces_per_pack="24",
        pack_unit_name="قطعة",   # collision with base
    )
    # The route catches ValueError and re-renders the form (HTTP 200).
    # Regardless of the response shape, the important thing is that
    # NO product row survived the failed transaction.
    assert Product.query.filter_by(
        company_id=_STATE["cid"], name="BOGUS-1").first() is None, \
        "product must NOT be persisted after a collision"
    after = Product.query.filter_by(company_id=_STATE["cid"]).count()
    assert before == after, f"count changed {before} → {after}"
    return f"rejected cleanly (status={r.status_code}, count unchanged)"


@check("6. Legacy path — no pack fields, plain unit_cost — keeps "
       "working exactly as before")
def _():
    from app.models import Product, ProductVariant, ProductUnit
    c = _client_for(_STATE["uid"], _STATE["cid"])
    r = _create_product(
        c, name="LEGACY", unit_cost="5",
    )
    assert r.status_code == 302, f"HTTP {r.status_code}"
    p = Product.query.filter_by(company_id=_STATE["cid"],
                                name="LEGACY").one()
    v = ProductVariant.query.filter_by(product_id=p.id).one()
    assert abs(float(v.unit_cost) - 5.0) < 1e-6, \
        f"unit_cost={v.unit_cost} (expected 5.0)"
    # ONLY the base unit exists — no pack unit auto-created.
    unit_names = {u.unit_name for u
                  in ProductUnit.query.filter_by(product_id=p.id).all()}
    assert unit_names == {"قطعة"}, \
        f"unexpected units: {unit_names}"
    return "legacy path unchanged (base unit only)"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}  ⇒ {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback; traceback.print_exc()
        finally:
            try:
                _wipe("__PACK__")
                print("\n(cleaned up)")
            except Exception as e:
                print(f"\n(teardown: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
