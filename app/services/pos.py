"""MARSOUD-ERP-01 Phase 2 — POS service.

Two entry points:

  create_pos_order — ring up a sale at the register. Builds an Invoice
    with source=POS, calls the existing post_invoice_to_ledger (which
    posts revenue + COGS + VAT and drops stock), then immediately
    settles the receivable against the chosen payment method (Dr cash
    / Cr AR). Net journals when the dust settles:

      Dr cash             total
        Cr Revenue        net
        Cr VAT            tax
      Dr 5100             COGS
        Cr 1140           COGS

  void_pos_order — atomic full undo. Reverses every journal the order
    posted, restocks every tracked line at the frozen unit_cost_at_sale,
    flips invoice.status to VOIDED, records reason + actor.

Both run inside a single db.session.commit() per business event so any
sub-step failure rolls the whole thing back.
"""
from datetime import datetime, date
from sqlalchemy import select

from app import db
from app.models import (
    Invoice, InvoiceItem, InvoiceStatus, Payment, PaymentMethod,
    ProductVariant, Account, JournalEntry, StockMovement,
    StockMovementKind, DiscountType,
)
from app.services.ledger import (
    post_journal, get_account_by_code, reverse_journal, LedgerError,
)
from app.services.invoicing import (
    post_invoice_to_ledger, record_payment,
)
from app.services.inventory import (
    record_return, default_warehouse, post_refund_cogs_reversal,
    InventoryError,
)
from app.services.numbering import next_number


class POSError(Exception):
    """Raised when a POS operation cannot complete."""


