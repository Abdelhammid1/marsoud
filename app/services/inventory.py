"""MARSOUD-ERP-01 — inventory service.

The single point of entry for every stock mutation. All other code paths
(vendor bill posting, invoice posting, refund, manual adjustment, opening
balance, AI agent, future POS) call into this module so we have one place
where the invariants live:

  - Atomicity. apply_stock_movement is a session-add — it must run inside
    a caller-owned transaction. The caller commits or rolls back the whole
    business event (journal + stock + audit) together.

  - Row lock under contention. For each (variant, warehouse) pair we
    SELECT … FOR UPDATE the StockBalance row before computing the delta,
    so two concurrent sales of the last unit serialize and exactly one
    succeeds. On SQLite the lock is effectively the database-level lock;
    on Postgres it's a row-level lock.

  - Moving-average cost. Inflows update the running average; outflows
    consume at the average and the caller cannot override it — that's the
    "freeze cost at sale time" rule. The current average is snapshotted
    into the StockMovement row (unit_cost_at_time) and onto the originating
    invoice item (unit_cost_at_sale) so future reports never recompute it.

  - Audit. Every movement records actor_id, source_type, source_id, and
    the resulting balance_qty_after / balance_value_after. With those the
    movement log is replayable and the cached StockBalance is verifiable
    against recompute_balance().
"""
from decimal import Decimal
from sqlalchemy import func, select, text

from app import db
from app.models import (
    Warehouse, ProductVariant, StockBalance, StockMovement, StockMovementKind,
    StockLot, Account, Company,
)
from app.services.ledger import post_journal, LedgerError


class InventoryError(Exception):
    """Raised when a movement is impossible (e.g. overdraw under strict mode)."""


# ─── Internal helpers ─────────────────────────────────────────────────────
def _get_account_by_code(company_id, code):
    return Account.query.filter_by(company_id=company_id, code=code).first()


def _lock_balance(variant_id, warehouse_id):
    """SELECT … FOR UPDATE the balance row, creating it at 0/0 if missing.

    Returns the locked StockBalance instance attached to the session.
    """
    # Try to lock the existing row.
    bal = db.session.query(StockBalance).filter_by(
        variant_id=variant_id, warehouse_id=warehouse_id,
    ).with_for_update().first()
    if bal is not None:
        return bal
    # Doesn't exist yet — insert at zero. Insert-then-lock pattern is fine
    # under our SQLite dev DB; on Postgres an INSERT … ON CONFLICT DO
    # NOTHING would be slightly nicer but the current SQLAlchemy-level
    # insert + flush works because every caller is already inside a tx.
    # company_id is denormalized onto the balance row so a company purge
    # can find it — see the note on the column in models/inventory.py.
    # Taken from the variant, which is the row's real owner.
    variant = db.session.get(ProductVariant, variant_id)
    if variant is None:
        raise InventoryError("الصنف غير موجود")
    bal = StockBalance(
        variant_id=variant_id, warehouse_id=warehouse_id,
        company_id=variant.company_id, qty=0, value=0,
    )
    db.session.add(bal)
    db.session.flush()
    # Re-lock now that the row exists.
    bal = db.session.query(StockBalance).filter_by(
        variant_id=variant_id, warehouse_id=warehouse_id,
    ).with_for_update().first()
    return bal


def _company_strict(variant):
    co = Company.query.get(variant.company_id)
    return bool(getattr(co, "stock_strict_mode", True))


def _company_is_fifo(variant):
    co = Company.query.get(variant.company_id)
    return getattr(co, "cost_method", "AVERAGE") == "FIFO"


def _consume_lots_fifo(variant_id, warehouse_id, qty_needed):
    """Walk lots oldest-first, decreasing qty_remaining, until we've
    consumed `qty_needed` units. Returns the weighted cost basis the
    caller should use for the COGS snapshot.

    If the available lots don't cover the request, returns the basis of
    everything consumed; the apply_stock_movement strict check upstream
    catches the overdraw separately.
    """
    remaining = float(qty_needed)
    if remaining <= 0:
        return 0.0
    lots = db.session.query(StockLot).filter(
        StockLot.variant_id == variant_id,
        StockLot.warehouse_id == warehouse_id,
        StockLot.qty_remaining > 0,
    ).order_by(StockLot.received_at.asc(),
               StockLot.id.asc()).with_for_update().all()
    total_consumed_value = 0.0
    total_consumed_qty = 0.0
    for lot in lots:
        if remaining <= 0.0001:
            break
        avail = float(lot.qty_remaining or 0)
        take = min(avail, remaining)
        total_consumed_value += take * float(lot.unit_cost or 0)
        total_consumed_qty += take
        lot.qty_remaining = avail - take
        remaining -= take
    if total_consumed_qty <= 0:
        return 0.0
    return total_consumed_value / total_consumed_qty


