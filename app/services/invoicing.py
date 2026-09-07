"""Invoice posting logic — when an invoice is sent or paid, post journals automatically."""
from datetime import date
from app import db
from app.models import Invoice, InvoiceStatus, Payment, Account, Refund, RefundType, CreditNote, PaymentMethod
from app.services.ledger import post_journal, reverse_journal, get_account_by_code, LedgerError


def send_invoice_notification(invoice):
    """Wrapper: emails customer when invoice is sent. Safe — logs on failure."""
    try:
        from app.services.email import send_invoice_email
        return send_invoice_email(invoice, attach_pdf=True)
    except Exception:
        import logging
        logging.getLogger("ledgeros.invoicing").exception("Failed to send invoice email")
        return False


def _revenue_lines_by_cost_center(invoice, revenue_account_id):
    """MARSOUD-COST-CENTERS-03-REVENUE-SPLIT (2026-09-03) — split the
    single aggregate revenue credit into one line per distinct
    InvoiceItem.cost_center_id bucket. Weight by item.line_total
    (post-line-discount, pre-invoice-discount — matches subtotal
    exactly). The last bucket in insertion order absorbs the rounding
    residue so Σ bucket_credits == float(taxable_base) TO THE CENT,
    matching what post_journal expects for AR = Σ Revenue + VAT.

    Zero-behaviour-change guarantee: when every item.cost_center_id
    is None (every historic invoice + every POS invoice today), the
    grouping collapses to one bucket → one credit line, byte-
    identical to the pre-ticket shape.

    Two revenue lines with the same (account, cost_center_id) are
    never emitted — items sharing a CC accumulate into one bucket by
    the dict.
    """
    revenue_credit = float(invoice.taxable_base
                            if invoice.taxable_base
                            else invoice.subtotal)
    # cost_center_id (int or None) -> Σ line_total. Iteration order
    # is invoice.items order, i.e. deterministic.
    groups = {}
    for item in invoice.items:
        key = item.cost_center_id
        groups[key] = groups.get(key, 0.0) + float(item.line_total or 0)
    if not groups:
        # Defensive — invoice.recalc runs first so items_total > 0
        # normally, but a caller could hand us an empty invoice.
        return [{
            "account_id": revenue_account_id, "debit": 0,
            "credit": revenue_credit,
            "memo": "إيراد (صافي بعد الخصم)",
            "cost_center_id": None,
        }]
    items_total = sum(groups.values()) or 1.0
    lines = []
    running = 0.0
    keys = list(groups.keys())
    for i, cc_id in enumerate(keys):
        if i < len(keys) - 1:
            val = round(revenue_credit * groups[cc_id]
                        / items_total, 2)
            running += val
        else:
            # Last bucket carries the rounding residue so the
            # invariant Σ credits == taxable_base holds exactly.
            val = round(revenue_credit - running, 2)
        lines.append({
            "account_id": revenue_account_id, "debit": 0,
            "credit": val,
            "memo": "إيراد (صافي بعد الخصم)",
            "cost_center_id": cc_id,
        })
    return lines


