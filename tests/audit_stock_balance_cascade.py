#!/usr/bin/env python3
"""MARSOUD-STOCK-BALANCE-CASCADE (2026-08-04).

`tests/_orphan_sweep.py` kept reporting the same thing on every run:

    orphan_sweep purged rows: {'stock_balances': 5}

Root cause: hard_delete_company (app/services/lifecycle.py) purges a
company by walking every table that HAS a company_id column.
`stock_balances` was the only inventory table without one — stock_movements
and stock_lots both have it — so the purge deleted product_variants and
warehouses and skipped the balances, orphaning every row on BOTH foreign
keys at once. The variant_id CASCADE never fired because SQLite does not
enforce foreign keys without PRAGMA foreign_keys=ON, which the app never
sets, and warehouse_id had no ondelete at all.

The fix is structural, not a bigger broom: company_id on the table, so
the existing purge loop finds it, plus CASCADE on both parent FKs.

Checks:
  1. The model carries company_id and both parent FKs cascade.
  2. Balances are written with company_id populated.
  3. THE BUG: hard_delete_company leaves no orphan behind.
  4. ...and the sweep confirms it, finding nothing to purge.
  5. The sweep now checks the warehouse side, which it never did.
  6. The purge is complete: no inventory rows survive at all.
"""
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
COMPANY_NAME = "__SB_CASCADE__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ────────────────────────────────────────────────────────────
def _build_company():
    """A company with a warehouse, a product variant and a stock balance
    — the exact shape that used to leave orphans behind."""
    from app.models import (
        Company, Warehouse, Product, ProductVariant, StockBalance,
    )
    from app.services.seed_coa import seed_default_coa

    co = Company(name=COMPANY_NAME, base_currency="EGP", vat_rate=0)
    db.session.add(co)
    db.session.flush()
    seed_default_coa(co.id)

    wh = Warehouse(company_id=co.id, code="SBC1", name="مخزن الاختبار",
                   is_active=True)
    db.session.add(wh)
    db.session.flush()

    p = Product(company_id=co.id, name="صنف اختبار", is_active=True)
    db.session.add(p)
    db.session.flush()

    ids = []
    for i in range(3):
        v = ProductVariant(company_id=co.id, product_id=p.id,
                           sku=f"SBC-SKU-{i}", is_active=True)
        db.session.add(v)
        db.session.flush()
        db.session.add(StockBalance(
            variant_id=v.id, warehouse_id=wh.id, company_id=co.id,
            qty=10, value=100))
        ids.append(v.id)
    db.session.commit()
    return co.id, wh.id, ids


def _count(sql, **kw):
    from sqlalchemy import text
    return db.session.execute(text(sql), kw).scalar()


def _orphan_counts():
    return {
        "by_variant": _count(
            "SELECT COUNT(*) FROM stock_balances WHERE variant_id "
            "NOT IN (SELECT id FROM product_variants)"),
        "by_warehouse": _count(
            "SELECT COUNT(*) FROM stock_balances WHERE warehouse_id "
            "NOT IN (SELECT id FROM warehouses)"),
    }