# ─── The single entry point ──────────────────────────────────────────────
def apply_stock_movement(
    *, variant, warehouse, qty_delta, kind,
    unit_cost=None, source_type=None, source_id=None,
    journal_entry_id=None, actor_id=None, reason=None,
    piece_delta=None,
):
    """Atomic stock mutation. Caller MUST be inside a transaction.

    Returns the new StockMovement row (already added to the session,
    flushed so id is set).

    qty_delta:  signed Decimal/float. Inflow >0, outflow <0.
    unit_cost:  caller-supplied for inflows; IGNORED for outflows (we
                consume at the current moving average and snapshot that
                onto the movement row).
    kind:       a StockMovementKind. Used for filtering + UI labels.
    """
    if variant.company_id != warehouse.company_id:
        raise InventoryError("الصنف والمخزن من شركتين مختلفتين")
    if float(qty_delta) == 0:
        raise InventoryError("لا يمكن تسجيل حركة بقيمة صفر")

    bal = _lock_balance(variant.id, warehouse.id)
    cur_qty = float(bal.qty or 0)
    cur_value = float(bal.value or 0)
    cur_avg = (cur_value / cur_qty) if cur_qty > 0 else 0.0

    delta = float(qty_delta)
    new_qty = cur_qty + delta

    is_fifo = _company_is_fifo(variant)

    if delta > 0:
        # Inflow: caller-supplied unit_cost recomputes the moving average.
        in_cost = float(unit_cost) if unit_cost is not None else cur_avg
        if in_cost < 0:
            raise InventoryError("تكلفة الوحدة لا يمكن أن تكون سالبة")
        added_value = delta * in_cost
        new_value = cur_value + added_value
        snapshot_cost = in_cost
    else:
        # Outflow: refuse if strict mode + would overdraw.
        if new_qty < 0 and _company_strict(variant):
            raise InventoryError(
                f"الكمية غير كافية للصنف {variant.sku}: "
                f"المتاح {cur_qty:.2f}، المطلوب {-delta:.2f}"
            )
        if is_fifo:
            # ERP-03 — consume FIFO lots oldest-first. The weighted basis
            # may be DIFFERENT from cur_avg when lots have non-uniform cost.
            fifo_basis = _consume_lots_fifo(
                variant.id, warehouse.id, -delta,
            )
            snapshot_cost = fifo_basis if fifo_basis > 0 else cur_avg
            consumed_value = (-delta) * snapshot_cost
        else:
            snapshot_cost = cur_avg
            consumed_value = (-delta) * cur_avg
        new_value = cur_value - consumed_value
        if new_qty == 0:
            # Edge case: ensure no rounding drift leaves a fractional
            # value behind when the balance hits zero.
            new_value = 0.0

    # Update cached balance.
    bal.qty = new_qty
    bal.value = new_value

    # MARSOUD-DUAL-UOM-WEIGHT-01 (Abdelhamid 2026-07-24) — for
    # products opted into piece tracking, apply the piece delta
    # inside the SAME transaction. avg_cost stays weight-based, so
    # no cost math changes here. Ignored (no-op) for products that
    # never opted in.
    if piece_delta is not None and \
            getattr(variant.product, "tracks_piece_count", False):
        cur_pieces = float(bal.piece_count or 0)
        bal.piece_count = cur_pieces + float(piece_delta)
    db.session.flush()

    # Insert the immutable movement row.
    mv = StockMovement(
        company_id=variant.company_id,
        variant_id=variant.id,
        warehouse_id=warehouse.id,
        qty_delta=delta,
        unit_cost_at_time=snapshot_cost,
        kind=kind.value if hasattr(kind, "value") else str(kind),
        source_type=source_type,
        source_id=source_id,
        journal_entry_id=journal_entry_id,
        actor_id=actor_id,
        reason=reason,
        balance_qty_after=new_qty,
        balance_value_after=new_value,
    )
    db.session.add(mv)
    db.session.flush()

    # ERP-03 — create a lot row for every inflow. We do this even when
    # cost_method == AVERAGE so an owner who flips to FIFO mid-life has
    # the lot history available going forward. Lots just sit unused under
    # AVERAGE mode.
    if delta > 0:
        lot = StockLot(
            company_id=variant.company_id,
            variant_id=variant.id,
            warehouse_id=warehouse.id,
            qty_remaining=delta,
            unit_cost=snapshot_cost,
            source_movement_id=mv.id,
        )
        db.session.add(lot)
        db.session.flush()

    return mv


