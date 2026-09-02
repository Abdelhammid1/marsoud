"""MARSOUD-PURCHASE-ORDERS-01 (2026-09-02) — أوامر الشراء + إذن الاستلام.

Every state transition + every counter mutation lives here. The
route layer calls in; it never flips a status or bumps a counter
directly. Same discipline as `post_vendor_bill` — one place to look,
one place to audit.

Non-negotiable design constraint (ticket §2):
  * `create_grn` / `receive_purchase_order_items` MUST NOT touch
    `StockMovement` or `receive_stock`.
  * MUST NOT call `post_journal` or any ledger side-effect.
  * The vendor bill is still the single source of both. This service
    only bumps `qty_received` on PO items.

`_apply_bill_to_po(bill)` is the tiny helper `post_vendor_bill`
imports at the end of its own successful path to bump `qty_invoiced`
and refuse over-invoicing. It raises `LedgerError` (not
`PurchaseOrderError`) so the vendor-bill route's existing `except
LedgerError` catches it and rolls back the whole transaction.
"""
from datetime import datetime, date
from app import db
from app.models import (
    PurchaseOrder, PurchaseOrderItem, PurchaseOrderStatus,
    GoodsReceiptNote, GoodsReceiptItem,
    Vendor, VendorBill, User,
)
from app.models.vendor_bill import BillLineType, VendorBillStatus
from app.services.numbering import next_number
from app.services.ledger import LedgerError


class PurchaseOrderError(Exception):
    """Domain-level validation failure. Route layer catches + flashes."""


# ─── Create + number ─────────────────────────────────────────────
def create_po(company_id, *, vendor_id, items, currency="SAR",
              issue_date=None, expected_date=None, tax_rate=0,
              notes=None, requested_by_id):
    """Persist a new PurchaseOrder in status REQUESTED.

    `items` is a list of dicts. Each dict must carry `description` +
    `quantity`; the rest (line_type, variant_id, warehouse_id,
    unit_id, unit_price) are optional and default sanely. Empty item
    list is refused — a request-to-buy with nothing on it makes no
    sense.
    """
    if not vendor_id:
        raise PurchaseOrderError("اختر المورد أولاً")
    vendor = db.session.get(Vendor, int(vendor_id))
    if not vendor or vendor.company_id != company_id:
        raise PurchaseOrderError("المورد غير موجود")

    items = [i for i in (items or []) if (i.get("description") or "").strip()
             and float(i.get("quantity") or 0) > 0]
    if not items:
        raise PurchaseOrderError("طلب الشراء لازم يحتوي على بند واحد على الأقل")

    po = PurchaseOrder(
        company_id=company_id,
        number=next_number(company_id, "PURCHASE_ORDER"),
        vendor_id=vendor.id,
        status=PurchaseOrderStatus.REQUESTED,
        currency=currency or "SAR",
        issue_date=issue_date or date.today(),
        expected_date=expected_date,
        tax_rate=float(tax_rate or 0),
        notes=(notes or "").strip() or None,
        requested_by_id=requested_by_id,
    )
    db.session.add(po)
    db.session.flush()

    for row in items:
        lt = row.get("line_type") or "INVENTORY"
        if isinstance(lt, BillLineType):
            line_type = lt
        else:
            try:
                line_type = BillLineType(lt)
            except ValueError:
                line_type = BillLineType.INVENTORY
        item = PurchaseOrderItem(
            purchase_order_id=po.id,
            description=(row.get("description") or "").strip()[:255],
            line_type=line_type,
            variant_id=row.get("variant_id") or None,
            warehouse_id=row.get("warehouse_id") or None,
            unit_id=row.get("unit_id") or None,
            quantity=float(row.get("quantity") or 0),
            unit_price=float(row.get("unit_price") or 0),
        )
        db.session.add(item)

    db.session.flush()
    po.recalc()
    db.session.commit()
    return po


# ─── Approve / Reject / Cancel / Delete ──────────────────────────
def approve_po(po, *, actor_id):
    """REQUESTED → APPROVED. Non-INVENTORY lines auto-receive
    because there is no physical inspection step for them
    (ticket §12 edge case)."""
    if po.status != PurchaseOrderStatus.REQUESTED:
        raise PurchaseOrderError(
            f"لا يمكن اعتماد أمر شراء في حالة {po.status_ar}")
    po.status = PurchaseOrderStatus.APPROVED
    po.approved_by_id = actor_id
    po.approved_at = datetime.utcnow()

    for it in po.items:
        if it.line_type != BillLineType.INVENTORY:
            it.qty_received = float(it.quantity or 0)

    if po.is_fully_received:
        po.status = PurchaseOrderStatus.RECEIVED
    db.session.commit()
    _log(po, "UPDATE", extra={"outcome": "approved"})
    return po


