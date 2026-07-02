"""MARSOUD-MANUFACTURING-01 — post a work order.

The single interesting entry point is `post_work_order_completion()`,
which is what fires when the operator clicks "إكمال" on the work-order
detail page. Everything else is CRUD wrapping.

Accounting model
================
When a work order completes we:

  1. Draw each BOM component from the chosen warehouse at the current
     weighted-average cost (same math SALE uses).
  2. Sum up (material cost) + labor + overhead → "cost of finished good".
  3. Post 1 balanced journal:

        Dr 1300 Inventory (finished good, at full absorbed cost)
              Cr 1300 Inventory (raw materials, at consumed cost)
              Cr 5120 Direct Labor        (if labor cost given)
              Cr 5130 Manufacturing Overhead (if overhead given)

     A finished-good cost that doesn't tie out to component-cost +
     labor + overhead means a variance — we route the delta to
     5140 Manufacturing Variances so both sides balance to the cent.

  4. Record PRODUCTION_ISSUE stock movements (one per component) +
     one PRODUCTION_RECEIPT for the finished good, all linked to the
     work order + the journal.

Everything happens inside a single db.session commit so a mid-way
failure rolls back cleanly — no half-completed work orders.
"""
from datetime import datetime, date
from app import db
from app.models import (
    BillOfMaterial, BOMLine, WorkOrder, WorkOrderStatus,
    WorkOrderConsumption, ProductVariant,
)
from app.models.inventory import StockMovementKind
from app.services.ledger import post_journal, get_account_by_code, LedgerError
from app.services.inventory import (
    apply_stock_movement, InventoryError,
)


class ManufacturingError(LedgerError):
    """Domain-specific error, still catchable as LedgerError so the
    route layer doesn't need to know about it separately."""
    pass


