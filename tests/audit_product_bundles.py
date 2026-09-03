#!/usr/bin/env python3
"""MARSOUD-PRODUCT-BUNDLES-01 (2026-09-02) — product bundles.

Bundle = a Product with `is_bundle=True` whose components enumerate
real ProductVariants. On POS sale, the bundle line is EXPANDED into
N real per-variant InvoiceItems before any inventory / JE work — so
`record_sale`, `post_invoice_to_ledger`, `inventory.py`, `ledger.py`
never see a "bundle".

Checks:
  1. Migration applied — is_bundle on products, bundle_components
     table, bundle_ref + bundle_product_id on invoice_items.
  2. Create bundle + persist components via the service.
  3. `expand_bundle_line` allocator: Σ line_total exactly equals
     bundle_qty × bundle_unit_price (to 0.01).
  4. `create_pos_order` with a bundle line → N per-component
     InvoiceItems, each with the right variant_id + bundle_ref +
     bundle_product_id.
  5. StockMovement count after a bundle sale = N (one per
     component), NOT N+1 (no wrapper row moves stock).
  6. `check_bundle_availability` refuses when a component is short.
  7. `validate_bundle_components` refuses empty list + bundle-in-
     bundle.
"""
import os
import sys
from pathlib import Path
from datetime import datetime, date, timedelta
from decimal import Decimal

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _boot(prefix):
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        "SELECT id FROM companies WHERE name LIKE :p"),
        {"p": f"__{prefix}__%"})]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"),
                    {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        "DELETE FROM users WHERE email LIKE :p"),
        {"p": f"%__{prefix.lower()}__%"})
    # Orphan cleanups so SQLite rowid reuse doesn't leak old rows.
    db.session.execute(text(
        "DELETE FROM invoice_items WHERE invoice_id NOT IN (SELECT id FROM invoices)"))
    db.session.execute(text(
        "DELETE FROM stock_movements WHERE company_id NOT IN (SELECT id FROM companies)"))
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C")
        db.session.add(plan)
    plan.set_modules(["accounting", "sales", "pos", "hr", "reports"])
    db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency="EGP",
                subdomain=prefix.lower(), plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.commit()
    seed_default_coa(c.id); db.session.commit()

    try:
        from app.services.legal import get_terms_version
        tv = get_terms_version() or "audit"
    except Exception:
        tv = "audit"
    owner = User(email=f"owner__{prefix.lower()}__@x.io",
                 full_name=f"Owner {prefix}", is_active=True,
                 email_verified_at=datetime.utcnow(),
                 terms_version=tv, terms_accepted_at=datetime.utcnow())
    owner.set_password("pw12345678")
    db.session.add(owner); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner"))
    db.session.commit()
    return owner.email, c.id, owner.id


def _make_product_with_variant(cid, *, name, price=10, is_bundle=False):
    """Build a Product + one ProductVariant + a base ProductUnit +
    (for goods) a Warehouse + a small opening StockBalance. Returns
    (product, variant)."""
    from app import db
    from app.models import (
        Product, ProductVariant, Warehouse, ProductCategory,
        ProductGroup,
    )
    from app.services.product_pricing import apply_pack_pricing

    grp = ProductGroup.query.filter_by(company_id=cid).first()
    if not grp:
        grp = ProductGroup(company_id=cid, name="عام", is_active=True)
        db.session.add(grp); db.session.flush()
    cat = ProductCategory.query.filter_by(company_id=cid, group_id=grp.id).first()
    if not cat:
        cat = ProductCategory(company_id=cid, group_id=grp.id,
                                name="عام", is_active=True)
        db.session.add(cat); db.session.flush()

    p = Product(company_id=cid, name=name,
                is_tracked=(not is_bundle), is_bundle=is_bundle,
                category_id=cat.id,
                default_price=Decimal(str(price)))
    db.session.add(p); db.session.flush()
    v = ProductVariant(company_id=cid, product_id=p.id,
                        sku=f"SKU-{p.id}",
                        name="", is_active=True,
                        unit_cost=Decimal("5"))
    db.session.add(v); db.session.flush()
    if not is_bundle:
        apply_pack_pricing(p, v, pieces=1, pack_purchase=5,
                           pack_sale=price, pack_name="قطعة",
                           is_goods=True)
    db.session.commit()
    return p, v