def reject_po(po, *, reason, actor_id):
    reason = (reason or "").strip()
    if not reason:
        raise PurchaseOrderError("سبب الرفض مطلوب")
    if po.status != PurchaseOrderStatus.REQUESTED:
        raise PurchaseOrderError(
            f"لا يمكن رفض أمر شراء في حالة {po.status_ar}")
    po.status = PurchaseOrderStatus.REJECTED
    po.rejected_reason = reason
    db.session.commit()
    _log(po, "UPDATE", extra={"outcome": "rejected", "reason": reason})
    return po


def cancel_po(po, *, reason, actor_id):
    """APPROVED → CANCELLED. Only when zero GRNs exist. Any receipt
    means the vendor already delivered — an "cancel" would leave
    delivered goods off-books. Enforced here per the ticket state
    machine."""
    reason = (reason or "").strip()
    if not reason:
        raise PurchaseOrderError("سبب الإلغاء مطلوب")
    if po.status != PurchaseOrderStatus.APPROVED:
        raise PurchaseOrderError(
            f"لا يمكن إلغاء أمر شراء في حالة {po.status_ar}")
    if po.receipts.count() > 0:
        raise PurchaseOrderError(
            "لا يمكن إلغاء أمر شراء تم استلامه — عالج أي بضاعة "
            "زائدة بتسوية مخزون بعد الفوترة")
    po.status = PurchaseOrderStatus.CANCELLED
    po.cancelled_reason = reason
    db.session.commit()
    _log(po, "UPDATE", extra={"outcome": "cancelled", "reason": reason})
    return po


def delete_po(po, *, actor_id):
    """Soft delete. REQUESTED only, per ticket §12."""
    if po.status != PurchaseOrderStatus.REQUESTED:
        raise PurchaseOrderError(
            "لا يمكن حذف أمر شراء تم اعتماده — استخدم إلغاء")
    if po.deleted_at is not None:
        raise PurchaseOrderError("أمر الشراء محذوف بالفعل")
    po.deleted_at = datetime.utcnow()
    po.deleted_by_id = actor_id
    db.session.commit()
    _log(po, "DELETE", extra={"outcome": "soft_deleted"})
    return po


# ─── Receive (GRN) ────────────────────────────────────────────────
def receive_purchase_order_items(po, items_data, received_by_id,
                                  notes=None, received_date=None):
    """Create a GoodsReceiptNote + bump PO counters.

    `items_data` is a list of dicts: `{"po_item_id": <int>,
    "quantity_received": <float>}`. Zero/negative quantities are
    silently skipped. Over-receipt on any line raises
    `PurchaseOrderError` BEFORE any commit, so the whole GRN is
    rolled back cleanly.

    Does NOT post a JE or move stock. See ticket §2.
    """
    if po.status not in (PurchaseOrderStatus.APPROVED,
                          PurchaseOrderStatus.PARTIALLY_RECEIVED):
        raise PurchaseOrderError(
            f"لا يمكن الاستلام من أمر شراء في حالة {po.status_ar}")

    # Pre-validate everything BEFORE we insert the GRN row.
    normalized = []
    by_id = {i.id: i for i in po.items}
    for row in (items_data or []):
        pid = int(row.get("po_item_id") or 0)
        po_item = by_id.get(pid)
        if not po_item:
            continue  # tolerate stray row from a stale form
        qty = float(row.get("quantity_received") or 0)
        if qty <= 0:
            continue
        remaining = po_item.qty_remaining_to_receive
        if qty > remaining + 0.001:
            raise PurchaseOrderError(
                f"الكمية المستلمة ({qty}) أكبر من المتبقي "
                f"({remaining}) في بند '{po_item.description}'"
            )
        normalized.append((po_item, qty))

    if not normalized:
        raise PurchaseOrderError("لا توجد كمية لاستلامها")

    grn = GoodsReceiptNote(
        company_id=po.company_id,
        number=next_number(po.company_id, "GOODS_RECEIPT"),
        purchase_order_id=po.id,
        received_date=received_date or date.today(),
        received_by_id=received_by_id,
        notes=(notes or "").strip() or None,
    )
    db.session.add(grn)
    db.session.flush()

    for po_item, qty in normalized:
        db.session.add(GoodsReceiptItem(
            grn_id=grn.id,
            po_item_id=po_item.id,
            quantity_received=qty,
        ))
        po_item.qty_received = float(po_item.qty_received or 0) + qty

    if po.is_fully_received:
        po.status = PurchaseOrderStatus.RECEIVED
    else:
        po.status = PurchaseOrderStatus.PARTIALLY_RECEIVED

    db.session.commit()
    _log(po, "UPDATE",
         extra={"outcome": "received", "grn_id": grn.id,
                "line_count": len(normalized)})
    return grn