def post_invoice_to_ledger(invoice, created_by=None):
    """Dr Accounts Receivable / Cr Revenue (split per cost center)
    + Cr VAT Payable. See _revenue_lines_by_cost_center for the
    split logic; VAT + AR stay aggregate.

    MARSOUD-INVOICE-FX-01 (2026-09-08) — foreign-currency invoices
    (currency != company.base_currency) take a completely different
    path: no ledger entry at issue AT ALL. Physical stock still
    moves so the warehouse stays accurate, and the CoGS + AR +
    Revenue + VAT are posted in one EGP-denominated JE by
    `record_payment` once the cashier types the collection-day
    exchange rate. Existing EGP invoices are byte-identical to the
    pre-ticket flow.
    """
    # MARSOUD-INVOICE-FX-01 — foreign-currency invoices defer the JE
    # entirely. Runs BEFORE the AR/revenue-account lookups so a
    # tenant without a fully-set-up CoA can still raise a SAR
    # invoice (they can't collect it without 1110/4100/2120 either
    # way, but that failure surfaces at the payment form).
    company = invoice.company
    base_ccy = ((company.base_currency if company else None) or "EGP").upper()
    inv_ccy = (invoice.currency or "").upper()
    is_foreign = inv_ccy != base_ccy
    if is_foreign:
        _move_stock_only_for_invoice(invoice, created_by=created_by)
        _log_invoice_activity(invoice)
        return None

    # MARSOUD-COA-REBUILD — AR debit lands on the customer's own
    # sub-account (auto-created the first time we need it) rather than
    # the parent header. Without this we'd trip post_journal's
    # is_postable guard because 1130 is now a header.
    from app.services.subsidiary import party_ar_account
    ar = party_ar_account(invoice)
    revenue = get_account_by_code(invoice.company_id, "4100")
    vat_payable = get_account_by_code(invoice.company_id, "2120")
    if not ar or not revenue:
        raise LedgerError("شجرة الحسابات الافتراضية ناقصة (1130 / 4100)")

    # MARSOUD-COST-CENTERS-03-REVENUE-SPLIT — the AR debit stays
    # aggregate; revenue splits per CC bucket via the helper.
    lines = [
        {"account_id": ar.id, "debit": float(invoice.total),
         "credit": 0, "memo": f"فاتورة {invoice.number}"},
    ]
    lines.extend(
        _revenue_lines_by_cost_center(invoice, revenue.id))

    if float(invoice.tax_amount or 0) > 0.001 and vat_payable:
        lines.append({
            "account_id": vat_payable.id,
            "debit": 0,
            "credit": float(invoice.tax_amount),
            "memo": "ضريبة قيمة مضافة",
        })

    customer_label = invoice.customer.name if invoice.customer else "زبون نقدي"
    entry = post_journal(
        company_id=invoice.company_id,
        description=f"فاتورة مبيعات #{invoice.number} — {customer_label}",
        lines=lines,
        entry_date=invoice.issue_date,
        reference=f"INV-{invoice.number}",
        currency=invoice.currency,
        created_by=created_by,
        source_type="invoice",
        source_id=invoice.id,
    )

    # ERP-01 — drop stock + post the COGS journal for every tracked item.
    # Runs inside the same transaction as the revenue journal so a stock
    # error (e.g. overdraw) rolls both back together.
    _apply_inventory_side_for_invoice(invoice, entry, created_by)

    # MARSOUD-COMM-ACCRUAL — accrue the sales-rep commission the
    # moment the invoice is posted (dated invoice.issue_date), so
    # revenue + commission expense land in the same period. This
    # replaces the old behaviour of accruing at payment time, which
    # split them across months and broke monthly profit closes.
    try:
        from app.services.sales_commissions import (
            record_commission_accrual_for_invoice,
        )
        record_commission_accrual_for_invoice(invoice, created_by=created_by)
    except Exception:
        import logging
        logging.getLogger("ledgeros.invoicing").exception(
            "Failed to accrue commission for invoice %s", invoice.number,
        )

    # MARSOUD-ACTLOG-01 — record the invoice posting as a CREATE action.
    _log_invoice_activity(invoice)

    return entry


def _fx_collection_journal(*, invoice, ar, receiving_account,
                            method_label, amount_foreign,
                            exchange_rate, payment_date, created_by):
    """MARSOUD-INVOICE-FX-01 — post the deferred sale-side + the
    collection cash-side + the proportional COGS/Stock as ONE
    EGP-denominated journal entry.

    Since the foreign invoice never posted at issue, the AR sub-
    account has a zero balance — Cr AR here would create a negative.
    Instead we go direct: Dr Cash, Cr Revenue (per CC), Cr VAT, and
    Dr COGS / Cr Stock for the same share of tracked-item cost. The
    proportional share `share = amount_foreign / invoice.total`
    means partial payments each book their own share cleanly with
    no reconciliation needed.

    All lines are EGP because currency=company.base_currency and
    exchange_rate=1.0 on the underlying post_journal call — the
    ledger has no idea a foreign invoice ever existed here, which
    is exactly what "ma tlmesh el compute_balance" needed.
    """
    total_foreign = float(invoice.total or 0)
    if total_foreign <= 0:
        raise LedgerError("إجمالي الفاتورة صفر — لا يمكن تحصيلها")
    share = amount_foreign / total_foreign

    taxable_egp = (float(invoice.taxable_base or 0)
                   * share * exchange_rate)
    vat_egp = (float(invoice.tax_amount or 0)
                * share * exchange_rate)
    paid_egp = amount_foreign * exchange_rate

    # Cost basis for the tracked items on this invoice — in EGP
    # already (unit_cost_at_sale was frozen at issue via
    # record_sale). Proportional to the share of the sale collected.
    total_cogs_egp = 0.0
    for it in invoice.items:
        ucs = float(getattr(it, "unit_cost_at_sale", 0) or 0)
        if ucs <= 0:
            continue
        base_qty = it.base_quantity if it.base_quantity is not None \
            else it.quantity
        total_cogs_egp += ucs * float(base_qty or 0)
    cogs_share_egp = round(total_cogs_egp * share, 2)

    revenue = get_account_by_code(invoice.company_id, "4100")
    vat_payable = get_account_by_code(invoice.company_id, "2120")
    if not revenue:
        raise LedgerError("حساب الإيرادات (4100) غير موجود")

    lines = [
        {"account_id": receiving_account.id,
         "debit": round(paid_egp, 2), "credit": 0,
         "memo": f"تحصيل — سعر الصرف {exchange_rate}"},
    ]
    # Reuse the CC-split helper — for the paid share we synthesise a
    # temporary "shrunk" invoice-like proxy so the same logic works.
    # Simpler: just push a single Revenue credit here; CC-split can
    # be added when the first tenant asks for it (foreign-currency
    # invoices don't set item.cost_center_id in v1).
    lines.append({
        "account_id": revenue.id, "debit": 0,
        "credit": round(taxable_egp, 2),
        "memo": "إيراد (صافي بعد الخصم) — تحصيل بعملة أجنبية",
    })
    if vat_egp > 0.001 and vat_payable:
        lines.append({
            "account_id": vat_payable.id, "debit": 0,
            "credit": round(vat_egp, 2),
            "memo": "ضريبة قيمة مضافة",
        })

    # COGS + Stock — only when the invoice actually has tracked
    # items (services-only invoice → cogs_share_egp is 0.0 and we
    # skip both lines cleanly).
    if cogs_share_egp > 0.001:
        cogs_acc = get_account_by_code(invoice.company_id, "5100")
        stock_acc = get_account_by_code(invoice.company_id, "1140")
        if cogs_acc and stock_acc:
            lines.append({
                "account_id": cogs_acc.id,
                "debit": cogs_share_egp, "credit": 0,
                "memo": "تكلفة البضاعة المباعة",
            })
            lines.append({
                "account_id": stock_acc.id, "debit": 0,
                "credit": cogs_share_egp,
                "memo": "خصم من المخزون",
            })

    customer_label = (invoice.customer.name if invoice.customer
                       else "زبون نقدي")
    company = invoice.company
    base_ccy = ((company.base_currency if company else None) or "EGP")
    entry = post_journal(
        company_id=invoice.company_id,
        description=(f"تحصيل من {customer_label} — "
                      f"فاتورة #{invoice.number} "
                      f"({invoice.currency}→{base_ccy}) "
                      f"({method_label})"),
        lines=lines,
        entry_date=payment_date,
        reference=f"PMT-{invoice.number}",
        currency=base_ccy,
        exchange_rate=1.0,
        created_by=created_by,
        source_type="payment",
        source_id=invoice.id,
    )
    return entry


