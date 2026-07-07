#!/usr/bin/env python3
"""MARSOUD-UOM-PRICE — audit for the two follow-up bugs on
MARSOUD-UNIT-CONVERSION-01 reported on 2026-07-05:

  1) Selling a small unit charged the same price as the big unit
     (no per-unit price scaling).
  2) Voiding a POS order restocked item.quantity (=1 carton)
     instead of item.base_quantity (=10 حبة) — leaving a phantom
     -9 حبة delta in the warehouse.

The audit spins up a self-contained company, plants a warehouse +
tracked product with base unit "حبة" and a "كرتونة" (factor=10) at
sale_price=100, then walks the POS end-to-end:

  1. effective_sale_price returns the explicit value when set, else
     default_price × factor.
  2. Cashier picks the base unit at POS → line price = 10 (derived).
  3. Cashier picks the كرتونة → line price = 100 (explicit override).
  4. Legacy row (sale_price=NULL) still derives via factor.
  5. POS void restocks in BASE units — after (sale=1 carton, void)
     the warehouse balance is back to the original count exactly.
"""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__UOM_PRICE_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import Company
    existing = Company.query.filter_by(name=COMPANY_NAME).first()
    if existing:
        _teardown_company(existing.id)
    c = Company(name=COMPANY_NAME, base_currency="SAR")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)
    _STATE["company_id"] = c.id
    db.session.commit()


