"""MARSOUD-ERP-01 Phase 3 — warehouse transfers.

A transfer moves stock from one warehouse to another. Critically:
NO journal entry — the inventory's value didn't change, only its
location. We post two StockMovement rows per item (TRANSFER_OUT from
the source warehouse, TRANSFER_IN to the destination), tied to the
StockTransferItem so the audit log can trace either side back to the
transfer.

Cost is preserved across the transfer:
  - The OUT side consumes at the source warehouse's moving avg (under
    AVERAGE mode) or FIFO basis (under FIFO mode) — same logic as a
    sale, so the existing apply_stock_movement handles it.
  - The IN side is inserted at the EXACT cost the OUT side consumed,
    so the total inventory value stays constant across the move.
"""
from datetime import datetime

from app import db
from app.models import (
    StockTransfer, StockTransferItem, StockTransferStatus,
    StockMovementKind, ProductVariant, Warehouse,
)
from app.services.inventory import (
    apply_stock_movement, InventoryError,
)
from app.services.numbering import next_number


class TransferError(Exception):
    """Raised when a transfer can't be saved/posted/cancelled."""


def create_transfer(*, company_id, from_warehouse_id, to_warehouse_id,
                    items, created_by_id=None, notes=None):
    """Persist a DRAFT transfer. Validates warehouse + variant ownership
    + non-empty items. Doesn't touch stock yet.

    items: list of dicts {variant_id, qty}.
    """
    if from_warehouse_id == to_warehouse_id:
        raise TransferError("لا يمكن التحويل لنفس المخزن")
    if not items:
        raise TransferError("لا توجد بنود")

    src = db.session.get(Warehouse, from_warehouse_id)
    dst = db.session.get(Warehouse, to_warehouse_id)
    if not src or src.company_id != company_id:
        raise TransferError("المخزن المصدر غير صالح")
    if not dst or dst.company_id != company_id:
        raise TransferError("المخزن الوجهة غير صالح")

    tr = StockTransfer(
        company_id=company_id,
        number=next_number(company_id, "STOCK_TRANSFER"),
        from_warehouse_id=from_warehouse_id,
        to_warehouse_id=to_warehouse_id,
        status=StockTransferStatus.DRAFT.value,
        notes=(notes or "").strip() or None,
        created_by_id=created_by_id,
    )
    db.session.add(tr)
    db.session.flush()

    for line in items:
        variant_id = int(line["variant_id"])
        qty = float(line["qty"])
        if qty <= 0:
            raise TransferError("الكمية يجب أن تكون أكبر من صفر")
        variant = db.session.get(ProductVariant, variant_id)
        if not variant or variant.company_id != company_id:
            raise TransferError(f"الصنف #{variant_id} غير صالح")
        db.session.add(StockTransferItem(
            transfer_id=tr.id,
            variant_id=variant.id,
            qty=qty,
        ))
    db.session.commit()
    return tr


def post_transfer(transfer, *, posted_by_id=None):
    """Atomic: post both sides of every item, tie movements back to the
    transfer items, flip status to POSTED.

    Pre-flight: under strict mode, refuse the whole transfer if any item
    would overdraw the source warehouse. Done by apply_stock_movement
    which raises InventoryError — caught and re-raised here so the whole
    transfer rolls back.
    """
    if transfer.status != StockTransferStatus.DRAFT.value:
        raise TransferError("فقط المسودة قابلة للتنفيذ")
    if not transfer.items:
        raise TransferError("لا توجد بنود")

    src = transfer.from_warehouse
    dst = transfer.to_warehouse

    try:
        for item in transfer.items:
            variant = item.variant
            qty = float(item.qty or 0)
            if qty <= 0:
                raise TransferError(
                    f"كمية غير صحيحة: {variant.sku}"
                )
            # OUT — uses the source warehouse's current cost basis.
            out_mv = apply_stock_movement(
                variant=variant, warehouse=src,
                qty_delta=-qty, kind=StockMovementKind.TRANSFER_OUT,
                source_type="stock_transfer", source_id=transfer.id,
                actor_id=posted_by_id,
                reason=f"transfer {transfer.number} → {dst.code}",
            )
            # Snapshot the cost the OUT side actually consumed.
            consumed_cost = float(out_mv.unit_cost_at_time or 0)
            item.unit_cost_at_time = consumed_cost
            item.out_movement_id = out_mv.id
            # IN — must use EXACTLY the same cost so the total inventory
            # value across the company stays constant.
            in_mv = apply_stock_movement(
                variant=variant, warehouse=dst,
                qty_delta=qty, kind=StockMovementKind.TRANSFER_IN,
                unit_cost=consumed_cost,
                source_type="stock_transfer", source_id=transfer.id,
                actor_id=posted_by_id,
                reason=f"transfer {transfer.number} ← {src.code}",
            )
            item.in_movement_id = in_mv.id
    except InventoryError as e:
        db.session.rollback()
        raise TransferError(str(e))

    transfer.status = StockTransferStatus.POSTED.value
    transfer.posted_at = datetime.utcnow()
    transfer.posted_by_id = posted_by_id
    db.session.commit()
    return transfer


def cancel_transfer(transfer, *, cancelled_by_id=None, reason=None):
    """Cancel a DRAFT. POSTED transfers can't be cancelled — issue a
    counter-transfer instead so the audit trail is complete.
    """
    if transfer.status != StockTransferStatus.DRAFT.value:
        raise TransferError(
            "فقط المسودة قابلة للإلغاء. لتراجع تحويل منفّذ، أنشئ تحويل عكسي."
        )
    transfer.status = StockTransferStatus.CANCELLED.value
    transfer.cancelled_at = datetime.utcnow()
    transfer.cancelled_by_id = cancelled_by_id
    transfer.cancel_reason = (reason or "").strip() or None
    db.session.commit()
    return transfer