def _log_invoice_activity(invoice):
    """Extracted so the EGP path and the FX-deferred path share one
    activity-log call site. Any log failure is swallowed — the audit
    trail is nice-to-have, never a blocker on invoice posting."""
    try:
        from app.services.activity import log_action
        log_action(
            action_type="CREATE", entity_type="invoice",
            entity_id=invoice.id,
            entity_label=f"فاتورة {invoice.number} — "
                          f"{invoice.customer.name if invoice.customer else 'زبون'}",
            company_id=invoice.company_id,
            extra_data={"total": float(invoice.total or 0),
                        "currency": invoice.currency},
        )
    except Exception:
        pass


def _move_stock_only_for_invoice(invoice, created_by):
    """MARSOUD-INVOICE-FX-01 — physical stock movement without the
    COGS journal. Foreign-currency invoices call this at ISSUE time
    (so stock leaves the warehouse immediately) and defer the COGS +
    Revenue + VAT ledger side to the collection JE that
    `record_payment` posts once the cashier has typed the actual
    exchange rate.

    Returns the total cost basis (sum of unit_cost_at_sale × base_qty
    over every tracked line) so the deferred JE can size the COGS
    debit correctly. Returns 0.0 for services-only invoices.
    """
    return _apply_inventory_side_for_invoice(
        invoice, entry=None, created_by=created_by,
        skip_cogs_journal=True)