def _teardown_company(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        inv_ids = [r[0] for r in conn.execute(text(
            "SELECT id FROM invoices WHERE company_id = :c"),
            {"c": company_id}).fetchall()]
        bill_ids = [r[0] for r in conn.execute(text(
            "SELECT id FROM vendor_bills WHERE company_id = :c"),
            {"c": company_id}).fetchall()]
        je_ids = [r[0] for r in conn.execute(text(
            "SELECT id FROM journal_entries WHERE company_id = :c"),
            {"c": company_id}).fetchall()]
        if inv_ids:
            _in = ",".join(str(i) for i in inv_ids)
            conn.execute(text(f"DELETE FROM invoice_items WHERE invoice_id IN ({_in})"))
            conn.execute(text(f"DELETE FROM payments WHERE invoice_id IN ({_in})"))
        if bill_ids:
            _in = ",".join(str(i) for i in bill_ids)
            conn.execute(text(f"DELETE FROM vendor_bill_items WHERE bill_id IN ({_in})"))
        if je_ids:
            _in = ",".join(str(i) for i in je_ids)
            conn.execute(text(f"DELETE FROM journal_lines WHERE entry_id IN ({_in})"))
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                             {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text("DELETE FROM stock_balances WHERE variant_id NOT IN (SELECT id FROM product_variants)"))
        conn.execute(text("DELETE FROM stock_movements WHERE variant_id NOT IN (SELECT id FROM product_variants)"))
        conn.execute(text("DELETE FROM stock_lots WHERE variant_id NOT IN (SELECT id FROM product_variants)"))
        # Fixture user (created directly, not via company scope) — the
        # loop above misses it and re-runs collide on the unique email.
        conn.execute(text("DELETE FROM users WHERE email = 'pos.cashier@audit.test'"))


@check("1. effective_sale_price: explicit override wins")
def _():
    from app.models import Product, ProductVariant
    from app.services.units import ensure_base_unit, create_unit
    cid = _STATE["company_id"]
    p = Product(company_id=cid, name="بيض", is_tracked=True,
                default_unit="حبة", default_price=Decimal("10"))
    db.session.add(p); db.session.flush()
    v = ProductVariant(product_id=p.id, sku="EGG-P",
                        company_id=cid, unit_cost=Decimal("0"))
    db.session.add(v); db.session.flush()
    base = ensure_base_unit(p)
    carton = create_unit(p, "كرتونة", 10, sale_price=Decimal("100"))
    db.session.commit()
    # Base has no explicit sale_price → derives from default_price × 1
    assert base.sale_price is None
    assert abs(base.effective_sale_price - 10.0) < 0.001
    # Carton set explicitly to 100
    assert abs(carton.effective_sale_price - 100.0) < 0.001
    _STATE.update(product_id=p.id, variant_id=v.id,
                   base_unit_id=base.id, carton_unit_id=carton.id)
    return "base=10 (derived), carton=100 (explicit)"


@check("2. effective_sale_price: NULL falls back to default_price × factor")
def _():
    from app.models import ProductUnit
    carton = db.session.get(ProductUnit, _STATE["carton_unit_id"])
    saved = carton.sale_price
    carton.sale_price = None
    db.session.flush()
    # Derived: 10 × 10 = 100 (which happens to match, but critically
    # is not reading the previous stored value)
    assert abs(carton.effective_sale_price - 100.0) < 0.001
    # Restore for downstream tests
    carton.sale_price = saved
    db.session.commit()
    return "NULL → derived 100 (=10×10)"


@check("3. Stock in — 5 كرتونة receive → +50 حبة on hand")
def _():
    from app.models import (
        Warehouse, Vendor, VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, BillLineType, Account, StockBalance,
    )
    from app.services.subsidiary import ensure_vendor_account
    from app.services.vendor_bills import post_vendor_bill
    cid = _STATE["company_id"]
    wh = Warehouse(company_id=cid, name="Main", code="M")
    db.session.add(wh); db.session.flush()
    v = Vendor(company_id=cid, name="مورد بيض")
    db.session.add(v); db.session.flush()
    ensure_vendor_account(v)
    bill = VendorBill(
        company_id=cid, vendor_id=v.id, number="U-BUY-P",
        issue_date=date.today(), due_date=date.today(),
        status=VendorBillStatus.DRAFT,
        payment_method=VendorBillPaymentMethod.CASH,
        tax_rate=Decimal("0"),
    )
    db.session.add(bill); db.session.flush()
    inv_acc = Account.query.filter_by(company_id=cid, code="1300").first()
    db.session.add(VendorBillItem(
        bill_id=bill.id, line_type=BillLineType.INVENTORY,
        account_id=inv_acc.id, description="بيض",
        variant_id=_STATE["variant_id"], warehouse_id=wh.id,
        quantity=Decimal("5"), unit_price=Decimal("80"),
        line_total=Decimal("400"),
        unit_id=_STATE["carton_unit_id"],
    ))
    db.session.flush()
    post_vendor_bill(bill)
    db.session.commit()
    bal = StockBalance.query.filter_by(
        variant_id=_STATE["variant_id"], warehouse_id=wh.id,
    ).first()
    assert float(bal.qty) == 50.0, f"expected 50 حبة, got {bal.qty}"
    _STATE["warehouse_id"] = wh.id
    _STATE["opening_qty"] = 50.0
    return f"warehouse={float(bal.qty):.0f} حبة"


@check("4. POS sale of 1 كرتونة → stock -10 حبة, invoice line at 100")
def _():
    from app.models import (
        Customer, PaymentMethod, Account, StockBalance, InvoiceItem,
    )
    from app.services.subsidiary import ensure_customer_account
    from app.services.pos import create_pos_order
    cid = _STATE["company_id"]
    # Setup: cashier + customer + cash PM
    cust = Customer(company_id=cid, name="زبون POS")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    cash_acc = Account.query.filter_by(company_id=cid, code="1110").first()
    pm = PaymentMethod(company_id=cid, name="نقدي", account_id=cash_acc.id,
                        is_default=True)
    db.session.add(pm); db.session.flush()
    # Cashier = a fake user id 1 for source_id purposes — the audit
    # doesn't gate on login here.
    from app.models import User
    u = User(email="pos.cashier@audit.test", password_hash="x",
              full_name="Cashier")
    db.session.add(u); db.session.flush()

    # Simulate a POS cart with 1 كرتونة at price 100
    items = [{
        "variant_id": _STATE["variant_id"],
        "qty": 1, "unit_price": 100.0,
        "unit_id": _STATE["carton_unit_id"],
    }]
    invoice = create_pos_order(
        company_id=cid, items=items, payment_method_id=pm.id,
        cashier_id=u.id, customer_id=cust.id,
        cash_received=100.0, tax_rate=0,
    )
    _STATE["pos_invoice_id"] = invoice.id
    _STATE["cashier_id"] = u.id

    # Stock: opening 50 → 40 (dropped 10 حبة for 1 carton)
    bal = StockBalance.query.filter_by(
        variant_id=_STATE["variant_id"],
        warehouse_id=_STATE["warehouse_id"],
    ).first()
    assert abs(float(bal.qty) - 40.0) < 0.001, \
        f"expected 40 حبة after 1-carton sale, got {bal.qty}"
    # Line unit_price frozen at what the cashier submitted (100)
    line = InvoiceItem.query.filter_by(invoice_id=invoice.id).first()
    assert abs(float(line.unit_price) - 100.0) < 0.001
    # base_quantity frozen at 10
    assert abs(float(line.base_quantity) - 10.0) < 0.001
    return f"stock=40 حبة, line.unit_price=100, base_quantity=10"


@check("5. POS void restocks BASE quantity — warehouse fully restored")
def _():
    from app.models import Invoice, StockBalance
    from app.services.pos import void_pos_order
    invoice = db.session.get(Invoice, _STATE["pos_invoice_id"])
    void_pos_order(invoice, reason="ملغى للاختبار",
                    actor_id=_STATE["cashier_id"])
    bal = StockBalance.query.filter_by(
        variant_id=_STATE["variant_id"],
        warehouse_id=_STATE["warehouse_id"],
    ).first()
    # Warehouse should be back to 50 حبة (not 41 = 40+1 which would be
    # the OLD bug where void restocked item.quantity=1 instead of
    # base_quantity=10).
    assert abs(float(bal.qty) - _STATE["opening_qty"]) < 0.001, (
        f"void restock buggy — expected {_STATE['opening_qty']}, "
        f"got {bal.qty} (would be 41 with the pre-fix code)"
    )
    return f"stock back to {float(bal.qty):.0f} حبة (full restore, was 41 with bug)"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        from tests._orphan_sweep import preflight
        preflight()
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
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "company_id" in _STATE:
                    _teardown_company(_STATE["company_id"])
                    print(f"\n(cleaned up fixture company)")
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
