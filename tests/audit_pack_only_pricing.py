#!/usr/bin/env python3
"""MARSOUD-PACK-ONLY-PRICING — no manual per-piece price or cost.

The product form used to take a per-piece price and a per-piece cost
while the user buys and sells by the box. Someone typed the box price
into the piece field and a piece sold for 2100 instead of 0.42. The
fields are gone: the user enters box numbers, the system divides.

Checks:
  1. goods 24 / buy 60 / sell 72 → unit_cost 2.5, default_price 3.0,
     and a كرتونة unit with factor 24 priced at 72
  2. pieces = 1 → no pack unit; per-piece values equal what was typed
  3. service → priced, no cost demanded, no units
  4. validation: pieces < 1 and a zero sale price are both refused
  5. edit round-trip shows the box numbers back exactly, and re-saving
     unchanged leaves every stored value identical
  6. a POST smuggling default_price / unit_cost is ignored
  7. the forms expose no per-piece input, and the units page base row
     is display-only
"""
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
COMPANY_NAME = "__PACK_ONLY_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import Company, User, Plan, ProductGroup, ProductCategory
    from app.services.seed_coa import seed_default_coa
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.roles import set_membership_role
    from app.services.legal import get_terms_version
    from app.services.subscription import activate_default_subscription

    _teardown()
    co = Company(name=COMPANY_NAME, base_currency="EGP", vat_rate=0)
    db.session.add(co)
    db.session.flush()
    # Trial window unlocks the per-page sub-item gate; the plan still has
    # to carry the module, because plan_allows() has no trial bypass.
    activate_default_subscription(co)
    pl = next((p for p in Plan.query.order_by(Plan.id).all()
               if "sales" in (p.modules or [])), None)
    assert pl is not None, "no seeded plan enables the sales module"
    co.plan_id = pl.id
    co.intended_plan_id = pl.id
    db.session.commit()

    seed_default_coa(co.id)
    ensure_roles_ready_for_company(co.id)

    u = User(email="__packonly@audit.local", full_name="PackOnly Owner",
             is_active=True, terms_version=get_terms_version(),
             terms_accepted_at=datetime.utcnow())
    u.set_password("Passw0rd!audit1")
    db.session.add(u)
    db.session.flush()
    db.session.commit()
    set_membership_role(u.id, co.id, "owner")

    g = ProductGroup(company_id=co.id, name="عام")
    db.session.add(g)
    db.session.flush()
    c = ProductCategory(company_id=co.id, group_id=g.id, name="عام")
    db.session.add(c)
    db.session.commit()

    _STATE.update(cid=co.id, uid=u.id, cat_id=c.id)


def _teardown():
    from sqlalchemy import text
    from app.models import Company, User
    co = Company.query.filter_by(name=COMPANY_NAME).first()
    if co:
        cid = co.id
        for s in [
            "DELETE FROM stock_movements WHERE variant_id IN "
            "(SELECT id FROM product_variants WHERE company_id=:c)",
            "DELETE FROM stock_balances WHERE variant_id IN "
            "(SELECT id FROM product_variants WHERE company_id=:c)",
            "DELETE FROM product_units WHERE company_id=:c",
            "DELETE FROM product_variants WHERE company_id=:c",
            "DELETE FROM products WHERE company_id=:c",
            "DELETE FROM product_categories WHERE company_id=:c",
            "DELETE FROM product_groups WHERE company_id=:c",
            "DELETE FROM journal_lines WHERE entry_id IN "
            "(SELECT id FROM journal_entries WHERE company_id=:c)",
            "DELETE FROM journal_entries WHERE company_id=:c",
            "DELETE FROM payment_methods WHERE company_id=:c",
            "DELETE FROM accounts WHERE company_id=:c",
            "DELETE FROM user_companies WHERE company_id=:c",
            "DELETE FROM role_permissions WHERE role_id IN "
            "(SELECT id FROM roles WHERE company_id=:c)",
            "DELETE FROM roles WHERE company_id=:c",
            "DELETE FROM doc_sequences WHERE company_id=:c",
            "DELETE FROM warehouses WHERE company_id=:c",
            "DELETE FROM companies WHERE id=:c",
        ]:
            try:
                db.session.execute(text(s), {"c": cid})
                db.session.commit()
            except Exception:
                db.session.rollback()
    u = User.query.filter_by(email="__packonly@audit.local").first()
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


