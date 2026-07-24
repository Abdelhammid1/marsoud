#!/usr/bin/env python3
"""MARSOUD-DUAL-UOM-WEIGHT-01 (Abdelhamid 2026-07-24).

Checks:
  1. Product with tracks_piece_count=False → piece_delta is ignored
     (backwards-compatible default; balance.piece_count stays 0).
  2. Product with tracks_piece_count=True: receive_stock with
     piece_delta=10 → qty grows AND piece_count grows.
  3. Sell by piece: record_sale(qty=7.5g, piece_delta=-1) → qty drops
     7.5, pieces drop 1. avg_cost stays weight-based.
  4. Sell by weight: record_sale(qty=12g) → qty drops 12, pieces
     unchanged.
  5. InventoryCount with variance → CONFIRMED, adjustment_movement
     posted, piece_count aligned.
  6. Zero-variance count → CONFIRMED, no stock movement, no JE.
"""
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

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


def _teardown():
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__DU_%__'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'du-%@x.test'"))
        # stock_balances + stock_lots have no company_id — cascade via
        # variant_id after the variants were purged.
        conn.execute(text(
            "DELETE FROM stock_balances WHERE variant_id NOT IN "
            "(SELECT id FROM product_variants)"))
        conn.execute(text(
            "DELETE FROM stock_lots WHERE variant_id NOT IN "
            "(SELECT id FROM product_variants)"))
        conn.execute(text(
            "DELETE FROM stock_movements WHERE variant_id NOT IN "
            "(SELECT id FROM product_variants)"))