def post_work_order_completion(work_order, created_by=None):
    """Complete `work_order`, posting all stock + ledger side effects.

    Guarantees:
      - The order must be in DRAFT or IN_PROGRESS. COMPLETED / CANCELLED
        raises.
      - Every component must have enough stock in the chosen warehouse.
        A shortfall aborts BEFORE any partial post.
      - The resulting journal is balanced (variance line auto-added).
      - PRODUCTION_ISSUE + PRODUCTION_RECEIPT rows link back to the
        journal via journal_entry_id.
    """
    if work_order.status not in (WorkOrderStatus.DRAFT,
                                    WorkOrderStatus.IN_PROGRESS):
        raise ManufacturingError(
            f"لا يمكن إكمال أمر إنتاج بحالة {work_order.status.value}"
        )
    bom = work_order.bom
    if not bom or not bom.lines:
        raise ManufacturingError("تركيبة المنتج (BOM) فارغة")

    qty_to_produce = float(work_order.quantity_to_produce or 0)
    if qty_to_produce <= 0:
        raise ManufacturingError("الكمية المطلوب إنتاجها يجب أن تكون أكبر من صفر")

    company_id = work_order.company_id
    warehouse = work_order.warehouse
    if not warehouse:
        raise ManufacturingError("لم يتم تحديد المخزن")

    # Pre-flight: verify each component has enough stock BEFORE we
    # start mutating balances. Cheaper than rolling back mid-flight.
    from app.models import StockBalance
    shortfalls = []
    for line in bom.lines:
        need = float(line.qty_per_unit) * qty_to_produce
        bal = StockBalance.query.filter_by(
            variant_id=line.component_variant_id,
            warehouse_id=warehouse.id,
        ).first()
        avail = float(bal.qty) if bal else 0.0
        if avail < need - 0.0001:
            shortfalls.append((line.component_variant.sku, avail, need))
    if shortfalls:
        msg = "؛ ".join(
            f"{sku}: متاح {a:.2f} / مطلوب {n:.2f}"
            for sku, a, n in shortfalls
        )
        raise ManufacturingError(
            f"الكميات غير كافية لإكمال الإنتاج: {msg}"
        )

    # ─── 1. Consume components (weighted-average cost snapshots) ────
    total_material_cost = 0.0
    consumption_rows = []
    for line in bom.lines:
        qty = float(line.qty_per_unit) * qty_to_produce
        try:
            mv = apply_stock_movement(
                variant=line.component_variant,
                warehouse=warehouse,
                qty_delta=-qty,
                kind=StockMovementKind.PRODUCTION_ISSUE,
                source_type="work_order_consumption",
                source_id=work_order.id,
                actor_id=created_by,
            )
        except InventoryError as e:
            raise ManufacturingError(str(e))
        unit_cost = float(mv.unit_cost_at_time or 0)
        total_material_cost += unit_cost * qty
        consumption_rows.append((line, qty, unit_cost, mv))

    labor_cost = float(work_order.direct_labor_cost or 0)
    overhead_cost = float(work_order.overhead_cost or 0)
    total_absorbed = total_material_cost + labor_cost + overhead_cost

    if total_absorbed <= 0:
        raise ManufacturingError(
            "تكلفة الإنتاج صفر — راجع تكاليف المواد والأجور والمصاريف"
        )

    # ─── 2. Receive the finished good at total_absorbed / qty ───────
    finished_variant = bom.product_variant
    if not finished_variant:
        raise ManufacturingError("المنتج التام في BOM غير موجود")

    finished_unit_cost = total_absorbed / qty_to_produce
    try:
        fg_movement = apply_stock_movement(
            variant=finished_variant,
            warehouse=warehouse,
            qty_delta=qty_to_produce,
            kind=StockMovementKind.PRODUCTION_RECEIPT,
            unit_cost=finished_unit_cost,
            source_type="work_order_receipt",
            source_id=work_order.id,
            actor_id=created_by,
        )
    except InventoryError as e:
        raise ManufacturingError(str(e))

    # ─── 3. Balanced journal ────────────────────────────────────────
    inv_account = get_account_by_code(company_id, "1300")
    labor_account = get_account_by_code(company_id, "5120")
    overhead_account = get_account_by_code(company_id, "5130")
    variance_account = get_account_by_code(company_id, "5140")
    if not (inv_account and labor_account and overhead_account
              and variance_account):
        raise ManufacturingError(
            "حسابات التصنيع (1300/5120/5130/5140) غير موجودة"
        )

    # Absorbed cost of finished good goes to Dr 1300; raw material,
    # labor, and overhead go to the credit side. Variance closes the
    # loop if any rounding drift is left.
    lines = [
        {"account_id": inv_account.id,
         "debit": round(total_absorbed, 2), "credit": 0,
         "memo": "استلام إنتاج تام"},
        {"account_id": inv_account.id,
         "debit": 0, "credit": round(total_material_cost, 2),
         "memo": "صرف مواد للإنتاج"},
    ]
    if labor_cost > 0.001:
        lines.append({
            "account_id": labor_account.id, "debit": 0,
            "credit": round(labor_cost, 2),
            "memo": "استيعاب الأجور المباشرة",
        })
    if overhead_cost > 0.001:
        lines.append({
            "account_id": overhead_account.id, "debit": 0,
            "credit": round(overhead_cost, 2),
            "memo": "استيعاب المصاريف الصناعية",
        })

    # Balance-check → variance if needed.
    total_dr = sum(l["debit"] for l in lines)
    total_cr = sum(l["credit"] for l in lines)
    diff = round(total_dr - total_cr, 2)
    if abs(diff) > 0.001:
        # Positive diff means Dr > Cr → variance credit; negative → debit.
        if diff > 0:
            lines.append({
                "account_id": variance_account.id, "debit": 0,
                "credit": abs(diff), "memo": "انحراف تكلفة التصنيع",
            })
        else:
            lines.append({
                "account_id": variance_account.id, "debit": abs(diff),
                "credit": 0, "memo": "انحراف تكلفة التصنيع",
            })

    entry = post_journal(
        company_id=company_id,
        description=(f"أمر إنتاج {work_order.number} — "
                       f"{finished_variant.sku} × {qty_to_produce}"),
        lines=lines,
        entry_date=date.today(),
        reference=f"MO-{work_order.number}",
        currency=(work_order.company.base_currency
                    if work_order.company else None),
        created_by=created_by,
        source_type="work_order", source_id=work_order.id,
    )

    # ─── 4. Persist consumption audit rows + finalise the order ─────
    for line, qty, unit_cost, mv in consumption_rows:
        db.session.add(WorkOrderConsumption(
            work_order_id=work_order.id,
            component_variant_id=line.component_variant_id,
            qty_consumed=qty,
            unit_cost_at_time=unit_cost,
        ))
        # Backfill journal_entry_id on the consumption movement so the
        # ledger and stock ledger cross-reference each other cleanly.
        mv.journal_entry_id = entry.id
    fg_movement.journal_entry_id = entry.id

    work_order.status = WorkOrderStatus.COMPLETED
    work_order.completed_at = datetime.utcnow()
    work_order.journal_entry_id = entry.id
    db.session.commit()
    return work_order