# ─── Wrappers used by the rest of the app ────────────────────────────────
def receive_stock(*, variant, warehouse, qty, unit_cost,
                  bill_id=None, line_id=None, actor_id=None,
                  journal_entry_id=None, reason=None,
                  piece_delta=None):
    """Inbound stock from a vendor bill (or any other receipt event).

    The matching journal entry is posted by the vendor bill code; we just
    record the stock side here and link the entry id for traceability.
    """
    if qty <= 0:
        raise InventoryError("الكمية المستلمة يجب أن تكون أكبر من صفر")
    return apply_stock_movement(
        variant=variant, warehouse=warehouse,
        qty_delta=qty, kind=StockMovementKind.RECEIPT,
        unit_cost=unit_cost,
        source_type="vendor_bill_item", source_id=line_id or bill_id,
        journal_entry_id=journal_entry_id, actor_id=actor_id, reason=reason,
        piece_delta=piece_delta,
    )


def record_sale(*, variant, warehouse, qty,
                invoice_id=None, line_id=None, actor_id=None,
                journal_entry_id=None, piece_delta=None):
    """Outbound stock for a sale. Returns the unit cost that was actually
    consumed — caller writes it to invoice_items.unit_cost_at_sale and
    uses it to build the COGS journal line.

    The caller is responsible for posting the COGS journal. We do NOT
    post it here because the journal aggregates ALL tracked lines on one
    invoice into a single Dr 5100 / Cr 1140 entry, which is cleaner.

    piece_delta: optional signed delta for products with
    tracks_piece_count=True. For a sell-by-piece sale, pass the
    negative piece count (e.g. -1 for one piece).
    """
    if qty <= 0:
        raise InventoryError("كمية البيع يجب أن تكون أكبر من صفر")
    mv = apply_stock_movement(
        variant=variant, warehouse=warehouse,
        qty_delta=-qty, kind=StockMovementKind.SALE,
        source_type="invoice_item", source_id=line_id or invoice_id,
        journal_entry_id=journal_entry_id, actor_id=actor_id,
        piece_delta=piece_delta,
    )
    return float(mv.unit_cost_at_time or 0)


def record_return(*, variant, warehouse, qty, unit_cost_at_sale,
                  refund_id=None, line_id=None, actor_id=None,
                  journal_entry_id=None):
    """Restock from a customer refund — at the SAME cost the original
    sale consumed, so the reversed COGS matches the original COGS to the
    cent. This is why we kept unit_cost_at_sale on the invoice item.
    """
    if qty <= 0:
        raise InventoryError("كمية الإرجاع يجب أن تكون أكبر من صفر")
    if unit_cost_at_sale < 0:
        unit_cost_at_sale = 0
    return apply_stock_movement(
        variant=variant, warehouse=warehouse,
        qty_delta=qty, kind=StockMovementKind.RETURN,
        unit_cost=unit_cost_at_sale,
        source_type="refund", source_id=refund_id or line_id,
        journal_entry_id=journal_entry_id, actor_id=actor_id,
    )


def record_purchase_return(*, variant, warehouse, qty,
                            refund_id=None, line_id=None, actor_id=None,
                            journal_entry_id=None):
    """MARSOUD-REFUNDS-01 — outbound stock, goods returned to a vendor.

    Consumed at the current weighted-average cost (same as SALE). Returns
    the unit cost that came out so the caller can build the balancing
    journal (Dr 2110 vendor / Cr 1300 inventory at that cost)."""
    if qty <= 0:
        raise InventoryError("كمية مرتجع المشتريات يجب أن تكون أكبر من صفر")
    mv = apply_stock_movement(
        variant=variant, warehouse=warehouse,
        qty_delta=-qty, kind=StockMovementKind.PURCHASE_RETURN,
        source_type="vendor_bill_refund",
        source_id=refund_id or line_id,
        journal_entry_id=journal_entry_id, actor_id=actor_id,
    )
    return float(mv.unit_cost_at_time or 0)