def _bootstrap(tracks_piece=False):
    from app.models import (
        Company, Product, ProductVariant, Warehouse, User, UserStatus,
        ProductGroup, ProductCategory,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    suffix = "P" if tracks_piece else "W"
    c = Company(name=f"__DU_{suffix}__", base_currency="EGP",
                 subdomain=f"du-{suffix.lower()}",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    # Product hierarchy (required for new products).
    g = ProductGroup(company_id=c.id, name="عام")
    db.session.add(g); db.session.flush()
    cat = ProductCategory(company_id=c.id, group_id=g.id, name="عام")
    db.session.add(cat); db.session.flush()
    u = User(email=f"du-{suffix.lower()}@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name=f"du-{suffix}", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    wh = Warehouse(company_id=c.id, code="MAIN",
                    name="المخزن الرئيسي", is_default=True)
    db.session.add(wh); db.session.flush()
    p = Product(company_id=c.id, name="فضة", is_tracked=True,
                 category_id=cat.id,
                 tracks_piece_count=tracks_piece)
    db.session.add(p); db.session.flush()
    v = ProductVariant(company_id=c.id, product_id=p.id,
                        sku=f"DU-{suffix}-SKU",
                        name="default", unit_cost=0)
    db.session.add(v); db.session.commit()
    return c, u, p, v, wh


@check("1. tracks_piece_count=False → piece_delta ignored")
def _():
    from app.services.inventory import receive_stock
    from app.models import StockBalance
    _teardown()
    c, u, p, v, wh = _bootstrap(tracks_piece=False)
    receive_stock(variant=v, warehouse=wh, qty=100, unit_cost=5,
                   piece_delta=999, actor_id=u.id)
    db.session.commit()
    bal = StockBalance.query.filter_by(
        variant_id=v.id, warehouse_id=wh.id).first()
    assert float(bal.qty) == 100.0
    assert float(bal.piece_count) == 0.0, \
        f"piece_count leaked: {bal.piece_count}"
    return "qty=100, pieces stays 0"


@check("2. tracks_piece_count=True: receive → qty AND pieces grow")
def _():
    from app.services.inventory import receive_stock
    from app.models import StockBalance
    c, u, p, v, wh = _bootstrap(tracks_piece=True)
    _STATE["c"] = c; _STATE["u"] = u
    _STATE["p"] = p; _STATE["v"] = v; _STATE["wh"] = wh
    receive_stock(variant=v, warehouse=wh, qty=500, unit_cost=10,
                   piece_delta=10, actor_id=u.id)
    db.session.commit()
    bal = StockBalance.query.filter_by(
        variant_id=v.id, warehouse_id=wh.id).first()
    assert float(bal.qty) == 500.0
    assert float(bal.piece_count) == 10.0
    return "qty=500g, pieces=10"


@check("3. Sell by piece: qty -7.5g + pieces -1")
def _():
    from app.services.inventory import record_sale
    from app.models import StockBalance
    v = _STATE["v"]; wh = _STATE["wh"]; u = _STATE["u"]
    record_sale(variant=v, warehouse=wh, qty=7.5, piece_delta=-1,
                 actor_id=u.id)
    db.session.commit()
    bal = StockBalance.query.filter_by(
        variant_id=v.id, warehouse_id=wh.id).first()
    assert float(bal.qty) == 492.5, f"qty={bal.qty}"
    assert float(bal.piece_count) == 9.0, f"pieces={bal.piece_count}"
    assert abs(bal.avg_cost - 10.0) < 0.001, \
        f"avg_cost drifted: {bal.avg_cost}"
    return "qty=492.5g, pieces=9, avg_cost stable"


@check("4. Sell by weight (no piece_delta) → pieces unchanged")
def _():
    from app.services.inventory import record_sale
    from app.models import StockBalance
    v = _STATE["v"]; wh = _STATE["wh"]; u = _STATE["u"]
    record_sale(variant=v, warehouse=wh, qty=12.0, actor_id=u.id)
    db.session.commit()
    bal = StockBalance.query.filter_by(
        variant_id=v.id, warehouse_id=wh.id).first()
    assert float(bal.qty) == 480.5, f"qty={bal.qty}"
    assert float(bal.piece_count) == 9.0, \
        f"pieces changed: {bal.piece_count}"
    return "qty=480.5g, pieces still 9"


@check("5. InventoryCount with variance → CONFIRMED + adjustment posted")
def _():
    from app.services.inventory import (
        start_inventory_count, commit_inventory_count,
    )
    from app.models import (
        InventoryCount, StockBalance, StockMovement,
        INV_COUNT_CONFIRMED,
    )
    v = _STATE["v"]; wh = _STATE["wh"]; u = _STATE["u"]
    # Physical count: 475g / 8 pieces  → variance qty=-5.5, pieces=-1.
    row = start_inventory_count(
        variant=v, warehouse=wh,
        counted_qty=475, counted_pieces=8,
        counted_by_id=u.id,
    )
    assert row.status == "DRAFT"
    assert float(row.variance_qty) == -5.5, \
        f"variance_qty={row.variance_qty}"
    assert float(row.variance_pieces) == -1.0
    committed = commit_inventory_count(row, actor_id=u.id)
    assert committed.status == INV_COUNT_CONFIRMED
    assert committed.adjustment_movement_id, \
        "adjustment_movement not linked"
    bal = StockBalance.query.filter_by(
        variant_id=v.id, warehouse_id=wh.id).first()
    assert float(bal.qty) == 475.0, f"post-adj qty={bal.qty}"
    assert float(bal.piece_count) == 8.0, \
        f"post-adj pieces={bal.piece_count}"
    return f"CONFIRMED, adj={committed.adjustment_movement_id}"


@check("6. Zero-variance count → CONFIRMED, no adjustment")
def _():
    from app.services.inventory import (
        start_inventory_count, commit_inventory_count,
    )
    from app.models import INV_COUNT_CONFIRMED
    v = _STATE["v"]; wh = _STATE["wh"]; u = _STATE["u"]
    # Book is now 475g / 8 pieces. Count matches exactly.
    row = start_inventory_count(
        variant=v, warehouse=wh,
        counted_qty=475, counted_pieces=8,
        counted_by_id=u.id,
    )
    assert float(row.variance_qty) == 0
    assert float(row.variance_pieces) == 0
    committed = commit_inventory_count(row, actor_id=u.id)
    assert committed.status == INV_COUNT_CONFIRMED
    assert committed.adjustment_movement_id is None, \
        "zero-variance shouldn't post an adjustment"
    return "CONFIRMED, no adjustment"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _teardown()
            for label, fn in CHECKS:
                try:
                    res = fn()
                    print(f"PASS  {label}  ⇒ {res}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback; traceback.print_exc()
        finally:
            _teardown()
            print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