def _apply_inventory_side_for_invoice(invoice, entry, created_by,
                                       *, skip_cogs_journal=False):
    """For each tracked invoice line:
      - resolve variant + warehouse (default to company's main warehouse)
      - call record_sale() to drop stock + snapshot the cost
      - aggregate the cost basis, then post a single COGS journal
        (Dr 5100 / Cr 1140) covering the whole invoice.

    MARSOUD-INVOICE-FX-01 — when `skip_cogs_journal=True` the stock
    movements still fire but the COGS journal is skipped and NOT
    linked to any entry. The caller (foreign-currency issue path)
    is responsible for posting the collection JE later. Returns the
    aggregated `total_cogs` in EGP so the caller can size that JE.
    """
    from app.services.inventory import (
        record_sale, default_warehouse, post_sale_cogs_journal,
        InventoryError,
    )

    # Pre-flight: catch missing variant/warehouse before mutating anything.
    tracked = []
    for item in invoice.items:
        if not item.product or not item.product.is_tracked:
            continue
        variant = item.variant
        if variant is None and item.product:
            variant = item.product.default_variant
        if not variant:
            raise LedgerError(
                f"الصنف '{item.description}' متتبّع لكن لا يوجد له variant"
            )
        warehouse = item.warehouse or default_warehouse(invoice.company_id)
        if not warehouse:
            raise LedgerError("لا يوجد مخزن افتراضي للشركة")
        tracked.append((item, variant, warehouse))

    # MARSOUD-UNIT-CONVERSION-01 — translate the cashier-entered qty
    # into the base unit BEFORE the inventory engine sees it. The
    # engine (record_sale + weighted-average cost) is untouched — it
    # still operates on a single unit dimension.
    from app.services.units import convert_to_base, UnitError

    total_cogs = 0.0
    for item, variant, warehouse in tracked:
        display_qty = float(item.quantity or 0)
        if display_qty <= 0:
            continue
        try:
            base_qty_dec = convert_to_base(
                item.product, display_qty, unit_id=item.unit_id,
            )
        except UnitError as e:
            raise LedgerError(str(e))
        base_qty = float(base_qty_dec)
        # MARSOUD-DUAL-UOM-WEIGHT-01 pt 2 (Abdelhamid 2026-07-25) —
        # forward the piece count as a negative delta for products
        # opted into piece tracking. sold_pieces NULL / 0 → skipped
        # by the inventory service (guarded on
        # tracks_piece_count too).
        piece_delta = None
        if item.sold_pieces is not None and float(item.sold_pieces or 0) > 0:
            piece_delta = -float(item.sold_pieces)
        try:
            unit_cost = record_sale(
                variant=variant, warehouse=warehouse, qty=base_qty,
                invoice_id=invoice.id, line_id=item.id,
                actor_id=created_by,
                piece_delta=piece_delta,
            )
        except InventoryError as e:
            raise LedgerError(str(e))
        # Backfill the columns on the line so refunds + reports read it later.
        item.variant_id = variant.id
        item.warehouse_id = warehouse.id
        item.unit_cost_at_sale = unit_cost
        # Freeze the base-quantity conversion on the line — same
        # philosophy as unit_cost_at_sale: an edit to the unit's
        # conversion_factor tomorrow must not silently rewrite what
        # was actually taken out of stock today.
        item.base_quantity = base_qty
        total_cogs += unit_cost * base_qty

    if total_cogs > 0.001 and not skip_cogs_journal:
        cogs_entry = post_sale_cogs_journal(
            company_id=invoice.company_id,
            total_cost=total_cogs, invoice=invoice,
            created_by=created_by,
        )
        # Link the COGS journal back onto each movement so the audit log
        # shows "this sale produced THIS journal entry".
        from app.models import StockMovement
        for item, variant, _ in tracked:
            StockMovement.query.filter_by(
                source_type="invoice_item", source_id=item.id,
                journal_entry_id=None,
            ).update({"journal_entry_id": cogs_entry.id})
    # MARSOUD-INVOICE-FX-01 — the foreign-currency issue path needs
    # the COGS total to size its collection JE later. EGP callers
    # ignore the return value (backward compatible).
    return total_cogs