def _prime_stock(cid, variant, qty=100):
    from app import db
    from app.models import Warehouse, StockBalance
    from app.services.inventory import default_warehouse
    wh = default_warehouse(cid)
    if not wh:
        wh = Warehouse(company_id=cid, code=f"WH-{cid}",
                        name="مخزن",
                        is_default=True, is_active=True)
        db.session.add(wh); db.session.commit()
    sb = StockBalance.query.filter_by(
        variant_id=variant.id, warehouse_id=wh.id).first()
    if not sb:
        sb = StockBalance(company_id=cid, variant_id=variant.id,
                            warehouse_id=wh.id, qty=Decimal("0"))
        db.session.add(sb)
    sb.qty = Decimal(str(qty))
    db.session.commit()
    return wh


@check("1. migration applied — is_bundle + bundle_components + 2 cols")
def _():
    from app import create_app
    from sqlalchemy import inspect
    app = create_app()
    with app.app_context():
        from app import db
        insp = inspect(db.engine)
        assert "bundle_components" in insp.get_table_names()
        assert "is_bundle" in {
            c["name"] for c in insp.get_columns("products")}
        item_cols = {c["name"] for c in insp.get_columns("invoice_items")}
        assert "bundle_ref" in item_cols
        assert "bundle_product_id" in item_cols
        return "all schema present"


@check("2. create bundle + 3 components via validate_bundle_components")
def _():
    from app import create_app, db
    from app.models import Product, BundleComponent
    from app.services.bundles import validate_bundle_components

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("PB2")
        try:
            _, v1 = _make_product_with_variant(cid, name="شاي", price=10)
            _, v2 = _make_product_with_variant(cid, name="سكر", price=15)
            _, v3 = _make_product_with_variant(cid, name="بسكويت", price=20)
            bp, bv = _make_product_with_variant(
                cid, name="طقم إفطار", price=45, is_bundle=True)
            comps = [
                {"variant_id": v1.id, "qty_per_bundle": 1},
                {"variant_id": v2.id, "qty_per_bundle": 1},
                {"variant_id": v3.id, "qty_per_bundle": 2},
            ]
            validate_bundle_components(bp, comps)
            for c in comps:
                db.session.add(BundleComponent(
                    company_id=cid, bundle_product_id=bp.id,
                    component_variant_id=c["variant_id"],
                    qty_per_bundle=c["qty_per_bundle"],
                ))
            db.session.commit()
            db.session.refresh(bp)
            assert len(list(bp.bundle_components)) == 3
            return "3 components persisted"
        finally:
            pass


@check("3. expand_bundle_line: Σ line_total exactly = qty × price")
def _():
    from app import create_app, db
    from app.models import BundleComponent
    from app.services.bundles import expand_bundle_line

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("PB3")
        try:
            _, v1 = _make_product_with_variant(cid, name="شاي", price=10)
            _, v2 = _make_product_with_variant(cid, name="سكر", price=15)
            _, v3 = _make_product_with_variant(cid, name="بسكويت", price=20)
            bp, _ = _make_product_with_variant(
                cid, name="طقم إفطار", price=45, is_bundle=True)
            for vv, q in [(v1, 1), (v2, 1), (v3, 2)]:
                db.session.add(BundleComponent(
                    company_id=cid, bundle_product_id=bp.id,
                    component_variant_id=vv.id, qty_per_bundle=q))
            db.session.commit(); db.session.refresh(bp)
            # Odd price to force rounding
            lines = expand_bundle_line(bp, bundle_qty=3,
                                         bundle_unit_price=47.33)
            total = sum(l["line_value_hint"] for l in lines)
            expected = round(3 * 47.33, 2)
            assert abs(total - expected) < 0.005, \
                f"Σ={total} vs expected={expected}"
            assert len(lines) == 3
            return f"Σ={total:.2f} matches {expected:.2f} (rounding absorbed)"
        finally:
            pass