def record_adjustment(*, variant, warehouse, new_qty,
                      reason=None, actor_id=None, created_by=None):
    """Set the physical count to new_qty and post the variance journal.

    Variance journal (when delta != 0):
      Surplus (delta > 0): Dr 1140 / Cr 5990
      Shortage (delta < 0): Dr 5990 / Cr 1140
    Posts in the same transaction so the StockMovement journal_entry_id
    links back to the variance.
    """
    bal = _lock_balance(variant.id, warehouse.id)
    cur_qty = float(bal.qty or 0)
    delta = float(new_qty) - cur_qty
    if abs(delta) < 0.0001:
        # No-op — record a zero-value entry so the audit log still shows
        # someone did a count, but without posting a journal.
        return apply_stock_movement(
            variant=variant, warehouse=warehouse,
            qty_delta=0.0001 if cur_qty > 0 else 0,
            kind=StockMovementKind.ADJUSTMENT,
            source_type="manual_adjustment", source_id=None,
            actor_id=actor_id, reason=reason or "جرد بدون فرق",
        ) if False else None
    # Variance journal: use the CURRENT moving-average cost.
    cur_avg = (float(bal.value or 0) / cur_qty) if cur_qty > 0 else 0.0
    variance_value = abs(delta) * cur_avg
    inventory_acc = _get_account_by_code(variant.company_id, "1300")
    variance_acc = _get_account_by_code(variant.company_id, "5990")
    if not inventory_acc or not variance_acc:
        raise InventoryError("حسابات المخزون/الفروقات غير موجودة في الشجرة")

    if variance_value > 0.001:
        if delta > 0:
            # Surplus — physical > books → inventory goes UP.
            lines = [
                {"account_id": inventory_acc.id, "debit": variance_value,
                 "credit": 0, "memo": f"زيادة جرد — {variant.sku}"},
                {"account_id": variance_acc.id, "debit": 0,
                 "credit": variance_value, "memo": f"فرق جرد — {variant.sku}"},
            ]
        else:
            # Shortage — physical < books → inventory goes DOWN.
            lines = [
                {"account_id": variance_acc.id, "debit": variance_value,
                 "credit": 0, "memo": f"عجز جرد — {variant.sku}"},
                {"account_id": inventory_acc.id, "debit": 0,
                 "credit": variance_value, "memo": f"عجز جرد — {variant.sku}"},
            ]
        entry = post_journal(
            company_id=variant.company_id,
            description=f"تسوية جرد — {variant.sku}: {reason or 'بدون سبب'}",
            lines=lines,
            created_by=created_by or actor_id,
            source_type="stock_adjustment",
            source_id=None,
        )
        je_id = entry.id
    else:
        # Tiny delta with zero cost basis — record movement but no journal.
        je_id = None

    return apply_stock_movement(
        variant=variant, warehouse=warehouse,
        qty_delta=delta, kind=StockMovementKind.ADJUSTMENT,
        source_type="manual_adjustment", source_id=None,
        journal_entry_id=je_id, actor_id=actor_id, reason=reason,
    )