def _teardown():
    from app.models import Company
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    for co in Company.query.filter_by(name=COMPANY_NAME).all():
        cid = co.id
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(
                        text(f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        db.session.execute(text("DELETE FROM companies WHERE id=:c"),
                           {"c": cid})
        db.session.commit()


# ─── 1-2. the structural fix ────────────────────────────────────────────
@check("1. StockBalance has company_id and both parent FKs cascade")
def _():
    from app.models import StockBalance
    cols = StockBalance.__table__.c
    assert "company_id" in cols, \
        "stock_balances still has no company_id — hard_delete_company " \
        "skips every table without one"
    assert not cols["company_id"].nullable, "company_id must be NOT NULL"

    cascading = {}
    for fk in StockBalance.__table__.foreign_keys:
        cascading[fk.parent.name] = (fk.ondelete or "").upper()
    for col in ("variant_id", "warehouse_id", "company_id"):
        assert cascading.get(col) == "CASCADE", (
            f"{col} FK ondelete is {cascading.get(col)!r}, expected CASCADE")
    return f"company_id NOT NULL · {len(cascading)} FKs all CASCADE"


@check("2. balances are written with company_id populated")
def _():
    """The column is only useful if every writer fills it — a NULL would
    be invisible to the purge loop all over again."""
    from app.models import Warehouse, ProductVariant, StockBalance
    from app.services.inventory import _lock_balance
    cid, wh_id, vids = _STATE["ids"]
    # Go through the real service writer, not a hand-built row.
    bal = _lock_balance(vids[0], wh_id)
    db.session.commit()
    assert bal.company_id == cid, \
        f"_lock_balance wrote company_id={bal.company_id}, expected {cid}"
    nulls = _count("SELECT COUNT(*) FROM stock_balances "
                   "WHERE company_id IS NULL")
    assert nulls == 0, f"{nulls} stock_balances rows have a NULL company_id"
    # And the transient row the model hands out carries it too.
    v = db.session.get(ProductVariant, vids[0])
    wh2 = Warehouse(company_id=cid, code="SBC2", name="مخزن ٢")
    db.session.add(wh2)
    db.session.flush()
    assert v.balance_in(wh2).company_id == cid, \
        "ProductVariant.balance_in() builds a row with no company_id"
    db.session.rollback()
    return "service writer + model helper both stamp company_id"


# ─── 3-4. THE BUG ───────────────────────────────────────────────────────
@check("3. hard_delete_company leaves no orphaned stock_balances")
def _():
    from app.models import Company
    from app.services.lifecycle import hard_delete_company
    cid, wh_id, vids = _STATE["ids"]

    before = _count("SELECT COUNT(*) FROM stock_balances "
                    "WHERE company_id=:c", c=cid)
    assert before >= 3, f"fixture wrote only {before} balances"

    co = db.session.get(Company, cid)
    hard_delete_company(co, actor_id=None, reason="audit")
    _STATE["purged"] = True

    left = _count("SELECT COUNT(*) FROM stock_balances "
                  "WHERE company_id=:c", c=cid)
    assert left == 0, (
        f"{left} of {before} stock_balances rows survived the purge — "
        "this is the orphan bug")
    orphans = _orphan_counts()
    assert orphans["by_variant"] == 0, (
        f"{orphans['by_variant']} rows orphaned on variant_id — the "
        "original 'orphan_sweep purged rows' report")
    assert orphans["by_warehouse"] == 0, (
        f"{orphans['by_warehouse']} rows orphaned on warehouse_id — the "
        "half no sweep ever looked at")
    return f"{before} balances purged, 0 orphans on either FK"


@check("4. the orphan sweep now finds nothing to purge")
def _():
    """The report that opened the ticket was the sweep cleaning up after
    the purge. With the purge complete, the sweep must be a no-op.

    The ticket asks specifically for `tests/_orphan_sweep.py` preflight()
    — the script whose output ("orphan_sweep purged rows:
    {'stock_balances': 5}") started this — so run that one, then the
    runtime twin that fires on every boot."""
    assert _STATE.get("purged"), "check 3 must run first"

    before = _count("SELECT COUNT(*) FROM stock_balances")
    from tests._orphan_sweep import preflight
    preflight()
    after = _count("SELECT COUNT(*) FROM stock_balances")
    assert before == after, (
        f"preflight() still had to delete {before - after} "
        "stock_balances row(s) after a clean purge")

    from app.services.orphan_sweep import sweep_orphans
    purged = sweep_orphans(db.engine)
    stock = {k: v for k, v in (purged or {}).items()
             if k.startswith("stock_")}
    assert not stock, (
        f"the boot sweep still had to clean inventory rows: {stock}")
    return (f"preflight() deleted 0 rows; boot sweep report: "
            f"{purged or '{}'}")


@check("5. the sweep checks the warehouse side, which it never did")
def _():
    from app.services.orphan_sweep import ORPHAN_QUERIES
    sql = " ".join(q for _label, q in ORPHAN_QUERIES)
    assert "stock_balances WHERE warehouse_id NOT IN" in sql, (
        "the sweep still only checks variant_id — a balance whose "
        "warehouse was deleted stays invisible")
    teardown = (ROOT / "tests/_teardown.py").read_text(encoding="utf-8")
    assert "stock_balances WHERE warehouse_id NOT IN" in teardown, \
        "tests/_teardown.py has the same blind spot"
    return "both sweeps check variant_id AND warehouse_id"


@check("6. the purge removes every inventory row, not just the balances")
def _():
    cid = _STATE["ids"][0]
    assert _STATE.get("purged"), "check 3 must run first"
    leftovers = {}
    for tbl in ("stock_balances", "stock_movements", "stock_lots",
                "product_variants", "products", "warehouses"):
        try:
            n = _count(f"SELECT COUNT(*) FROM {tbl} WHERE company_id=:c",
                       c=cid)
        except Exception:
            db.session.rollback()
            continue
        if n:
            leftovers[tbl] = n
    assert not leftovers, f"rows survived the purge: {leftovers}"
    return "no inventory rows left for the deleted company"



def _neutralise_session_cookie_domain(app):
    """A domain-scoped session cookie is never sent to the test client.

    Copied from tests/audit_portal_403.py (MARSOUD-SESSION-COOKIE-DEV-FIX).
    A production-style .env sets SESSION_COOKIE_DOMAIN=.marsoud.com, which
    scopes the cookie to that domain while the test client runs on
    localhost — so the cookie is never sent back, every request answers
    as anonymous, and @login_required bounces it to /login. The run then
    reports 302s and 500s that read as real failures when in fact no
    fixture session ever existed.

    It is irrelevant to what these audits exercise, so neutralise it for
    the run rather than depend on which .env is on the machine.
    """
    domain = app.config.get("SESSION_COOKIE_DOMAIN")
    if domain:
        app.config["SESSION_COOKIE_DOMAIN"] = None
        print(f"NOTE  SESSION_COOKIE_DOMAIN={domain!r} overridden to None "
              f"for this run -- a domain-scoped cookie is never sent "
              f"to the localhost test client.")

def main():
    app = create_app()
    _neutralise_session_cookie_domain(app)
    _STATE["app"] = app
    with app.app_context():
        _teardown()
        _STATE["ids"] = _build_company()
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
        print("\n(cleaned up)")
    print(f"\n────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