@check("4. create_pos_order expands bundle to N InvoiceItems")
def _():
    from app import create_app, db
    from app.models import (
        BundleComponent, InvoiceItem, PaymentMethod, Account,
    )
    from app.services.bundles import expand_bundle_line
    from app.services.pos import create_pos_order
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("PB4")
        try:
            _, v1 = _make_product_with_variant(cid, name="شاي", price=10)
            _, v2 = _make_product_with_variant(cid, name="سكر", price=15)
            _, v3 = _make_product_with_variant(cid, name="بسكويت", price=20)
            _prime_stock(cid, v1, qty=50)
            _prime_stock(cid, v2, qty=50)
            _prime_stock(cid, v3, qty=50)
            bp, bv = _make_product_with_variant(
                cid, name="طقم إفطار", price=45, is_bundle=True)
            for vv, q in [(v1, 1), (v2, 1), (v3, 2)]:
                db.session.add(BundleComponent(
                    company_id=cid, bundle_product_id=bp.id,
                    component_variant_id=vv.id, qty_per_bundle=q))
            db.session.commit()
            # Payment method — pick cash 1110
            cash = Account.query.filter_by(company_id=cid, code="1110").first()
            pm = PaymentMethod(company_id=cid, name="cash",
                                account_id=cash.id, is_active=True)
            db.session.add(pm); db.session.commit()
            invoice = create_pos_order(
                company_id=cid,
                items=[{"variant_id": bv.id, "qty": 1,
                        "unit_price": 45}],
                payment_method_id=pm.id, cashier_id=oid,
            )
            items = InvoiceItem.query.filter_by(
                invoice_id=invoice.id).all()
            assert len(items) == 3, f"expected 3 items, got {len(items)}"
            refs = {it.bundle_ref for it in items}
            assert refs and None not in refs, "bundle_ref missing"
            assert len(refs) == 1, "multiple bundle_refs on one bundle"
            bpids = {it.bundle_product_id for it in items}
            assert bpids == {bp.id}
            return f"3 expanded rows, shared bundle_ref={list(refs)[0][:6]}…"
        finally:
            pass


@check("5. StockMovement count = 3, not 4 (no wrapper row)")
def _():
    from app import create_app, db
    from app.models import (
        BundleComponent, PaymentMethod, Account, StockMovement,
    )
    from app.services.pos import create_pos_order
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("PB5")
        try:
            _, v1 = _make_product_with_variant(cid, name="شاي", price=10)
            _, v2 = _make_product_with_variant(cid, name="سكر", price=15)
            _, v3 = _make_product_with_variant(cid, name="بسكويت", price=20)
            _prime_stock(cid, v1)
            _prime_stock(cid, v2)
            _prime_stock(cid, v3)
            bp, bv = _make_product_with_variant(
                cid, name="طقم", price=45, is_bundle=True)
            for vv, q in [(v1, 1), (v2, 1), (v3, 1)]:
                db.session.add(BundleComponent(
                    company_id=cid, bundle_product_id=bp.id,
                    component_variant_id=vv.id, qty_per_bundle=q))
            db.session.commit()
            cash = Account.query.filter_by(company_id=cid, code="1110").first()
            pm = PaymentMethod(company_id=cid, name="cash",
                                account_id=cash.id, is_active=True)
            db.session.add(pm); db.session.commit()
            before = StockMovement.query.filter_by(company_id=cid).count()
            create_pos_order(
                company_id=cid,
                items=[{"variant_id": bv.id, "qty": 1,
                        "unit_price": 45}],
                payment_method_id=pm.id, cashier_id=oid,
            )
            after = StockMovement.query.filter_by(company_id=cid).count()
            assert after - before == 3, (
                f"expected +3 movements, got +{after - before}"
                " (wrapper row leaked?)")
            return f"Δ StockMovement = {after - before} (no wrapper)"
        finally:
            pass


@check("6. check_bundle_availability refuses on short component")
def _():
    from app import create_app, db
    from app.models import BundleComponent
    from app.services.bundles import check_bundle_availability
    from app.services.inventory import default_warehouse

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("PB6")
        try:
            _, v1 = _make_product_with_variant(cid, name="شاي", price=10)
            _, v2 = _make_product_with_variant(cid, name="سكر", price=15)
            _prime_stock(cid, v1, qty=100)
            _prime_stock(cid, v2, qty=1)   # will be short for 3× bundle
            bp, _ = _make_product_with_variant(
                cid, name="طقم", price=45, is_bundle=True)
            for vv, q in [(v1, 1), (v2, 2)]:
                db.session.add(BundleComponent(
                    company_id=cid, bundle_product_id=bp.id,
                    component_variant_id=vv.id, qty_per_bundle=q))
            db.session.commit(); db.session.refresh(bp)
            wh = default_warehouse(cid)
            ok, msg = check_bundle_availability(bp, 3, wh)
            assert not ok
            assert "غير كافية" in msg or "غير كافي" in msg
            return "short stock refused"
        finally:
            pass