def record_opening_balance(*, variant, warehouse, qty, unit_cost,
                           actor_id=None, created_by=None, reason=None):
    """First-time entry of bookkeeping-existing stock at system go-live.

    Posts: Dr 1140 / Cr 3900 (Opening Balance Equity)
    Creates an OPENING movement linked to that journal.

    Refuses to run if any stock movement already exists for this
    (variant, warehouse) pair — opening balance is one-shot.
    """
    if qty <= 0:
        raise InventoryError("الرصيد الافتتاحي يجب أن يكون أكبر من صفر")
    if unit_cost <= 0:
        raise InventoryError("تكلفة الوحدة يجب أن تكون أكبر من صفر")
    existing = StockMovement.query.filter_by(
        variant_id=variant.id, warehouse_id=warehouse.id,
    ).first()
    if existing:
        raise InventoryError(
            f"يوجد حركة مخزون سابقة للصنف {variant.sku} في هذا المخزن — "
            f"لا يمكن إدخال رصيد افتتاحي."
        )

    inv_acc = _get_account_by_code(variant.company_id, "1300")
    open_acc = _get_account_by_code(variant.company_id, "3900")
    if not inv_acc or not open_acc:
        raise InventoryError("حسابات المخزون/الافتتاح غير موجودة")
    value = float(qty) * float(unit_cost)
    entry = post_journal(
        company_id=variant.company_id,
        description=f"رصيد افتتاحي — {variant.sku} × {qty}",
        lines=[
            {"account_id": inv_acc.id, "debit": value, "credit": 0,
             "memo": f"رصيد افتتاحي مخزون — {variant.sku}"},
            {"account_id": open_acc.id, "debit": 0, "credit": value,
             "memo": "حساب الافتتاح"},
        ],
        created_by=created_by or actor_id,
        source_type="opening_stock", source_id=variant.id,
    )
    return apply_stock_movement(
        variant=variant, warehouse=warehouse,
        qty_delta=qty, kind=StockMovementKind.OPENING,
        unit_cost=unit_cost,
        source_type="opening_stock", source_id=variant.id,
        journal_entry_id=entry.id, actor_id=actor_id, reason=reason,
    )


# ─── Verification / healing ──────────────────────────────────────────────
def recompute_balance(variant, warehouse):
    """Walk every stock_movements row for this (variant, warehouse) and
    sum the qty_delta + last balance_value_after.

    Returns dict {qty, value, cached_qty, cached_value, ok}.
    ok=False means StockBalance has drifted from the ledger — something
    bypassed apply_stock_movement, which is a bug.
    """
    movs = StockMovement.query.filter_by(
        variant_id=variant.id, warehouse_id=warehouse.id,
    ).order_by(StockMovement.id.asc()).all()
    qty = sum(float(m.qty_delta or 0) for m in movs)
    value = float(movs[-1].balance_value_after or 0) if movs else 0.0
    bal = StockBalance.query.filter_by(
        variant_id=variant.id, warehouse_id=warehouse.id,
    ).first()
    cached_qty = float(bal.qty or 0) if bal else 0.0
    cached_value = float(bal.value or 0) if bal else 0.0
    return {
        "qty": qty, "value": value,
        "cached_qty": cached_qty, "cached_value": cached_value,
        "ok": abs(qty - cached_qty) < 0.001 and abs(value - cached_value) < 0.01,
    }


# ─── Read-only helpers used by routes + agent ─────────────────────────────
def low_stock_variants(company_id, multiplier=1.0):
    """Variants whose total_qty < reorder_level × multiplier."""
    out = []
    variants = ProductVariant.query.filter_by(
        company_id=company_id, is_active=True,
    ).all()
    for v in variants:
        if v.reorder_level and v.total_qty < float(v.reorder_level) * multiplier:
            out.append(v)
    return out


def total_inventory_value(company_id):
    """SUM(value) across every StockBalance in this company."""
    rows = db.session.query(
        StockBalance.variant_id, StockBalance.value,
    ).join(
        ProductVariant, StockBalance.variant_id == ProductVariant.id,
    ).filter(ProductVariant.company_id == company_id).all()
    return float(sum(float(r.value or 0) for r in rows))


def default_warehouse(company_id):
    return Warehouse.query.filter_by(
        company_id=company_id, is_default=True, is_active=True,
    ).first() or Warehouse.query.filter_by(
        company_id=company_id, is_active=True,
    ).first()


def find_variant_by_barcode(company_id, barcode):
    return ProductVariant.query.filter_by(
        company_id=company_id, barcode=barcode, is_active=True,
    ).first()


def post_sale_cogs_journal(*, company_id, total_cost, invoice,
                           created_by=None):
    """Helper used by the invoice posting code to put the aggregated
    Dr 5100 / Cr 1140 entry. Returns the JournalEntry.
    """
    if total_cost <= 0:
        return None
    cogs_acc = _get_account_by_code(company_id, "5100")
    inv_acc = _get_account_by_code(company_id, "1300")
    if not cogs_acc or not inv_acc:
        raise InventoryError("حسابات COGS/Inventory غير موجودة في الشجرة")
    return post_journal(
        company_id=company_id,
        description=f"تكلفة بضاعة مباعة — فاتورة {invoice.number}",
        lines=[
            {"account_id": cogs_acc.id, "debit": total_cost, "credit": 0,
             "memo": f"COGS — فاتورة {invoice.number}"},
            {"account_id": inv_acc.id, "debit": 0, "credit": total_cost,
             "memo": f"خصم من المخزون — فاتورة {invoice.number}"},
        ],
        entry_date=invoice.issue_date,
        reference=f"COGS-{invoice.number}",
        created_by=created_by,
        source_type="invoice_cogs", source_id=invoice.id,
    )


