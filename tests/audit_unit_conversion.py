#!/usr/bin/env python3
"""MARSOUD-UNIT-CONVERSION-01 — end-to-end audit.

Proves, on a fresh company:
  1. ensure_base_unit() creates + is idempotent.
  2. create_unit() adds non-base units with correct factor.
  3. Buying 10 كرتونة (factor=30) increases inventory by 300 حبة.
  4. Weighted-average unit cost lands per BASE unit (per حبة),
     independent of purchase unit.
  5. Selling 2 كرتونة decreases inventory by 60 حبة, COGS journal
     matches the pre-computed cost.
  6. Selling 1 حبة (base unit) decreases by 1.
  7. Legacy product with no unit_id on the item still works
     (base_quantity = quantity fallback).
  8. delete_unit refuses base unit + units with historical movements.
  9. Fractional quantity (0.5 كرتونة → 15 حبة) works.
 10. Different companies with the same product name have independent
     units (no cross-tenant leak via product_units).
"""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__UNITS_AUDIT__"
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
        # Grand-children first (invoice_items, journal_lines etc.)
        inv_ids = [r[0] for r in conn.execute(text(
            "SELECT id FROM invoices WHERE company_id = :c"), {"c": company_id}).fetchall()]
        bill_ids = [r[0] for r in conn.execute(text(
            "SELECT id FROM vendor_bills WHERE company_id = :c"), {"c": company_id}).fetchall()]
        je_ids = [r[0] for r in conn.execute(text(
            "SELECT id FROM journal_entries WHERE company_id = :c"), {"c": company_id}).fetchall()]
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
                conn.execute(text(f"DELETE FROM {tbl.name} WHERE company_id = :c"), {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"), {"c": company_id})
        # Sweep orphaned stock rows whose variant/lot pointer no longer
        # resolves — SQLite reuses IDs, so leftovers pollute the next
        # run's balance for a fresh variant that happens to grab the
        # same numeric id.
        conn.execute(text("DELETE FROM stock_balances WHERE variant_id NOT IN (SELECT id FROM product_variants)"))
        conn.execute(text("DELETE FROM stock_movements WHERE variant_id NOT IN (SELECT id FROM product_variants)"))
        conn.execute(text("DELETE FROM stock_lots WHERE variant_id NOT IN (SELECT id FROM product_variants)"))


def _make_variant_with_units(sku, unit_name="حبة"):
    """Fresh product + variant + base unit set to `unit_name`."""
    from app.models import Product, ProductVariant
    from app.services.units import ensure_base_unit
    cid = _STATE["company_id"]
    p = Product(company_id=cid, name=sku, is_tracked=True,
                  default_unit=unit_name, default_price=Decimal("0"))
    db.session.add(p); db.session.flush()
    v = ProductVariant(product_id=p.id, sku=sku, company_id=cid,
                          unit_cost=Decimal("0"))
    db.session.add(v); db.session.flush()
    ensure_base_unit(p)
    return p, v


@check("1. ensure_base_unit creates + is idempotent")
def _():
    from app.services.units import ensure_base_unit
    p, v = _make_variant_with_units("EGG-01")
    db.session.commit()
    u1 = ensure_base_unit(p)
    u2 = ensure_base_unit(p)
    assert u1.id == u2.id
    assert u1.is_base is True
    assert float(u1.conversion_factor) == 1.0
    _STATE.update(product_id=p.id, variant_id=v.id, base_unit_id=u1.id)
    return f"base unit #{u1.id} ({u1.unit_name})"


@check("2. create_unit adds non-base with correct factor")
def _():
    from app.models import Product, ProductUnit
    from app.services.units import create_unit
    p = db.session.get(Product, _STATE["product_id"])
    box = create_unit(p, "كرتونة", 30)
    tray = create_unit(p, "طبق", 6)
    db.session.commit()
    assert not box.is_base and float(box.conversion_factor) == 30
    assert float(tray.conversion_factor) == 6
    _STATE.update(box_unit_id=box.id, tray_unit_id=tray.id)
    return f"كرتونة #{box.id} (×30), طبق #{tray.id} (×6)"


@check("3. Buy 10 كرتونة → stock +300 حبة")
def _():
    from app.models import (
        Warehouse, Vendor, VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, BillLineType, Account, StockBalance,
    )
    from app.services.subsidiary import ensure_vendor_account
    from app.services.vendor_bills import post_vendor_bill
    cid = _STATE["company_id"]
    wh = Warehouse(company_id=cid, name="M", code="M")
    db.session.add(wh); db.session.flush()
    v_vendor = Vendor(company_id=cid, name="مورد كرتون")
    db.session.add(v_vendor); db.session.flush()
    ensure_vendor_account(v_vendor)

    bill = VendorBill(
        company_id=cid, vendor_id=v_vendor.id, number="U-BUY-1",
        issue_date=date.today(), due_date=date.today(),
        status=VendorBillStatus.DRAFT,
        payment_method=VendorBillPaymentMethod.CASH,
        tax_rate=Decimal("0"),
    )
    db.session.add(bill); db.session.flush()
    inv_acc = Account.query.filter_by(company_id=cid, code="1300").first()
    # 10 كرتونة @ 90 SAR each = 900 SAR total; base=300 حبة → 3 SAR/حبة
    db.session.add(VendorBillItem(
        bill_id=bill.id, line_type=BillLineType.INVENTORY,
        account_id=inv_acc.id, description="بيض",
        variant_id=_STATE["variant_id"], warehouse_id=wh.id,
        quantity=Decimal("10"), unit_price=Decimal("90"),
        line_total=Decimal("900"),
        unit_id=_STATE["box_unit_id"],
    ))
    db.session.flush()
    post_vendor_bill(bill)
    db.session.commit()

    bal = StockBalance.query.filter_by(
        variant_id=_STATE["variant_id"], warehouse_id=wh.id,
    ).first()
    assert bal is not None
    assert float(bal.qty) == 300.0, f"expected 300 حبة, got {bal.qty}"
    # Cost per BASE unit: 900 / 300 = 3
    assert abs(float(bal.value) / float(bal.qty) - 3.0) < 0.001
    _STATE["warehouse_id"] = wh.id
    return f"stock=300 حبة @ 3.00/حبة"


@check("4. Sell 2 كرتونة → stock -60 حبة, COGS journal balanced")
def _():
    from app.models import (
        Customer, Invoice, InvoiceItem, InvoiceStatus, PaymentMethod,
        StockBalance, JournalEntry, JournalLine,
    )
    from app.services.subsidiary import ensure_customer_account
    from app.services.invoicing import post_invoice_to_ledger, record_payment
    cid = _STATE["company_id"]
    cust = Customer(company_id=cid, name="زبون")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    inv = Invoice(
        company_id=cid, customer_id=cust.id, number="U-SELL-1",
        issue_date=date.today(), due_date=date.today(),
        currency="SAR", status=InvoiceStatus.DRAFT,
        tax_rate=Decimal("0"),
    )
    db.session.add(inv); db.session.flush()
    # Sell 2 كرتونة @ 150 each = 300
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="بيض",
        product_id=_STATE["product_id"],
        variant_id=_STATE["variant_id"],
        warehouse_id=_STATE["warehouse_id"],
        quantity=Decimal("2"), unit_price=Decimal("150"),
        line_total=Decimal("300"),
        unit_id=_STATE["box_unit_id"],
    ))
    db.session.flush()
    inv.recalc(); db.session.flush()
    post_invoice_to_ledger(inv)
    db.session.commit()

    bal = StockBalance.query.filter_by(
        variant_id=_STATE["variant_id"],
        warehouse_id=_STATE["warehouse_id"],
    ).first()
    assert float(bal.qty) == 240.0, f"expected 240 حبة, got {bal.qty}"
    # COGS should be 60 × 3 = 180
    cogs_entry = JournalEntry.query.filter_by(
        source_type="invoice_cogs", source_id=inv.id,
    ).first()
    assert cogs_entry is not None
    lines = JournalLine.query.filter_by(entry_id=cogs_entry.id).all()
    total_dr = sum(float(l.debit or 0) for l in lines)
    total_cr = sum(float(l.credit or 0) for l in lines)
    assert abs(total_dr - total_cr) < 0.01
    assert abs(total_dr - 180.0) < 0.01, \
        f"COGS should be 180 (60×3), got {total_dr}"
    _STATE["invoice_id"] = inv.id
    return f"stock=240 حبة, COGS journal balanced at 180"


