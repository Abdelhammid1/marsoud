#!/usr/bin/env python3
"""MARSOUD-MANUFACTURING-01 — end-to-end audit.

Proves, on a fresh company:
  1. BOM created for a finished good with 2 raw materials at different
     unit costs.
  2. Insufficient stock → post_work_order_completion refuses BEFORE
     mutating anything (stock balances unchanged).
  3. Enough stock → completion succeeds:
     - PRODUCTION_ISSUE stock movements for each component (negative
       qty, weighted-average cost snapshot).
     - PRODUCTION_RECEIPT for the finished good.
     - Balanced journal touching 1300 (Dr + Cr), 5120, 5130, and
       possibly 5140 for rounding.
  4. WorkOrderConsumption rows written with unit_cost_at_time matching
     the stock movement's snapshot.
  5. Work order status = COMPLETED, journal_entry_id populated.
  6. Second attempt on a completed order is refused.
  7. plan gating: manufacturing.view maps to `manufacturing` module.
  8. Permissions catalog + DB rows for all 3 codes present.
"""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__MFG_AUDIT__"
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
    from app.models import (
        Company, JournalEntry, JournalLine, Invoice, InvoiceItem,
        Payment, VendorBill, VendorBillItem,
    )
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    entry_ids = [r.id for r in JournalEntry.query.filter_by(
        company_id=company_id).all()]
    if entry_ids:
        JournalLine.query.filter(
            JournalLine.entry_id.in_(entry_ids),
        ).delete(synchronize_session=False)
    inv_ids = [r.id for r in Invoice.query.filter_by(
        company_id=company_id).all()]
    if inv_ids:
        InvoiceItem.query.filter(
            InvoiceItem.invoice_id.in_(inv_ids),
        ).delete(synchronize_session=False)
        Payment.query.filter(
            Payment.invoice_id.in_(inv_ids),
        ).delete(synchronize_session=False)
    bill_ids = [r.id for r in VendorBill.query.filter_by(
        company_id=company_id).all()]
    if bill_ids:
        VendorBillItem.query.filter(
            VendorBillItem.bill_id.in_(bill_ids),
        ).delete(synchronize_session=False)
    for table in reversed(db.metadata.sorted_tables):
        if "company_id" in {col["name"] for col in insp.get_columns(table.name)}:
            db.session.execute(
                table.delete().where(table.c.company_id == company_id),
            )
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()


def _make_variant(sku):
    from app.models import Product, ProductVariant
    cid = _STATE["company_id"]
    p = Product(company_id=cid, name=sku, is_tracked=True,
                  default_price=Decimal("0"))
    db.session.add(p); db.session.flush()
    v = ProductVariant(product_id=p.id, sku=sku, company_id=cid,
                          unit_cost=Decimal("0"))
    db.session.add(v); db.session.flush()
    return v


def _stock_in(variant, warehouse, qty, unit_cost):
    from app.services.inventory import receive_stock
    receive_stock(
        variant=variant, warehouse=warehouse,
        qty=qty, unit_cost=unit_cost,
    )


@check("1. BOM created with 2 components")
def _():
    from app.models import Warehouse, BillOfMaterial, BOMLine
    cid = _STATE["company_id"]
    wh = Warehouse(company_id=cid, name="مخزن رئيسي", code="M")
    db.session.add(wh); db.session.flush()
    _STATE["warehouse_id"] = wh.id

    finished = _make_variant("FG-01")
    raw_a = _make_variant("RAW-A")
    raw_b = _make_variant("RAW-B")
    _STATE.update(finished_id=finished.id,
                    raw_a_id=raw_a.id, raw_b_id=raw_b.id)

    bom = BillOfMaterial(
        company_id=cid, name="تركيبة الاختبار",
        product_variant_id=finished.id,
    )
    db.session.add(bom); db.session.flush()
    db.session.add(BOMLine(bom_id=bom.id,
                              component_variant_id=raw_a.id,
                              qty_per_unit=Decimal("2")))
    db.session.add(BOMLine(bom_id=bom.id,
                              component_variant_id=raw_b.id,
                              qty_per_unit=Decimal("1")))
    db.session.commit()
    _STATE["bom_id"] = bom.id
    return f"BOM #{bom.id}: FG-01 = 2×RAW-A + 1×RAW-B"


@check("2. Insufficient stock → completion refused, balances untouched")
def _():
    from app.models import (
        Warehouse, WorkOrder, WorkOrderStatus, StockBalance,
        ProductVariant,
    )
    from app.services.manufacturing import (
        post_work_order_completion, ManufacturingError,
    )
    from app.services.numbering import next_number
    cid = _STATE["company_id"]
    wh = db.session.get(Warehouse, _STATE["warehouse_id"])
    # Try to produce 10 finished — needs 20 RAW-A + 10 RAW-B. Balance
    # is currently 0 for everything.
    wo = WorkOrder(
        company_id=cid, number=next_number(cid, "MANUFACTURING_ORDER"),
        bom_id=_STATE["bom_id"], warehouse_id=wh.id,
        quantity_to_produce=Decimal("10"),
        status=WorkOrderStatus.DRAFT,
    )
    db.session.add(wo); db.session.commit()
    raised = False
    try:
        post_work_order_completion(wo)
    except ManufacturingError as e:
        raised = True
        msg = str(e)
    db.session.rollback()
    assert raised, "expected ManufacturingError on empty stock"
    assert "كافية" in msg
    # Balances still zero
    for vid in (_STATE["raw_a_id"], _STATE["raw_b_id"]):
        bal = StockBalance.query.filter_by(
            variant_id=vid, warehouse_id=wh.id,
        ).first()
        assert bal is None or float(bal.qty) == 0
    _STATE["insufficient_wo_id"] = wo.id
    return f"refused: {msg}"


