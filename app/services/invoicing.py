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


def post_invoice_to_ledger(invoice, created_by=None):
    """Dr Accounts Receivable / Cr Revenue + Cr VAT Payable."""
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

    # Revenue is credited at the taxable_base (subtotal AFTER invoice-level discount).
    # This keeps the journal balanced: Dr AR (total) = Cr Revenue (net) + Cr VAT (tax).
    revenue_credit = float(invoice.taxable_base if invoice.taxable_base else invoice.subtotal)
    lines = [
        {"account_id": ar.id, "debit": float(invoice.total), "credit": 0, "memo": f"فاتورة {invoice.number}"},
        {"account_id": revenue.id, "debit": 0, "credit": revenue_credit, "memo": "إيراد (صافي بعد الخصم)"},
    ]
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

    # MARSOUD-ACTLOG-01 — record the invoice posting as a CREATE action.
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

    return entry


def _apply_inventory_side_for_invoice(invoice, entry, created_by):
    """For each tracked invoice line:
      - resolve variant + warehouse (default to company's main warehouse)
      - call record_sale() to drop stock + snapshot the cost
      - aggregate the cost basis, then post a single COGS journal
        (Dr 5100 / Cr 1140) covering the whole invoice.
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

    total_cogs = 0.0
    for item, variant, warehouse in tracked:
        qty = float(item.quantity or 0)
        if qty <= 0:
            continue
        try:
            unit_cost = record_sale(
                variant=variant, warehouse=warehouse, qty=qty,
                invoice_id=invoice.id, line_id=item.id,
                actor_id=created_by,
            )
        except InventoryError as e:
            raise LedgerError(str(e))
        # Backfill the columns on the line so refunds + reports read it later.
        item.variant_id = variant.id
        item.warehouse_id = warehouse.id
        item.unit_cost_at_sale = unit_cost
        total_cogs += unit_cost * qty

    if total_cogs > 0.001:
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


def record_payment(invoice, amount, payment_date=None, method=None, payment_method_id=None, created_by=None, notify=True):
    """Record a payment posting Dr <method.account> / Cr AR. Resolves the receiving
    account either from a PaymentMethod row (preferred) or from the legacy
    'cash'/'bank' string.
    """
    amount = float(amount)
    if amount <= 0:
        raise LedgerError("المبلغ يجب أن يكون أكبر من صفر")
    if amount > invoice.balance + 0.01:
        raise LedgerError(f"المبلغ ({amount:.2f}) أكبر من الرصيد المتبقي ({invoice.balance:.2f})")

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
        amount=amount,
        payment_date=payment_date or date.today(),
        payment_method_id=pm.id if pm else None,
        method=(pm.name if pm else method),
        journal_entry_id=entry.id,
    )
    db.session.add(payment)

    invoice.paid_amount = float(invoice.paid_amount or 0) + amount
    is_full = invoice.paid_amount >= float(invoice.total) - 0.01
    if is_full:
        invoice.status = InvoiceStatus.PAID
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
        qty = float(item.quantity or 0)
        if qty <= 0:
            continue
        cost = float(item.unit_cost_at_sale or 0)
        try:
            record_return(
                variant=variant, warehouse=warehouse, qty=qty,
                unit_cost_at_sale=cost,
                refund_id=refund.id, line_id=item.id,
                actor_id=created_by,
            )
        except InventoryError as e:
            raise LedgerError(str(e))
        total_restock_cost += cost * qty

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
            reference=f"CN-{invoice.number}",
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
                reference=f"REF-{invoice.number}",
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
                reference=f"REF-{invoice.number}",
                currency=invoice.currency,
                created_by=created_by,
                source_type="refund",
                source_id=invoice.id,
            )

    refund = Refund(
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