def _post_new(**over):
    data = {
        "product_type": "goods",
        "name": over.pop("name", "منتج اختبار"),
        "category_id": str(_STATE["cat_id"]),
        "pieces_per_pack": "24",
        "pack_purchase_price": "60",
        "pack_sale_price": "72",
        "pack_unit_name": "كرتونة",
        "opening_qty": "0",
    }
    data.update(over)
    return _client().post("/products/new", data=data, follow_redirects=True)


def _product(name):
    from app.models import Product
    return Product.query.filter_by(
        company_id=_STATE["cid"], name=name).first()


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. box 24 / buy 60 / sell 72 → cost 2.5, price 3.0, كرتونة @ 72")
def _():
    r = _post_new(name="مياه بالعلبة")
    assert r.status_code == 200, r.status_code
    p = _product("مياه بالعلبة")
    assert p is not None, "product not created"
    v = p.default_variant
    assert abs(float(v.unit_cost) - 2.5) < 1e-6, f"unit_cost={v.unit_cost}"
    assert abs(float(p.default_price) - 3.0) < 1e-6, f"price={p.default_price}"
    assert p.pack_pieces == 24 and abs(float(p.pack_purchase_price) - 60) < 1e-6
    packs = [u for u in p.units if not u.is_base]
    assert len(packs) == 1, [u.unit_name for u in p.units]
    assert packs[0].unit_name == "كرتونة"
    assert abs(float(packs[0].conversion_factor) - 24) < 1e-6
    assert abs(float(packs[0].sale_price) - 72) < 1e-6, \
        f"pack sale price not stored: {packs[0].sale_price}"
    _STATE["p1"] = p.id
    return "cost 2.50 · price 3.00 · كرتونة×24 @ 72"


@check("2. pieces = 1 → no pack unit, box price IS the piece price")
def _():
    r = _post_new(name="قلم مفرد", pieces_per_pack="1",
                  pack_purchase_price="7", pack_sale_price="10")
    assert r.status_code == 200
    p = _product("قلم مفرد")
    assert p is not None, "product not created"
    assert abs(float(p.default_variant.unit_cost) - 7.0) < 1e-6
    assert abs(float(p.default_price) - 10.0) < 1e-6
    assert [u.unit_name for u in p.units if not u.is_base] == [], \
        "a redundant pack unit was created for an individually-sold item"
    assert p.pack_pieces == 1
    return "cost 7 · price 10 · base unit only"


@check("3. service → priced, no cost demanded, no units")
def _():
    r = _post_new(name="استشارة", product_type="service",
                  pieces_per_pack="", pack_purchase_price="",
                  pack_sale_price="150")
    assert r.status_code == 200
    p = _product("استشارة")
    assert p is not None, "service not created"
    assert abs(float(p.default_price) - 150.0) < 1e-6, p.default_price
    assert p.is_tracked is False
    assert list(p.units) == [], "a service should not get units"
    return "price 150, no purchase side, no units"


@check("4. pieces < 1 and a zero sale price are both refused")
def _():
    from app.models import Product
    before = Product.query.filter_by(company_id=_STATE["cid"]).count()
    r = _post_new(name="رفض صفر قطع", pieces_per_pack="0")
    assert "عدد القطع" in r.get_data(as_text=True), "no error shown for pieces=0"
    assert _product("رفض صفر قطع") is None, "product saved with pieces=0"

    r = _post_new(name="رفض بدون سعر بيع", pack_sale_price="0")
    assert "سعر بيع العلبة" in r.get_data(as_text=True), "no error for sale=0"
    assert _product("رفض بدون سعر بيع") is None, "product saved with sale=0"

    r = _post_new(name="رفض بدون سعر شراء", pack_purchase_price="0")
    assert _product("رفض بدون سعر شراء") is None, "product saved with buy=0"

    after = Product.query.filter_by(company_id=_STATE["cid"]).count()
    assert before == after, f"rows leaked: {before} → {after}"
    return "all three invalid inputs rejected, nothing persisted"