def create_pos_order(
    *, company_id, items, payment_method_id, cashier_id,
    customer_id=None, cash_received=None,
    invoice_discount_type=DiscountType.NONE, invoice_discount_value=0,
    tax_rate=None, notes=None,
):
    """Ring up a sale.

    items: list of dicts:
        {variant_id, qty, unit_price,
         discount_type ('NONE'|'PERCENT'|'FIXED'), discount_value}

    Returns the persisted Invoice (status=PAID, source=POS).
    """
    if not items:
        raise POSError("الكارت فارغ — أضف منتج قبل الدفع")

    pm = db.session.get(PaymentMethod, payment_method_id)
    if not pm or pm.company_id != company_id:
        raise POSError("طريقة الدفع غير صالحة")

    # ERP-03 — stamp the open shift, if any. When shift_required_for_pos
    # is True, the /pos/ route already enforced that an open shift exists
    # before letting the cashier reach this point. When it's False, we
    # still link if a shift happens to be open (cashier could have one
    # for personal tracking).
    from app.services.pos_shifts import current_open_shift_for
    open_shift = current_open_shift_for(cashier_id, company_id)

    # Build the Invoice + items in memory.
    # MARSOUD-CURRENCY-DEFAULT (2026-07-11) — was hardcoded "SAR" so
    # POS orders in an EGP/AED/USD tenant all persisted as SAR and
    # broke totals in reports. Read from the company's base_currency
    # instead; falls back to "SAR" only when the company row itself
    # is somehow missing (defence-in-depth).
    from app.models import Company as _Company
    _company = db.session.get(_Company, company_id)
    _cur = ((_company.base_currency if _company else None) or "SAR").strip() or "SAR"
    invoice = Invoice(
        company_id=company_id,
        number=next_number(company_id, "POS"),
        customer_id=customer_id,
        cashier_id=cashier_id,
        # MARSOUD-INVOICE-CREATOR — for POS the cashier IS the creator.
        created_by_id=cashier_id,
        shift_id=open_shift.id if open_shift else None,
        source="POS",
        issue_date=date.today(),
        due_date=date.today(),
        currency=_cur,
        status=InvoiceStatus.DRAFT,
        invoice_discount_type=(
            DiscountType[invoice_discount_type]
            if isinstance(invoice_discount_type, str)
            else invoice_discount_type
        ),
        invoice_discount_value=float(invoice_discount_value or 0),
        # MARSOUD-INVOICE-TAX-ZERO (Batch 9 Ticket 1, 2026-08-01) —
        # if the POST didn't include a tax_rate, fall back to the
        # company's saved vat_rate (which may legitimately be 0 for
        # 0%-VAT jurisdictions). Old code hardcoded 15%.
        tax_rate=(float(tax_rate) if tax_rate is not None
                   else float(_company.vat_rate)
                        if _company and _company.vat_rate is not None
                        else 0.0),
        notes=notes,
        cash_received=float(cash_received) if cash_received is not None else None,
    )
    db.session.add(invoice)
    db.session.flush()

    default_wh = default_warehouse(company_id)
    if not default_wh:
        raise POSError("لا يوجد مخزن افتراضي")

    # MARSOUD-PRODUCT-BUNDLES-01 — inflate any bundle line to per-
    # component lines BEFORE any inventory / JE work. Nothing below
    # this call is aware bundles exist — every line is a real
    # per-variant sale from here on. Non-bundle lines pass through
    # unchanged. Any component with insufficient stock raises here
    # (belt-and-suspenders on top of the /lookup pre-flight).
    try:
        from app.services.bundles import (
            expand_bundle_items, BundleError,
        )
        items = expand_bundle_items(company_id, items, default_wh)
    except BundleError as _be:
        raise POSError(str(_be)) from _be

    for line in items:
        variant_id = int(line["variant_id"])
        qty = float(line["qty"])
        unit_price = float(line["unit_price"])
        if qty <= 0 or unit_price < 0:
            raise POSError("كمية أو سعر غير صحيح")
        variant = db.session.get(ProductVariant, variant_id)
        if not variant or variant.company_id != company_id or not variant.is_active:
            raise POSError(f"الصنف #{variant_id} غير صالح")
        ldt = line.get("discount_type", "NONE")
        if isinstance(ldt, str):
            try:
                ldt = DiscountType[ldt]
            except KeyError:
                ldt = DiscountType.NONE
        # MARSOUD-UNIT-CONVERSION-01 — cashier may have picked a
        # non-base unit ("كرتونة"). Persist it on the item so
        # post_invoice_to_ledger converts to base at posting time.
        unit_id = line.get("unit_id")
        try:
            unit_id = int(unit_id) if unit_id else None
        except (ValueError, TypeError):
            unit_id = None
        # MARSOUD-DUAL-UOM-WEIGHT-01 pt 2 (Abdelhamid 2026-07-25) —
        # POS carries an optional `sold_pieces` per line for products
        # with tracks_piece_count=True. Cart sends the weight in
        # `qty` (grams) plus the piece count in `sold_pieces` (usually
        # 1 for "one item of THIS weight"). Ignored for products
        # without the flag — the inventory service double-checks.
        sold_pieces = None
        raw_pieces = line.get("sold_pieces")
        if raw_pieces not in (None, "", 0):
            try:
                _v = float(raw_pieces)
                if _v > 0 and variant.product and \
                        getattr(variant.product,
                                 "tracks_piece_count", False):
                    sold_pieces = _v
            except (TypeError, ValueError):
                pass
        item = InvoiceItem(
            invoice_id=invoice.id,
            company_id=invoice.company_id,
            product_id=variant.product_id,
            variant_id=variant.id,
            warehouse_id=default_wh.id,
            description=variant.display_name,
            quantity=qty,
            unit_price=unit_price,
            discount_type=ldt,
            discount_value=float(line.get("discount_value") or 0),
            unit_id=unit_id,
            sold_pieces=sold_pieces,
            # MARSOUD-PRODUCT-BUNDLES-01 — visual grouping only.
            bundle_ref=line.get("bundle_ref"),
            bundle_product_id=line.get("bundle_product_id"),
        )
        db.session.add(item)
    db.session.flush()

    # Compute totals — same recalc the invoice flow uses.
    invoice.recalc()
    db.session.flush()

    # Validate cash receipt now (before posting).
    if cash_received is not None and float(cash_received) < float(invoice.total) - 0.001:
        raise POSError(
            f"المبلغ المستلم {float(cash_received):.2f} أقل من الإجمالي {float(invoice.total):.2f}"
        )

    # Post the standard revenue+VAT+stock+COGS path.
    try:
        post_invoice_to_ledger(invoice, created_by=cashier_id)
    except LedgerError:
        db.session.rollback()
        raise
    except InventoryError as e:
        db.session.rollback()
        raise POSError(str(e))

    invoice.status = InvoiceStatus.SENT
    db.session.flush()

    # Settle the receivable against the chosen payment method —
    # this turns the AR back into cash/bank in one shot.
    try:
        record_payment(
            invoice, float(invoice.total),
            payment_method_id=pm.id,
            created_by=cashier_id,
            notify=False,
        )
    except LedgerError:
        db.session.rollback()
        raise

    db.session.commit()
    return invoice