def record_payment(invoice, amount, payment_date=None, method=None,
                    payment_method_id=None, created_by=None, notify=True,
                    exchange_rate=None):
    """Record a payment posting Dr <method.account> / Cr AR. Resolves the receiving
    account either from a PaymentMethod row (preferred) or from the legacy
    'cash'/'bank' string.

    MARSOUD-INVOICE-FX-01 (2026-09-08) — when invoice.currency !=
    company.base_currency, `exchange_rate` is REQUIRED (raises
    LedgerError otherwise) and the collection JE is a full
    EGP-denominated 5-line entry covering Cash + Revenue + VAT +
    COGS + Stock proportional to this slice of the sale — because
    the AR/Revenue/VAT + COGS side was deferred at issue time. EGP
    invoices are byte-identical to the pre-ticket flow.
    """
    amount = float(amount)
    if amount <= 0:
        raise LedgerError("المبلغ يجب أن يكون أكبر من صفر")
    if amount > invoice.balance + 0.01:
        raise LedgerError(f"المبلغ ({amount:.2f}) أكبر من الرصيد المتبقي ({invoice.balance:.2f})")

    # ─── MARSOUD-INVOICE-FX-01 — foreign vs base branch ─────────────
    company = invoice.company
    base_ccy = ((company.base_currency if company else None) or "EGP").upper()
    inv_ccy = (invoice.currency or "").upper()
    is_foreign = inv_ccy != base_ccy
    if is_foreign:
        if exchange_rate is None:
            raise LedgerError(
                "سعر الصرف مطلوب لتحصيل فاتورة بعملة أجنبية")
        try:
            exchange_rate = float(exchange_rate)
        except (TypeError, ValueError):
            raise LedgerError("سعر الصرف غير صالح") from None
        if exchange_rate <= 0:
            raise LedgerError("سعر الصرف يجب أن يكون أكبر من صفر")

    pm = None
    receiving_account = None
    if payment_method_id:
        pm = db.session.get(PaymentMethod, int(payment_method_id))
        if not pm or pm.company_id != invoice.company_id or not pm.is_active:
            raise LedgerError("طريقة دفع غير صالحة")
        receiving_account = pm.account
        method_label = pm.name_ar or pm.name
    else:
        # Legacy fallback. 1120 (banks) is now a header — fall back to
        # the first configured bank leaf (1121-1125) when nobody picked
        # a payment method explicitly.
        if (method or "cash") == "cash":
            receiving_account = get_account_by_code(invoice.company_id, "1110")
        else:
            for code in ("1124", "1121", "1122", "1123", "1125"):
                receiving_account = get_account_by_code(invoice.company_id, code)
                if receiving_account:
                    break
        method_label = method or "cash"

    # MARSOUD-COA-REBUILD — AR credit lands on the customer's sub-account.
    from app.services.subsidiary import party_ar_account
    ar = party_ar_account(invoice)
    if not receiving_account or not ar:
        raise LedgerError("حسابات النقدية / العملاء غير موجودة")

    if is_foreign:
        # ─── MARSOUD-INVOICE-FX-01 — deferred EGP collection JE ─────
        # Because post_invoice_to_ledger skipped the sale-side JE at
        # issue, this collection recognises the whole accounting
        # picture in one go: Cash (Dr) + Revenue (Cr) + VAT (Cr) +
        # COGS (Dr) + Stock (Cr), all in EGP at the cashier's rate.
        # Proportional to the paid slice so partial payments each
        # book their own share cleanly.
        entry = _fx_collection_journal(
            invoice=invoice, ar=ar,
            receiving_account=receiving_account,
            method_label=method_label,
            amount_foreign=amount,
            exchange_rate=exchange_rate,
            payment_date=payment_date or date.today(),
            created_by=created_by,
        )
    else:
        entry = post_journal(
            company_id=invoice.company_id,
            description=f"تحصيل من {invoice.customer.name if invoice.customer else 'زبون نقدي'} — فاتورة #{invoice.number} ({method_label})",
            lines=[
                {"account_id": receiving_account.id, "debit": amount, "credit": 0},
                {"account_id": ar.id, "debit": 0, "credit": amount},
            ],
            entry_date=payment_date or date.today(),
            reference=f"PMT-{invoice.number}",
            currency=invoice.currency,
            created_by=created_by,
            source_type="payment",
            source_id=invoice.id,
        )

    payment = Payment(
        invoice_id=invoice.id,
        company_id=invoice.company_id,
        amount=amount,
        payment_date=payment_date or date.today(),
        payment_method_id=pm.id if pm else None,
        method=(pm.name if pm else method),
        journal_entry_id=entry.id,
        # MARSOUD-INVOICE-FX-01 — capture the rate on the row; NULL
        # for base-currency payments so historic rows stay untouched.
        fx_rate=(exchange_rate if is_foreign else None),
    )
    db.session.add(payment)

    invoice.paid_amount = float(invoice.paid_amount or 0) + amount
    is_full = invoice.paid_amount >= float(invoice.total) - 0.01
    # MARSOUD-INVOICE-FX-01 — stamp the last-collection rate on the
    # invoice too, so reports don't have to join through Payment.
    if is_foreign:
        invoice.fx_rate_at_receipt = exchange_rate
    if is_full:
        invoice.status = InvoiceStatus.PAID
        # MARSOUD-LOYALTY-POINTS-01 — award loyalty points on the
        # first full-paid transition. Idempotent (guarded by
        # invoice.loyalty_points_awarded_at). Wrapped so any loyalty
        # bug never blocks a real payment.
        try:
            from app.services.loyalty import award_points_for_invoice
            award_points_for_invoice(invoice, actor_id=created_by)
        except Exception:
            import logging
            logging.getLogger("ledgeros.invoicing").exception(
                "loyalty award failed for invoice %s",
                invoice.number)
    else:
        invoice.status = InvoiceStatus.PARTIALLY_PAID

    # MARSOUD-COMM-01 Phase A — record + post a commission row for the
    # customer's assigned sales rep, if any. Wrapped + try/except so a
    # commission posting failure never blocks the actual payment.
    try:
        from app.services.sales_commissions import record_commission_for_payment
        # payment was added but not flushed yet; flush so we have payment.id
        db.session.flush()
        record_commission_for_payment(
            invoice, payment, amount,
            payment_date=payment_date or date.today(),
            created_by=created_by,
        )
    except Exception:
        import logging
        logging.getLogger("ledgeros.invoicing").exception(
            "Failed to record sales commission for payment on invoice %s",
            invoice.number,
        )

    db.session.commit()

    # MARSOUD-SAAS-BILLING-BACKFILL-01 (Batch 6 Ticket 2, 2026-07-29)
    # — unified payment path. When a SaaS invoice gets fully paid
    # from ANY screen (regular /invoices/<id> payment form, bulk
    # payment, admin/saas mark-paid), run the shared post-payment
    # routine so the tenant's subscription renews + the next
    # invoice gets created + the coupon (if any) is redeemed.
    # Idempotent — _saas_post_payment refuses to double-run on the
    # same invoice.
    if is_full and invoice.source == "SAAS_BILLING":
        try:
            from app.services.saas_billing import _saas_post_payment
            _saas_post_payment(invoice, created_by)
            db.session.commit()
        except Exception:
            db.session.rollback()
            import logging
            logging.getLogger("ledgeros.invoicing").exception(
                "SaaS post-payment hook failed for invoice %s "
                "(payment recorded, renewal skipped)", invoice.number)

    # Email notification — non-blocking, controlled by caller
    if notify:
        try:
            from app.services.email import send_payment_received_email
            send_payment_received_email(invoice, payment, is_full=is_full)
        except Exception:
            import logging
            logging.getLogger("ledgeros.invoicing").exception("Failed to send payment email")

    # MARSOUD-ACTLOG-01 — log the payment as a CREATE action.
    try:
        from app.services.activity import log_action
        log_action(
            action_type="CREATE", entity_type="payment",
            entity_id=payment.id,
            entity_label=f"دفعة على فاتورة {invoice.number} — "
                          f"{float(payment.amount or 0):.2f} {invoice.currency}",
            company_id=invoice.company_id,
            extra_data={"invoice_id": invoice.id,
                        "amount": float(payment.amount or 0)},
        )
    except Exception:
        pass

    return payment