@check("5. edit round-trip returns the box numbers unchanged")
def _():
    from app.models import Product
    pid = _STATE["p1"]
    body = _client().get(f"/products/{pid}/edit").get_data(as_text=True)
    for field, want in (("pieces_per_pack", "24"),
                        ("pack_purchase_price", "60"),
                        ("pack_sale_price", "72")):
        m = re.search(r'name="%s"[^>]*value="([^"]*)"' % field, body)
        assert m, f"{field} not rendered on the edit form"
        assert abs(float(m.group(1)) - float(want)) < 1e-6, \
            f"{field} came back as {m.group(1)}, expected {want}"

    # Re-save unchanged → nothing moves.
    p = db.session.get(Product, pid)
    before = (float(p.default_price), float(p.default_variant.unit_cost),
              p.pack_pieces, float(p.pack_purchase_price))
    r = _client().post(f"/products/{pid}/edit", data={
        "name": p.name, "category_id": str(_STATE["cat_id"]),
        "pieces_per_pack": "24", "pack_purchase_price": "60",
        "pack_sale_price": "72", "pack_unit_name": "كرتونة",
        "is_active": "on",
    }, follow_redirects=True)
    assert r.status_code == 200
    db.session.expire_all()
    p = db.session.get(Product, pid)
    after = (float(p.default_price), float(p.default_variant.unit_cost),
             p.pack_pieces, float(p.pack_purchase_price))
    assert before == after, f"values drifted on re-save: {before} → {after}"
    packs = [u for u in p.units if not u.is_base]
    assert len(packs) == 1, \
        f"editing duplicated the pack unit: {[u.unit_name for u in p.units]}"
    return f"24/60/72 round-tripped; still one pack unit"


@check("6. a POST smuggling default_price / unit_cost is ignored")
def _():
    r = _post_new(name="محاولة تهريب", pieces_per_pack="10",
                  pack_purchase_price="100", pack_sale_price="200",
                  default_price="9999", unit_cost="8888")
    assert r.status_code == 200
    p = _product("محاولة تهريب")
    assert p is not None
    assert abs(float(p.default_price) - 20.0) < 1e-6, \
        f"smuggled default_price won: {p.default_price}"
    assert abs(float(p.default_variant.unit_cost) - 10.0) < 1e-6, \
        f"smuggled unit_cost won: {p.default_variant.unit_cost}"
    return "derived 10 / 20 kept; posted 8888 / 9999 ignored"


@check("10. a product created before this ticket edits without damage")
def _():
    # Every product already in production is a "legacy" one: pack
    # columns NULL, per-piece values typed directly, possibly a pack
    # unit left with no sale_price. Backfilling them is out of scope,
    # so the least this must do is open and re-save without zeroing a
    # price or losing a unit.
    from app.models import Product, ProductVariant
    from app.services.units import ensure_base_unit, create_unit
    cid = _STATE["cid"]
    p = Product(company_id=cid, name="منتج قديم", is_tracked=True,
                sku="LEGACY-1", category_id=_STATE["cat_id"],
                default_price=3)
    db.session.add(p)
    db.session.flush()
    v = ProductVariant(company_id=cid, product_id=p.id, sku="LEGACY-1",
                       name="", unit_cost=2.5)
    db.session.add(v)
    db.session.flush()
    ensure_base_unit(p)
    create_unit(p, unit_name="كرتونة", conversion_factor=24, sale_price=None)
    db.session.commit()
    pid = p.id
    assert p.pack_pieces in (None, 1) and p.pack_purchase_price is None

    body = _client().get(f"/products/{pid}/edit").get_data(as_text=True)
    assert "pack_sale_price" in body, "edit page failed to render a legacy product"
    m = re.search(r'name="pack_sale_price"[^>]*value="([^"]*)"', body)
    assert m and abs(float(m.group(1)) - 3.0) < 1e-6, \
        f"legacy sale price prefilled as {m and m.group(1)}, expected 3.0"

    before_units = {u.unit_name: float(u.conversion_factor) for u in p.units}
    r = _client().post(f"/products/{pid}/edit", data={
        "name": "منتج قديم", "category_id": str(_STATE["cat_id"]),
        "is_active": "on", "pieces_per_pack": "1",
        "pack_purchase_price": "2.5", "pack_sale_price": "3.0",
        "pack_unit_name": "كرتونة",
    }, follow_redirects=True)
    assert r.status_code == 200
    db.session.expire_all()
    p = db.session.get(Product, pid)
    assert abs(float(p.default_price) - 3.0) < 1e-6, \
        f"legacy price changed to {p.default_price}"
    assert abs(float(p.default_variant.unit_cost) - 2.5) < 1e-6, \
        f"legacy cost changed to {p.default_variant.unit_cost}"
    after_units = {u.unit_name: float(u.conversion_factor) for u in p.units}
    assert after_units == before_units, \
        f"units changed: {before_units} → {after_units}"
    return "opens, prefills 3.0, re-saves with price/cost/units intact"