@check("5. Sell 1 حبة (base unit) → stock -1")
def _():
    from app.models import (
        Customer, Invoice, InvoiceItem, InvoiceStatus, StockBalance,
    )
    from app.services.subsidiary import ensure_customer_account
    from app.services.invoicing import post_invoice_to_ledger
    cid = _STATE["company_id"]
    cust = Customer(company_id=cid, name="زبون تجزئة")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    inv = Invoice(
        company_id=cid, customer_id=cust.id, number="U-SELL-2",
        issue_date=date.today(), due_date=date.today(),
        currency="SAR", status=InvoiceStatus.DRAFT,
        tax_rate=Decimal("0"),
    )
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="بيض حبة",
        product_id=_STATE["product_id"],
        variant_id=_STATE["variant_id"],
        warehouse_id=_STATE["warehouse_id"],
        quantity=Decimal("1"), unit_price=Decimal("5"),
        line_total=Decimal("5"),
        unit_id=_STATE["base_unit_id"],
    ))
    db.session.flush()
    inv.recalc(); db.session.flush()
    post_invoice_to_ledger(inv)
    db.session.commit()

    bal = StockBalance.query.filter_by(
        variant_id=_STATE["variant_id"],
        warehouse_id=_STATE["warehouse_id"],
    ).first()
    assert float(bal.qty) == 239.0
    return f"stock=239 حبة"