def void_pos_order(invoice, *, reason, actor_id):
    """Atomic full undo of a POS order.

    Order of operations (all inside one commit):
      1. Bail if not a POS invoice or already voided / non-paid.
      2. For each tracked item → record_return at the frozen
         unit_cost_at_sale. This restocks + creates RETURN movements.
      3. Reverse every JournalEntry tied to this invoice (revenue,
         COGS, payment) via the existing reverse_journal helper.
      4. invoice.status = VOIDED; voided_at/by/reason set.
    """
    if not invoice:
        raise POSError("الأوردر غير موجود")
    if invoice.source != "POS":
        raise POSError("الإلغاء متاح لأوردرات POS فقط")
    if invoice.is_voided:
        raise POSError("الأوردر ملغي بالفعل")
    if not (reason or "").strip():
        raise POSError("سبب الإلغاء مطلوب")

    # Restock + post the COGS reversal in one aggregated journal.
    tracked = []
    for item in invoice.items:
        if not item.variant_id or not item.warehouse_id:
            continue
        if not item.unit_cost_at_sale or float(item.unit_cost_at_sale) <= 0:
            continue
        tracked.append(item)

    total_restock_cost = 0.0
    from app.services.inventory import apply_stock_movement
    for item in tracked:
        # MARSOUD-UOM-PRICE — restock in BASE units to match what the
        # sale actually consumed. Previously used item.quantity (=1
        # carton) which restocked only 1 base-unit while the sale had
        # decremented 10 (item.base_quantity). Refund path already does
        # this — mirror it here for parity.
        if item.base_quantity is not None:
            qty = float(item.base_quantity or 0)
        else:
            qty = float(item.quantity or 0)
        cost = float(item.unit_cost_at_sale or 0)
        try:
            # Use REVERSAL kind so the movement log labels it as
            # an order void rather than a customer refund.
            apply_stock_movement(
                variant=item.variant, warehouse=item.warehouse,
                qty_delta=qty, kind=StockMovementKind.REVERSAL,
                unit_cost=cost,
                source_type="pos_void", source_id=invoice.id,
                actor_id=actor_id,
                reason=f"void POS-{invoice.number}: {reason}",
            )
        except InventoryError as e:
            db.session.rollback()
            raise POSError(str(e))
        total_restock_cost += cost * qty

    # Reverse the journals attached to this invoice. We pick the LATEST
    # journal of each source_type so SQLite rowid reuse (after invoice
    # deletion in dev) can't accidentally reverse an old journal tied to
    # a deleted invoice with the same id. We also skip journals that
    # were already reversed (defensive).
    journals_to_reverse = []
    for src in ("invoice", "invoice_cogs", "payment"):
        je = JournalEntry.query.filter(
            JournalEntry.company_id == invoice.company_id,
            JournalEntry.source_id == invoice.id,
            JournalEntry.source_type == src,
            JournalEntry.is_reversal == False,
        ).order_by(JournalEntry.id.desc()).first()
        if je:
            already = JournalEntry.query.filter_by(
                reversal_of=je.id, is_reversal=True,
            ).first()
            if not already:
                journals_to_reverse.append(je)
    if not journals_to_reverse:
        db.session.rollback()
        raise POSError("لم يُعثر على قيود محاسبية لهذا الأوردر")
    for je in journals_to_reverse:
        try:
            reverse_journal(je.id, created_by=actor_id)
        except LedgerError as e:
            db.session.rollback()
            raise POSError(str(e))

    invoice.status = InvoiceStatus.VOIDED
    invoice.voided_at = datetime.utcnow()
    invoice.voided_by_id = actor_id
    invoice.void_reason = reason.strip()
    invoice.paid_amount = 0
    # MARSOUD-LOYALTY-POINTS-01 — return any redeemed points to the
    # customer and claw back any earned points. Wrapped so a loyalty
    # hiccup can never block the void itself (§11 edge case).
    if invoice.customer_id:
        try:
            from app.services.loyalty import reverse_points_for_invoice
            reverse_points_for_invoice(invoice, actor_id=actor_id)
        except Exception:
            import logging
            logging.getLogger("ledgeros.pos").exception(
                "loyalty reversal failed for voided invoice %s",
                invoice.number)
    db.session.commit()
    return invoice