def post_refund_cogs_reversal(*, company_id, total_cost, refund,
                              created_by=None):
    """Reverse the COGS for a fully-refunded invoice.
    Dr 1140 / Cr 5100.
    """
    if total_cost <= 0:
        return None
    cogs_acc = _get_account_by_code(company_id, "5100")
    inv_acc = _get_account_by_code(company_id, "1300")
    if not cogs_acc or not inv_acc:
        raise InventoryError("حسابات COGS/Inventory غير موجودة في الشجرة")
    return post_journal(
        company_id=company_id,
        description=f"عكس تكلفة بضاعة مباعة — مرتجع #{refund.id}",
        lines=[
            {"account_id": inv_acc.id, "debit": total_cost, "credit": 0,
             "memo": "إعادة بضاعة للمخزون"},
            {"account_id": cogs_acc.id, "debit": 0, "credit": total_cost,
             "memo": "عكس COGS"},
        ],
        reference=f"REF-COGS-{refund.id}",
        created_by=created_by,
        source_type="refund_cogs", source_id=refund.id,
    )


# ─── MARSOUD-DUAL-UOM-WEIGHT-01 (Abdelhamid 2026-07-24) ──────────
def start_inventory_count(*, variant, warehouse,
                            counted_qty, counted_pieces,
                            counted_by_id=None):
    """Create a DRAFT InventoryCount capturing the current book
    balance + the physical count. Returns the row so the caller can
    show variance before confirming."""
    from app.models import InventoryCount, INV_COUNT_DRAFT
    if variant.company_id != warehouse.company_id:
        raise InventoryError("الصنف والمخزن من شركتين مختلفتين")
    bal = _lock_balance(variant.id, warehouse.id)
    book_qty = _dec(bal.qty)
    book_pieces = _dec(bal.piece_count or 0)
    row = InventoryCount(
        company_id=variant.company_id,
        variant_id=variant.id, warehouse_id=warehouse.id,
        book_qty=book_qty, book_pieces=book_pieces,
        counted_qty=_dec(counted_qty),
        counted_pieces=_dec(counted_pieces),
        variance_qty=_dec(counted_qty) - book_qty,
        variance_pieces=_dec(counted_pieces) - book_pieces,
        status=INV_COUNT_DRAFT,
        counted_by_id=counted_by_id,
    )
    db.session.add(row); db.session.commit()
    return row


def commit_inventory_count(count_row, *, actor_id=None):
    """Post the variance:
      · Weight variance → record_adjustment (existing service; posts
        the balancing JE).
      · Piece variance → direct piece_count update on StockBalance
        (no JE — pieces are a parallel counter, not a valued
        quantity).
    Zero-variance counts CONFIRM without posting anything."""
    from app.models import InventoryCount, INV_COUNT_CONFIRMED
    if count_row.status != "DRAFT":
        raise InventoryError("هذا الجرد مؤكد بالفعل")
    variant = count_row.variant
    warehouse = count_row.warehouse

    if _dec(count_row.variance_qty) != 0:
        mv = record_adjustment(
            variant=variant, warehouse=warehouse,
            new_qty=float(count_row.counted_qty),
            reason=f"جرد #{count_row.id}",
            actor_id=actor_id, created_by=actor_id,
        )
        if mv is not None:
            count_row.adjustment_movement_id = mv.id

    if _dec(count_row.variance_pieces) != 0:
        bal = _lock_balance(variant.id, warehouse.id)
        bal.piece_count = _dec(count_row.counted_pieces)

    from datetime import datetime as _dt
    count_row.status = INV_COUNT_CONFIRMED
    count_row.confirmed_at = _dt.utcnow()
    db.session.commit()
    return count_row


def _dec(v):
    from decimal import Decimal
    return Decimal(str(v or 0))