@check("7. validate_bundle_components refuses empty + bundle-in-bundle")
def _():
    from app import create_app, db
    from app.models import BundleComponent
    from app.services.bundles import (
        validate_bundle_components, BundleError,
    )
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("PB7")
        try:
            bp, _ = _make_product_with_variant(
                cid, name="طقم", price=45, is_bundle=True)
            # 7a: empty list
            try:
                validate_bundle_components(bp, [])
            except BundleError as e:
                assert "مكونات" in str(e)
            else:
                raise AssertionError("empty list accepted")
            # 7b: bundle inside bundle
            inner, inner_v = _make_product_with_variant(
                cid, name="باقة داخلية", price=20, is_bundle=True)
            try:
                validate_bundle_components(bp, [
                    {"variant_id": inner_v.id, "qty_per_bundle": 1},
                ])
            except BundleError as e:
                assert "داخل باقة" in str(e)
                return "empty + bundle-in-bundle refused"
            raise AssertionError("bundle-in-bundle accepted")
        finally:
            pass


@check("8. POS index() surfaces bundles despite is_tracked=False "
        "(MARSOUD-PRODUCT-BUNDLES-02-POS-VISIBILITY)")
def _():
    """Regression for the POS visibility gap: bundles have
    is_tracked=False by design, and the pre-fix `filter_by(is_tracked
    =True)` in app/routes/pos.py:index() hid them completely from
    the cashier's product grid — cashiers could only reach them by
    typing a SKU. This check drives index() with a bundle in the
    fixture and asserts it appears in the rendered products list."""
    from app import create_app, db
    from app.models import User
    from flask_login import login_user
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("PB8")
        try:
            # One tracked normal product + one bundle → both should
            # show. Bundle has is_tracked=False by construction.
            _make_product_with_variant(
                cid, name="منتج عادي", price=10)
            bundle_p, bundle_v = _make_product_with_variant(
                cid, name="باقة سلطة", price=30, is_bundle=True)
            db.session.commit()
            assert bundle_p.is_tracked is False, (
                "test bundle must be untracked to exercise the bug")
            # Log in the owner via test-client session — index() is
            # gated by @login_required + @require_permission("pos.use").
            client = app.test_client()
            with client.session_transaction() as sess:
                sess["_user_id"] = str(oid)
                sess["_fresh"] = True
                sess["active_company_id"] = cid
            r = client.get("/pos/")
            # Either 200 (rendered) or a shift-open redirect (302 to
            # /pos/shifts/open). We just need the query itself to
            # include the bundle — so if it 302'd because of the
            # shift gate, disable the shift requirement and retry.
            if r.status_code == 302:
                # Force-disable the shift gate for this tenant. The
                # gate reads Company.pos_requires_shift; setting it
                # False sidesteps the redirect entirely.
                from app.models import Company
                co = db.session.get(Company, cid)
                if hasattr(co, "pos_requires_shift"):
                    co.pos_requires_shift = False
                    db.session.commit()
                r = client.get("/pos/")
            assert r.status_code == 200, (
                f"POS index failed: {r.status_code} "
                f"{r.get_data(as_text=True)[:200]}")
            html = r.get_data(as_text=True)
            # products_by_cat|tojson unicode-escapes Arabic so match
            # on the ASCII SKU (unique per product) instead.
            bundle_sku = f"SKU-{bundle_p.id}"
            assert bundle_sku in html, (
                f"bundle SKU {bundle_sku!r} missing from rendered "
                "POS product grid — the is_tracked=True filter is "
                "still shadowing bundles")
            return f"bundle SKU {bundle_sku} rendered in POS grid"
        finally:
            pass


def main():
    passed = failed = 0
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