@check("3. Enough stock → completion succeeds + weighted-avg cost")
def _():
    from app.models import (
        Warehouse, ProductVariant, WorkOrder, WorkOrderStatus,
        StockMovement, JournalEntry, JournalLine,
    )
    from app.services.manufacturing import post_work_order_completion
    from app.services.numbering import next_number
    cid = _STATE["company_id"]
    wh = db.session.get(Warehouse, _STATE["warehouse_id"])
    raw_a = db.session.get(ProductVariant, _STATE["raw_a_id"])
    raw_b = db.session.get(ProductVariant, _STATE["raw_b_id"])
    # Two receipts of RAW-A at different costs — 10 @ 5, then 10 @ 7
    # → weighted avg = 6. Enough to build 10 finished (needs 20 RAW-A).
    _stock_in(raw_a, wh, qty=10, unit_cost=5)
    _stock_in(raw_a, wh, qty=10, unit_cost=7)
    _stock_in(raw_b, wh, qty=10, unit_cost=3)
    db.session.commit()

    wo = WorkOrder(
        company_id=cid, number=next_number(cid, "MANUFACTURING_ORDER"),
        bom_id=_STATE["bom_id"], warehouse_id=wh.id,
        quantity_to_produce=Decimal("10"),
        direct_labor_cost=Decimal("100"),
        overhead_cost=Decimal("50"),
        status=WorkOrderStatus.DRAFT,
    )
    db.session.add(wo); db.session.commit()
    post_work_order_completion(wo)
    db.session.refresh(wo)

    assert wo.status == WorkOrderStatus.COMPLETED
    assert wo.journal_entry_id is not None
    # Material: 20×6 + 10×3 = 150. Absorbed = 150 + 100 + 50 = 300.
    # Finished unit cost = 30.

    # Stock movements
    prod_issues = StockMovement.query.filter_by(
        source_type="work_order_consumption", source_id=wo.id,
        kind="PRODUCTION_ISSUE",
    ).all()
    assert len(prod_issues) == 2, f"expected 2 issues, got {len(prod_issues)}"
    prod_receipt = StockMovement.query.filter_by(
        source_type="work_order_receipt", source_id=wo.id,
        kind="PRODUCTION_RECEIPT",
    ).first()
    assert prod_receipt is not None
    assert float(prod_receipt.qty_delta) == 10.0
    assert abs(float(prod_receipt.unit_cost_at_time) - 30.0) < 0.01, \
        f"finished unit cost should be 30, got {prod_receipt.unit_cost_at_time}"

    # Journal balanced + touches 1300 twice, 5120, 5130
    lines = JournalLine.query.filter_by(entry_id=wo.journal_entry_id).all()
    total_dr = sum(float(l.debit or 0) for l in lines)
    total_cr = sum(float(l.credit or 0) for l in lines)
    assert abs(total_dr - total_cr) < 0.01, \
        f"unbalanced: dr={total_dr} cr={total_cr}"
    codes = {l.account.code for l in lines}
    assert "1300" in codes
    assert "5120" in codes
    assert "5130" in codes
    _STATE["success_wo_id"] = wo.id
    return (f"WO {wo.number} completed; finished unit cost=30, "
              f"journal balanced dr={total_dr:.2f}=cr={total_cr:.2f}")


@check("4. WorkOrderConsumption unit_cost matches stock movement snapshot")
def _():
    from app.models import (
        WorkOrder, StockMovement, WorkOrderConsumption,
    )
    wo = db.session.get(WorkOrder, _STATE["success_wo_id"])
    for c in wo.consumption:
        mv = StockMovement.query.filter_by(
            source_type="work_order_consumption", source_id=wo.id,
            variant_id=c.component_variant_id,
        ).first()
        assert mv, f"missing movement for {c.component_variant_id}"
        assert abs(float(c.unit_cost_at_time)
                    - float(mv.unit_cost_at_time)) < 0.001, \
            f"consumption cost mismatch"
    return "all consumption rows match stock movement snapshot"


@check("5. Second complete on a done order is refused")
def _():
    from app.models import WorkOrder
    from app.services.manufacturing import (
        post_work_order_completion, ManufacturingError,
    )
    wo = db.session.get(WorkOrder, _STATE["success_wo_id"])
    raised = False
    try:
        post_work_order_completion(wo)
    except ManufacturingError as e:
        raised = True
        msg = str(e)
    db.session.rollback()
    assert raised
    return f"refused: {msg}"


@check("6. plan_gating maps manufacturing to its own module")
def _():
    from app.services.plan_gating import action_module, SUB_ITEM_CATALOG
    assert action_module("manufacturing.view") == "manufacturing"
    assert action_module("manufacturing.manage") == "manufacturing"
    assert action_module("manufacturing.complete") == "manufacturing"
    assert "manufacturing" in SUB_ITEM_CATALOG
    return "module + section wired"


@check("7. permission catalog + DB rows for all 3 codes")
def _():
    from app.services.roles_seed import (
        PERMISSION_CATALOG, seed_permissions_catalog,
    )
    from app.models import Permission
    seed_permissions_catalog()
    for code in ("manufacturing.view", "manufacturing.manage",
                   "manufacturing.complete"):
        assert code in PERMISSION_CATALOG, f"{code} missing from catalog"
        assert Permission.query.filter_by(code=code).first(), \
            f"{code} missing from DB"
    return "all 3 codes present in catalog + DB"


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
                    print(f"\n(cleaned up fixture company "
                          f"#{_STATE['company_id']})")
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