def _apply_inventory_side_for_refund(invoice, refund, created_by):
    """Restock the tracked lines + post a reversing COGS journal."""
    from app.services.inventory import (
        record_return, default_warehouse, post_refund_cogs_reversal,
        InventoryError,
    )
    from app.models import StockMovement

    tracked = []
    for item in invoice.items:
        if not item.product or not item.product.is_tracked:
            continue
        variant = item.variant or (item.product.default_variant
                                    if item.product else None)
        if not variant:
            continue
        warehouse = item.warehouse or default_warehouse(invoice.company_id)
        if not warehouse:
            continue
        # No frozen cost on this line? Skip — the original sale didn't
        # post COGS (older data) so reversal would be wrong.
        if not item.unit_cost_at_sale or float(item.unit_cost_at_sale) <= 0:
            continue
        tracked.append((item, variant, warehouse))

    total_restock_cost = 0.0
    for item, variant, warehouse in tracked:
        # MARSOUD-UNIT-CONVERSION-01 — restock in BASE units to match
        # what was originally consumed. Prefer the frozen base_quantity
        # from the sale; fall back to display_qty for old rows that
        # predate this ticket (unit_id + base_quantity = NULL).
        if item.base_quantity is not None:
            base_qty = float(item.base_quantity or 0)
        else:
            base_qty = float(item.quantity or 0)
        if base_qty <= 0:
            continue
        cost = float(item.unit_cost_at_sale or 0)
        try:
            record_return(
                variant=variant, warehouse=warehouse, qty=base_qty,
                unit_cost_at_sale=cost,
                refund_id=refund.id, line_id=item.id,
                actor_id=created_by,
            )
        except InventoryError as e:
            raise LedgerError(str(e))
        total_restock_cost += cost * base_qty

    if total_restock_cost > 0.001:
        cogs_reversal = post_refund_cogs_reversal(
            company_id=invoice.company_id,
            total_cost=total_restock_cost, refund=refund,
            created_by=created_by,
        )
        if cogs_reversal:
            StockMovement.query.filter_by(
                source_type="refund", source_id=refund.id,
                journal_entry_id=None,
            ).update({"journal_entry_id": cogs_reversal.id})