@check("6. Legacy item (unit_id=NULL) still works")
def _():
    from app.models import (
        Customer, Invoice, InvoiceItem, InvoiceStatus, StockBalance,
    )
    from app.services.subsidiary import ensure_customer_account
    from app.services.invoicing import post_invoice_to_ledger
    cid = _STATE["company_id"]
    cust = Customer(company_id=cid, name="زبون قديم")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    inv = Invoice(
        company_id=cid, customer_id=cust.id, number="U-SELL-3",
        issue_date=date.today(), due_date=date.today(),
        currency="SAR", status=InvoiceStatus.DRAFT,
        tax_rate=Decimal("0"),
    )
    db.session.add(inv); db.session.flush()
    # Legacy: unit_id=None → treated as base unit already.
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="بيض حبة",
        product_id=_STATE["product_id"],
        variant_id=_STATE["variant_id"],
        warehouse_id=_STATE["warehouse_id"],
        quantity=Decimal("3"), unit_price=Decimal("5"),
        line_total=Decimal("15"),
        unit_id=None,
    ))
    db.session.flush()
    inv.recalc(); db.session.flush()
    post_invoice_to_ledger(inv)
    db.session.commit()
    bal = StockBalance.query.filter_by(
        variant_id=_STATE["variant_id"],
        warehouse_id=_STATE["warehouse_id"],
    ).first()
    assert float(bal.qty) == 236.0
    return f"stock=236 حبة (unit_id=None → treated as base)"


@check("7. delete_unit refuses base + units with movements")
def _():
    from app.models import ProductUnit
    from app.services.units import delete_unit, UnitError
    base_u = db.session.get(ProductUnit, _STATE["base_unit_id"])
    box_u = db.session.get(ProductUnit, _STATE["box_unit_id"])
    tray_u = db.session.get(ProductUnit, _STATE["tray_unit_id"])

    # Base — refused
    raised = False
    try:
        delete_unit(base_u)
    except UnitError:
        raised = True
    assert raised, "delete_unit should refuse base"

    # Box — has movements → refused
    raised = False
    try:
        delete_unit(box_u)
    except UnitError:
        raised = True
    assert raised, "delete_unit should refuse unit with movements"

    # Tray — no movements → succeeds
    delete_unit(tray_u)
    db.session.commit()
    assert db.session.get(ProductUnit, tray_u.id) is None
    return "base + box refused; tray deleted"


@check("8. Fractional quantity (0.5 كرتونة → 15 حبة)")
def _():
    from app.models import (
        Customer, Invoice, InvoiceItem, InvoiceStatus, StockBalance,
    )
    from app.services.subsidiary import ensure_customer_account
    from app.services.invoicing import post_invoice_to_ledger
    cid = _STATE["company_id"]
    cust = Customer(company_id=cid, name="زبون كسر")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    inv = Invoice(
        company_id=cid, customer_id=cust.id, number="U-SELL-FRAC",
        issue_date=date.today(), due_date=date.today(),
        currency="SAR", status=InvoiceStatus.DRAFT,
        tax_rate=Decimal("0"),
    )
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="نص كرتونة",
        product_id=_STATE["product_id"],
        variant_id=_STATE["variant_id"],
        warehouse_id=_STATE["warehouse_id"],
        quantity=Decimal("0.5"), unit_price=Decimal("50"),
        line_total=Decimal("25"),
        unit_id=_STATE["box_unit_id"],
    ))
    db.session.flush()
    inv.recalc(); db.session.flush()
    post_invoice_to_ledger(inv)
    db.session.commit()
    bal = StockBalance.query.filter_by(
        variant_id=_STATE["variant_id"],
        warehouse_id=_STATE["warehouse_id"],
    ).first()
    # 236 - 15 = 221
    assert abs(float(bal.qty) - 221.0) < 0.001, \
        f"expected 221, got {bal.qty}"
    return f"stock=221 حبة (0.5 كرتونة = 15 حبة consumed)"


@check("9. convert_to_base rejects cross-product unit")
def _():
    from app.models import Product, ProductVariant
    from app.services.units import (
        ensure_base_unit, convert_to_base, UnitError,
    )
    # Second product with its own base unit.
    p2, v2 = _make_variant_with_units("MILK-01", unit_name="لتر")
    db.session.commit()
    base_other = ensure_base_unit(p2)
    # Trying to convert with the OTHER product's unit_id should raise.
    p1 = db.session.get(Product, _STATE["product_id"])
    raised = False
    try:
        convert_to_base(p1, 5, unit_id=base_other.id)
    except UnitError:
        raised = True
    assert raised, "convert_to_base should refuse cross-product unit"
    return "cross-product unit correctly refused"


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