@check("9. a box name colliding with the base unit is rejected")
def _():
    # Carried over from audit_product_pack_pricing.py check 5 (the one
    # check in that suite that still ran). Two units with the same name
    # would merge in the POS picker and mis-price lines.
    from app.models import Product
    before = Product.query.filter_by(company_id=_STATE["cid"]).count()
    r = _post_new(name="تصادم الاسم", pack_unit_name="قطعة")
    body = r.get_data(as_text=True)
    assert "اسم العلبة مطابق" in body or _product("تصادم الاسم") is None, \
        "collision was accepted"
    assert _product("تصادم الاسم") is None, "product persisted despite collision"
    assert Product.query.filter_by(company_id=_STATE["cid"]).count() == before
    return "rejected, nothing persisted"


@check("8. opening balance entered in boxes still lands as pieces")
def _():
    # Carried over from audit_product_pack_pricing.py check 4, which this
    # suite supersedes: 3 boxes of 24 = 72 pieces, valued at the derived
    # per-piece cost.
    from app.models import Warehouse, StockBalance
    from app.services.inventory import default_warehouse
    cid = _STATE["cid"]
    wh = default_warehouse(cid)
    if wh is None:
        wh = Warehouse(company_id=cid, code="MAIN", name="الرئيسي",
                       is_default=True)
        db.session.add(wh)
        db.session.commit()
    r = _post_new(name="افتتاحي بالعلبة", opening_qty="3",
                  opening_qty_unit="pack", warehouse_id=str(wh.id))
    assert r.status_code == 200
    p = _product("افتتاحي بالعلبة")
    assert p is not None, "product not created"
    bal = StockBalance.query.filter_by(
        variant_id=p.default_variant.id, warehouse_id=wh.id).first()
    assert bal is not None, "no opening stock recorded"
    assert abs(float(bal.qty) - 72.0) < 1e-6, f"qty={bal.qty}, expected 72"
    assert abs(float(bal.value) / float(bal.qty) - 2.5) < 1e-6, \
        f"valued at {float(bal.value) / float(bal.qty)}, expected 2.50"
    return "3 boxes → 72 pieces @ 2.50"


@check("7. no per-piece input on the forms; units base row is read-only")
def _():
    tpl = ROOT / "app" / "templates" / "products"
    create = (tpl / "form.html").read_text(encoding="utf-8")
    edit = (tpl / "edit.html").read_text(encoding="utf-8")
    block = (tpl / "_pricing_block.html").read_text(encoding="utf-8")

    assert 'name="default_price"' not in create, "create form still asks a piece price"
    assert 'name="unit_cost"' not in create, "create form still asks a piece cost"
    assert 'name="default_price"' not in edit, "edit form still asks a piece price"
    for f in ("pieces_per_pack", "pack_purchase_price", "pack_sale_price"):
        assert f'name="{f}"' in block, f"{f} missing from the pricing block"
    # Each of the three carries its explanatory line.
    for hint in ("سيبها 1", "بتدفعه للمورد", "بتبيع بيه العلبة"):
        assert hint in block, f"helper text missing: {hint}"
    assert 'id="pack-derived"' in block, "live result line missing"

    units = (tpl / "units.html").read_text(encoding="utf-8")
    base_part = units.split("{% else %}")[0]
    assert 'name="sale_price"' not in base_part, \
        "base unit row still has an editable price"
    assert "عدّل من هنا" in units, "no link back to the product page"
    assert 'name="pack_purchase_price"' not in units, \
        "the second cost entry is still on the units page"
    assert "مش للقطعة الواحدة" in units, "unit price grain note missing"
    return "forms are box-only; base row display-only; helpers present"


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