# ─── Bill hook — called from post_vendor_bill ────────────────────
def _apply_bill_to_po(bill):
    """Called from post_vendor_bill AFTER the JE + inventory + status
    flip, BEFORE the final commit. Refuses over-invoicing by raising
    LedgerError — the caller's transaction rolls back cleanly.

    Matches VendorBillItem → PurchaseOrderItem by (variant_id,
    description) tuple. If the operator preserved the prefilled
    quantities the match is exact; if they added extra lines, those
    lines simply don't match anything and are ignored (the PO's
    remaining-to-invoice stays as-is; a follow-up bill without a
    from_po will absorb them).
    """
    if not getattr(bill, "purchase_order_id", None):
        return  # non-PO bill — no-op
    po = db.session.get(PurchaseOrder, bill.purchase_order_id)
    if not po or po.company_id != bill.company_id:
        return  # cross-tenant or missing — silently ignore

    # Match by (variant_id, description) with a fallback to
    # description-only. Multiple items with the same match tuple:
    # walk them in order and consume remaining-to-invoice per line.
    remaining_by_item = {i.id: i.qty_remaining_to_invoice
                          for i in po.items}
    updates = {}   # po_item_id -> qty to bump

    def _find_match(bi):
        # Exact: same variant_id AND same description.
        for it in po.items:
            if (remaining_by_item.get(it.id, 0) > 0.001
                    and (bi.variant_id or None) == (it.variant_id or None)
                    and (bi.description or "").strip() == (it.description or "").strip()):
                return it
        # Fallback: same variant_id only, or same description only.
        for it in po.items:
            if remaining_by_item.get(it.id, 0) > 0.001 and (
                    (bi.variant_id and bi.variant_id == it.variant_id)
                    or (bi.description and bi.description == it.description)):
                return it
        return None

    for bi in bill.items:
        qty = float(bi.quantity or 0)
        if qty <= 0:
            continue
        match = _find_match(bi)
        if not match:
            # Unmatched item — new line not in the PO. Skip; the
            # bill still posts, the PO counters just aren't touched
            # for this line.
            continue
        rem = remaining_by_item[match.id]
        if qty > rem + 0.001:
            raise LedgerError(
                f"الكمية المفوترة ({qty}) أكبر من المستلم غير "
                f"المفوتر ({rem}) في بند '{match.description}'"
            )
        updates[match.id] = updates.get(match.id, 0) + qty
        remaining_by_item[match.id] -= qty

    for pid, qty in updates.items():
        # In-place refetch (session-local) — no extra query.
        for it in po.items:
            if it.id == pid:
                it.qty_invoiced = float(it.qty_invoiced or 0) + qty
                break

    if po.is_fully_invoiced:
        po.status = PurchaseOrderStatus.CLOSED


# ─── Report ──────────────────────────────────────────────────────
def pending_pos_report(company_id, *, vendor_id=None, status=None):
    """AC #8 — "أوامر شراء معلّقة". Non-terminal statuses only unless
    the caller narrows by an explicit status filter."""
    q = (PurchaseOrder.query
         .filter_by(company_id=company_id)
         .filter(PurchaseOrder.deleted_at.is_(None)))
    if vendor_id:
        q = q.filter(PurchaseOrder.vendor_id == int(vendor_id))
    if status:
        q = q.filter(PurchaseOrder.status == PurchaseOrderStatus(status))
    else:
        q = q.filter(PurchaseOrder.status.in_((
            PurchaseOrderStatus.REQUESTED,
            PurchaseOrderStatus.APPROVED,
            PurchaseOrderStatus.PARTIALLY_RECEIVED,
        )))
    return q.order_by(PurchaseOrder.issue_date.desc()).all()


# ─── Internal ────────────────────────────────────────────────────
def _log(po, action_type, *, extra=None):
    try:
        from app.services.activity import log_action
        vendor_name = po.vendor.name if po.vendor else "?"
        log_action(
            action_type=action_type,
            entity_type="purchase_order",
            entity_id=po.id,
            entity_label=f"أمر شراء {po.number} — {vendor_name}",
            company_id=po.company_id,
            extra_data=extra or {},
        )
    except Exception:
        pass