def issue_refund(invoice, refund_type, amount=None, reason=None, created_by=None, notify=False):
    """3 scenarios: FULL, PARTIAL, CREDIT_NOTE."""
    if refund_type == RefundType.FULL:
        amount = float(invoice.total)
    elif refund_type == RefundType.PARTIAL:
        if not amount or float(amount) <= 0:
            raise LedgerError("حدد مبلغ الاسترداد الجزئي")
        if float(amount) > float(invoice.paid_amount or 0) + 0.01:
            raise LedgerError("لا يمكن استرداد أكبر من المبلغ المدفوع فعلياً")
        amount = float(amount)
    elif refund_type == RefundType.CREDIT_NOTE:
        if not amount or float(amount) <= 0:
            raise LedgerError("حدد قيمة الـ Credit Note")
        amount = float(amount)

    # MARSOUD-COA-REBUILD — refunds debit "Sales Returns & Allowances"
    # (4300, contra-revenue) instead of debiting the revenue account
    # directly. Same net effect on the P&L (revenue still goes down)
    # but the return is visible as a separate line for the accountant.
    sales_returns = get_account_by_code(invoice.company_id, "4300")
    if not sales_returns:
        raise LedgerError(
            "حساب مردودات المبيعات (4300) غير موجود — راجع شجرة الحسابات"
        )
    vat_payable = get_account_by_code(invoice.company_id, "2120")
    # MARSOUD-COA-REBUILD — AR side of the refund reversal lands on
    # the customer's own sub-account (matches the original sale).
    from app.services.subsidiary import party_ar_account
    ar = party_ar_account(invoice)
    cash = get_account_by_code(invoice.company_id, "1110")

    # Split the refund amount across Sales-Returns (net) and Output VAT
    # (tax) using the same ratio as the original invoice — mirrors the
    # original posting (Cr Revenue=subtotal + Cr VAT=tax) so VAT is
    # reclaimed correctly.
    invoice_total = float(invoice.total or 0)
    invoice_tax = float(invoice.tax_amount or 0)
    if invoice_total > 0 and invoice_tax > 0 and vat_payable:
        tax_ratio = invoice_tax / invoice_total
        refund_tax = round(amount * tax_ratio, 2)
        refund_net = round(amount - refund_tax, 2)
    else:
        refund_tax = 0.0
        refund_net = amount

    debit_lines = [{
        "account_id": sales_returns.id, "debit": refund_net,
        "credit": 0, "memo": "مردودات مبيعات",
    }]
    if refund_tax > 0:
        debit_lines.append({
            "account_id": vat_payable.id,
            "debit": refund_tax,
            "credit": 0,
            "memo": "عكس ضريبة المخرجات",
        })

    # MARSOUD-REFUNDS-01 — dedicated SRET-nnnn sequence per company for
    # sales refunds. Falls back to REF-<invoice-number> if the sequence
    # is unavailable for any reason (defensive; company_id must exist so
    # the sequence has a shard key).
    from app.services.numbering import next_number
    try:
        ref_no = next_number(invoice.company_id, "SALES_REFUND")
    except Exception:
        ref_no = f"REF-{invoice.number}"

    if refund_type == RefundType.CREDIT_NOTE:
        cn = CreditNote(
            company_id=invoice.company_id,
            customer_id=invoice.customer_id,
            invoice_id=invoice.id,
            amount=amount,
            reason=reason,
        )
        db.session.add(cn)
        entry = post_journal(
            company_id=invoice.company_id,
            description=f"Credit Note للعميل {invoice.customer.name} — فاتورة #{invoice.number}",
            lines=debit_lines + [
                {"account_id": ar.id, "debit": 0, "credit": amount, "memo": "رصيد دائن للعميل"},
            ],
            entry_date=date.today(),
            reference=ref_no,
            currency=invoice.currency,
            created_by=created_by,
            source_type="credit_note",
            source_id=invoice.id,
        )
    else:
        if float(invoice.paid_amount or 0) > 0:
            # Refund actual cash
            entry = post_journal(
                company_id=invoice.company_id,
                description=f"استرداد للعميل {invoice.customer.name} — فاتورة #{invoice.number}",
                lines=debit_lines + [
                    {"account_id": cash.id, "debit": 0, "credit": amount, "memo": "صرف نقدي للعميل"},
                ],
                entry_date=date.today(),
                reference=ref_no,
                currency=invoice.currency,
                created_by=created_by,
                source_type="refund",
                source_id=invoice.id,
            )
            invoice.paid_amount = float(invoice.paid_amount or 0) - amount
        else:
            # No payment yet — just reverse the receivable
            entry = post_journal(
                company_id=invoice.company_id,
                description=f"إلغاء فاتورة #{invoice.number}",
                lines=debit_lines + [
                    {"account_id": ar.id, "debit": 0, "credit": amount, "memo": "إلغاء الذمم"},
                ],
                entry_date=date.today(),
                reference=ref_no,
                currency=invoice.currency,
                created_by=created_by,
                source_type="refund",
                source_id=invoice.id,
            )

    refund = Refund(
        company_id=invoice.company_id,
        number=ref_no,
        invoice_id=invoice.id,
        type=refund_type,
        amount=amount,
        reason=reason,
        journal_entry_id=entry.id,
    )
    db.session.add(refund)
    db.session.flush()

    # ERP-01 — for a FULL refund of a tracked-item invoice, restock at
    # the frozen unit_cost_at_sale and reverse the COGS. PARTIAL/CREDIT
    # don't restock automatically — we just flash a hint in the route.
    if refund_type == RefundType.FULL:
        _apply_inventory_side_for_refund(invoice, refund, created_by)

    # MARSOUD-COMM-01 Phase B — claw back any commission earned on the
    # refunded portion. Reverses from the rep's unpaid bucket if any
    # exists, else creates a carry-forward charge against next month.
    # Wrapped so a commission posting failure never blocks the refund.
    try:
        from app.services.sales_commissions import record_commission_refund
        record_commission_refund(
            invoice, refund, amount,
            refund_date=date.today(),
            created_by=created_by,
        )
    except Exception:
        import logging
        logging.getLogger("ledgeros.invoicing").exception(
            "Failed to record commission clawback for refund on invoice %s",
            invoice.number,
        )

    if refund_type == RefundType.FULL:
        invoice.status = InvoiceStatus.REFUNDED
    elif refund_type == RefundType.PARTIAL:
        invoice.status = InvoiceStatus.PARTIALLY_REFUNDED

    db.session.commit()

    if notify:
        try:
            from app.services.email import send_refund_email, send_credit_note_email
            if refund_type == RefundType.CREDIT_NOTE:
                # cn was created above; fetch latest matching credit note
                cn = CreditNote.query.filter_by(invoice_id=invoice.id).order_by(CreditNote.id.desc()).first()
                if cn:
                    send_credit_note_email(invoice, cn)
            else:
                send_refund_email(invoice, refund)
        except Exception:
            import logging
            logging.getLogger("ledgeros.invoicing").exception("refund email failed")
    # MARSOUD-ACTLOG-01 — log the refund as a CREATE action.
    try:
        from app.services.activity import log_action
        log_action(
            action_type="CREATE", entity_type="refund",
            entity_id=refund.id if refund else None,
            entity_label=f"استرداد على فاتورة {invoice.number}",
            company_id=invoice.company_id,
            extra_data={"invoice_id": invoice.id,
                        "type": str(refund_type),
                        "amount": float(getattr(refund, 'amount', 0) or 0)},
        )
    except Exception:
        pass
    return refund


# ─── MARSOUD-TKT-ADMIN-VOID-SAAS-INVOICE (2026-08-31) ─────────────
# Shared void-invoice service. The tenant-side route
# (`invoices.delete`) and the super-admin shortcut on the SaaS
# subscriptions page both call this so behavior stays byte-identical.
# Before this existed the delete logic lived inline in
# routes/invoices.py:delete — extracted here so the ticket "run the
# same function from the admin page (not a new function)" is
# literally true, not just morally.
def void_invoice(invoice, reason, actor_id):
    """Void an invoice — the "delete" semantic used everywhere.

    * DRAFT (never posted a journal): hard-delete the row (items
      cascade via cascade='all, delete-orphan' on Invoice.items).
    * Anything else: issue a FULL refund (reverses AR/VAT/revenue/
      cash, restocks inventory, claws back commission), then stamp
      status=VOIDED + voided_at + voided_by_id + void_reason so the
      list surfaces it as "deleted" instead of "refunded" — the
      user's intent was delete, not customer-requested refund.

    Idempotent-ish: raises RuntimeError if the invoice is already
    VOIDED or REFUNDED so the caller can flash a friendly message
    without double-reversing the journal.

    Returns "deleted" (hard) or "voided" (soft) — callers can adjust
    the success flash based on the result.
    """
    from datetime import datetime as _dt

    if invoice.status in (InvoiceStatus.REFUNDED, InvoiceStatus.VOIDED):
        raise RuntimeError("الفاتورة معكوسة/ملغاة بالفعل")

    reason = (reason or "").strip() or "حذف الفاتورة"

    if invoice.status == InvoiceStatus.DRAFT:
        db.session.delete(invoice)
        db.session.commit()
        return "deleted"

    # Posted invoice — reverse via FULL refund, then relabel VOIDED.
    issue_refund(
        invoice, RefundType.FULL, reason=reason,
        created_by=actor_id, notify=False,
    )
    invoice.status = InvoiceStatus.VOIDED
    invoice.voided_at = _dt.utcnow()
    invoice.voided_by_id = actor_id
    invoice.void_reason = reason
    db.session.commit()
    return "voided"


def update_overdue_statuses(company_id):
    """Mark invoices as overdue if past due_date and unpaid."""
    today = date.today()
    invoices = Invoice.query.filter(
        Invoice.company_id == company_id,
        Invoice.status.in_([InvoiceStatus.SENT, InvoiceStatus.PARTIALLY_PAID]),
        Invoice.due_date < today,
    ).all()
    for inv in invoices:
        inv.status = InvoiceStatus.OVERDUE
    db.session.commit()
    return len(invoices)
