"""Vendor bill posting + payment logic.

A single bill can contain mixed line types (expense / fixed asset / inventory).
On post, the service:
  1. Validates each line's account matches its line_type
  2. Posts a single balanced journal (one debit row per line + one credit row for the funding source)
  3. Creates a FixedAsset record for each FIXED_ASSET line — linked back to the bill
  4. Sets the bill status to POSTED

The funding source determines the credit account:
  CASH   → 1110
  BANK   → 1120
  CREDIT → 2110 (Accounts Payable, vendor_id required)
"""
from datetime import date
from app import db
from app.models import (
    VendorBill, VendorBillItem, VendorBillPayment, VendorBillStatus,
    VendorBillPaymentMethod, BillLineType, FixedAsset, Account,
    PaymentMethod,
)
from app.services.ledger import post_journal, get_account_by_code, LedgerError


# Account code prefix that's valid for each line type
LINE_TYPE_ACCOUNT_PREFIX = {
    BillLineType.EXPENSE: "5",        # any 5xxx expense account
    BillLineType.FIXED_ASSET: "12",   # any 12xx fixed asset account (excl. 1290)
    BillLineType.INVENTORY: "1300",   # only the inventory account
}


def get_allowed_accounts_for_line_type(company_id, line_type):
    """Return the accounts allowed for a given line type."""
    if line_type == BillLineType.EXPENSE:
        return Account.query.filter(
            Account.company_id == company_id,
            Account.is_active.is_(True),
            Account.code.like("5%"),
        ).order_by(Account.code).all()
    if line_type == BillLineType.FIXED_ASSET:
        return Account.query.filter(
            Account.company_id == company_id,
            Account.is_active.is_(True),
            Account.code.like("12%"),
            Account.code != "1290",
        ).order_by(Account.code).all()
    if line_type == BillLineType.INVENTORY:
        return Account.query.filter(
            Account.company_id == company_id,
            Account.code == "1300",
        ).all()
    return []


def _validate_line_account(line, company_id):
    """Ensure the account picked actually matches the line type."""
    acc = db.session.get(Account, line.account_id)
    if not acc or acc.company_id != company_id:
        raise LedgerError(f"حساب البند غير صحيح: {line.description}")

    if line.line_type == BillLineType.EXPENSE and not acc.code.startswith("5"):
        raise LedgerError(f"البند '{line.description}' من نوع مصروف لكن الحساب ليس مصروفاً")
    if line.line_type == BillLineType.FIXED_ASSET:
        if not (acc.code.startswith("12") and acc.code != "1290"):
            raise LedgerError(f"البند '{line.description}' من نوع أصل ثابت لكن الحساب ليس أصلاً")
        if not line.useful_life_years or line.useful_life_years <= 0:
            raise LedgerError(f"العمر الإنتاجي مطلوب للأصل: {line.description}")
    if line.line_type == BillLineType.INVENTORY and acc.code != "1300":
        raise LedgerError(f"البند '{line.description}' من نوع مخزون لكن الحساب ليس حساب المخزون")


def post_vendor_bill(bill, created_by=None):
    """Post a vendor bill: validate, journal, create assets, set status.

    MARSOUD-PARTY-LEDGER-02 — when a vendor IS specified (regardless of
    payment method) we always route through the vendor's sub-account
    under 2110, then immediately settle with a second journal for
    CASH/BANK. That way every transaction shows up on the vendor's
    statement, and a cash bill's balance still nets to zero after the
    settlement leg.

    Journal pattern:
      CREDIT (was, no change):    Dr Expense+VAT  / Cr Vendor sub
      CASH/BANK WITH vendor:      Dr Expense+VAT  / Cr Vendor sub
                                  + Dr Vendor sub / Cr Cash|Bank   (settled now)
      CASH/BANK WITHOUT vendor:   Dr Expense+VAT  / Cr Cash|Bank   (legacy, vendor-less)
    """
    if bill.status != VendorBillStatus.DRAFT:
        raise LedgerError("الفاتورة ليست مسودة")
    if not bill.items:
        raise LedgerError("لا توجد بنود")

    # CREDIT method requires a vendor (we credit Accounts Payable for that vendor)
    if bill.payment_method == VendorBillPaymentMethod.CREDIT and not bill.vendor_id:
        raise LedgerError("لازم تختار المورد لو الدفع آجل")

    # Validate every line first
    for line in bill.items:
        _validate_line_account(line, bill.company_id)

    bill.recalc()

    # MARSOUD-PARTY-LEDGER-02 — if a vendor is selected, the credit
    # side of the bill always lands on the vendor's sub-account (so the
    # vendor statement is complete). The settlement to cash/bank is
    # posted as a SEPARATE journal afterwards. If there's no vendor
    # (legacy "petty cash" bills), we fall back to direct-to-funding.
    settle_to_funding = None  # set when we need a follow-up settlement leg
    settle_funding_label = None
    if bill.vendor_id:
        from app.services.subsidiary import party_ap_account
        credit_account = party_ap_account(bill)
        credit_label = f"على حساب المورد {bill.vendor.name}"
        # For CASH/BANK, prepare the settlement target
        if bill.payment_method == VendorBillPaymentMethod.CASH:
            settle_to_funding = get_account_by_code(bill.company_id, "1110")
            settle_funding_label = "نقدي"
        elif bill.payment_method == VendorBillPaymentMethod.BANK:
            for code in ("1124", "1121", "1122", "1123", "1125"):
                settle_to_funding = get_account_by_code(bill.company_id, code)
                if settle_to_funding:
                    break
            settle_funding_label = "بنك"
    else:
        # Legacy: no vendor selected, post straight to cash/bank
        if bill.payment_method == VendorBillPaymentMethod.CASH:
            credit_account = get_account_by_code(bill.company_id, "1110")
            credit_label = "نقدي (بدون مورد)"
        elif bill.payment_method == VendorBillPaymentMethod.BANK:
            credit_account = None
            for code in ("1124", "1121", "1122", "1123", "1125"):
                credit_account = get_account_by_code(bill.company_id, code)
                if credit_account:
                    break
            credit_label = "بنك (بدون مورد)"
        else:
            # CREDIT without vendor was already blocked above
            raise LedgerError("لا يوجد مورد لتسجيل الالتزام")
    if not credit_account:
        raise LedgerError("حساب الجهة المقابلة غير موجود في شجرة الحسابات")

    # Build journal lines: one debit per item + one credit for the total
    journal_lines = []
    for item in bill.items:
        journal_lines.append({
            "account_id": item.account_id,
            "debit": float(item.line_total),
            "credit": 0,
            "memo": f"{item.line_type.value}: {item.description}",
        })

    # MARSOUD-COA-REBUILD — input VAT (purchases) now posts to 1280
    # "Input VAT (Recoverable)", an asset. Used to mix into 2120 which
    # is the OUTPUT VAT liability — accounting bug fixed by the
    # rebuild. VAT settlement reads 2120 (output) − 1280 (input) =
    # net payable.
    tax_amount = float(bill.tax_amount or 0)
    if tax_amount > 0.001:
        vat = get_account_by_code(bill.company_id, "1280")
        if not vat:
            raise LedgerError(
                "حساب ضريبة المدخلات (1280) غير موجود — راجع شجرة الحسابات"
            )
        journal_lines.append({
            "account_id": vat.id,
            "debit": tax_amount,
            "credit": 0,
            "memo": f"ضريبة المدخلات على المشتريات — فاتورة {bill.number}",
        })

    journal_lines.append({
        "account_id": credit_account.id,
        "debit": 0,
        "credit": float(bill.total),
        "memo": f"إثبات الفاتورة {credit_label}",
    })

    vendor_desc = f" — {bill.vendor.name}" if bill.vendor else ""
    entry = post_journal(
        company_id=bill.company_id,
        description=f"فاتورة مشتريات {bill.number}{vendor_desc}",
        lines=journal_lines,
        entry_date=bill.issue_date,
        reference=f"VB-{bill.number}",
        currency=bill.currency,
        created_by=created_by,
        source_type="vendor_bill",
        source_id=bill.id,
    )

    bill.journal_entry_id = entry.id

    # MARSOUD-PARTY-LEDGER-02 — settlement leg for CASH/BANK WITH vendor:
    # the bill credited the vendor sub-account above; we now debit it and
    # credit cash/bank, so the vendor's running balance nets to zero
    # while keeping both transactions on his statement.
    if settle_to_funding is not None:
        post_journal(
            company_id=bill.company_id,
            description=(f"سداد فوري لفاتورة المورد {bill.number} "
                          f"({settle_funding_label})"),
            lines=[
                {"account_id": credit_account.id,
                 "debit": float(bill.total), "credit": 0,
                 "memo": f"سداد فاتورة {bill.number}"},
                {"account_id": settle_to_funding.id,
                 "debit": 0, "credit": float(bill.total),
                 "memo": f"دفع {settle_funding_label}"},
            ],
            entry_date=bill.issue_date,
            reference=f"VB-PAY-{bill.number}",
            currency=bill.currency,
            created_by=created_by,
            source_type="vendor_bill_payment",
            source_id=bill.id,
        )

    # ERP-01 — receive stock for every INVENTORY line. Runs inside the same
    # transaction as the journal so a failure rolls everything back together.
    from app.services.inventory import receive_stock, InventoryError
    # MARSOUD-UNIT-CONVERSION-01 — the cashier enters كرتونة but the
    # inventory engine tracks حبة. Convert BEFORE calling receive_stock,
    # and divide line_total by BASE qty so unit_cost lands per حبة (which
    # is what the moving-average expects going forward).
    from app.services.units import convert_to_base, UnitError
    for item in bill.items:
        if item.line_type != BillLineType.INVENTORY:
            continue
        if not item.variant_id or not item.warehouse_id:
            raise LedgerError(
                f"سطر مخزون يحتاج اختيار صنف ومخزن: {item.description}"
            )
        display_qty = float(item.quantity or 0)
        if display_qty <= 0:
            raise LedgerError(f"كمية الاستلام غير صالحة: {item.description}")
        # Look up the Product via the variant to resolve unit conversion.
        product = item.variant.product if item.variant else None
        try:
            base_qty_dec = convert_to_base(
                product, display_qty, unit_id=item.unit_id,
            )
        except UnitError as e:
            raise LedgerError(str(e))
        base_qty = float(base_qty_dec)
        # Unit cost per BASE unit — line_total covers the whole
        # purchase (however many كرتونة), divide by base_qty so
        # weighted-average adds pieces at the right per-piece cost.
        unit_cost = float(item.line_total or 0) / base_qty
        try:
            receive_stock(
                variant=item.variant, warehouse=item.warehouse,
                qty=base_qty, unit_cost=unit_cost,
                bill_id=bill.id, line_id=item.id,
                actor_id=created_by,
                journal_entry_id=entry.id,
            )
        except InventoryError as e:
            raise LedgerError(str(e))
        # Freeze the base conversion on the line for future report reads.
        item.base_quantity = base_qty

    # Create FixedAsset for each fixed-asset line
    for item in bill.items:
        if item.line_type != BillLineType.FIXED_ASSET:
            continue
        asset = FixedAsset(
            company_id=bill.company_id,
            name=item.description,
            purchase_date=bill.issue_date,
            cost=float(item.line_total),
            salvage_value=float(item.salvage_value or 0),
            useful_life_years=int(item.useful_life_years),
            account_id=item.account_id,
            vendor_id=bill.vendor_id,
            source_bill_id=bill.id,
        )
        db.session.add(asset)
        db.session.flush()
        item.created_asset_id = asset.id

    # If paid immediately (CASH/BANK), mark Paid; else POSTED waiting for payment(s)
    if bill.payment_method in (VendorBillPaymentMethod.CASH, VendorBillPaymentMethod.BANK):
        bill.paid_amount = bill.total
        bill.status = VendorBillStatus.PAID
    else:
        bill.status = VendorBillStatus.POSTED

    db.session.commit()
    try:
        from app.services.superadmin import log_platform_action
        log_platform_action("vendor_bill_posted",
                            target_company_id=bill.company_id,
                            actor_id=created_by,
                            details=f"#{bill.number} total={bill.total}")
    except Exception:
        pass
    # MARSOUD-ACTLOG-01
    try:
        from app.services.activity import log_action
        log_action(
            action_type="CREATE", entity_type="vendor_bill",
            entity_id=bill.id,
            entity_label=f"فاتورة مورد {bill.number}",
            company_id=bill.company_id,
            extra_data={"total": float(bill.total or 0),
                        "currency": bill.currency},
        )
    except Exception:
        pass
    return bill


def record_bill_payment(bill, amount, payment_method_id=None, created_by=None):
    """Record a payment against a posted (credit) vendor bill: Dr AP / Cr Cash|Bank."""
    if bill.payment_method != VendorBillPaymentMethod.CREDIT:
        raise LedgerError("الدفع غير مطلوب — الفاتورة مدفوعة بالفعل عند الإنشاء")
    if bill.status not in (VendorBillStatus.POSTED, VendorBillStatus.PARTIALLY_PAID, VendorBillStatus.OVERDUE):
        raise LedgerError(f"حالة الفاتورة لا تسمح بالدفع ({bill.status.value})")

    amount = float(amount)
    if amount <= 0:
        raise LedgerError("المبلغ يجب أن يكون أكبر من صفر")
    if amount > bill.balance + 0.01:
        raise LedgerError(f"المبلغ ({amount:.2f}) أكبر من المتبقي ({bill.balance:.2f})")

    pm = None
    receiving_account = None
    if payment_method_id:
        pm = db.session.get(PaymentMethod, int(payment_method_id))
        if not pm or pm.company_id != bill.company_id or not pm.is_active:
            raise LedgerError("طريقة دفع غير صالحة")
        receiving_account = pm.account
        method_label = pm.name_ar or pm.name
    else:
        receiving_account = get_account_by_code(bill.company_id, "1110")
        method_label = "نقدي"

    # MARSOUD-COA-REBUILD — AP debit hits the vendor's own sub-account.
    from app.services.subsidiary import party_ap_account
    ap = party_ap_account(bill)
    if not receiving_account or not ap:
        raise LedgerError("حسابات النقدية / الموردين غير موجودة")

    vendor_label = f" {bill.vendor.name}" if bill.vendor else ""
    entry = post_journal(
        company_id=bill.company_id,
        description=f"دفع لمورد{vendor_label} — فاتورة {bill.number} ({method_label})",
        lines=[
            {"account_id": ap.id, "debit": amount, "credit": 0},
            {"account_id": receiving_account.id, "debit": 0, "credit": amount},
        ],
        entry_date=date.today(),
        reference=f"VPMT-{bill.number}",
        currency=bill.currency,
        created_by=created_by,
        source_type="vendor_payment",
        source_id=bill.id,
    )

    payment = VendorBillPayment(
        bill_id=bill.id, amount=amount,
        payment_date=date.today(),
        payment_method_id=pm.id if pm else None,
        method=pm.name if pm else "cash",
        journal_entry_id=entry.id,
    )
    db.session.add(payment)

    bill.paid_amount = float(bill.paid_amount or 0) + amount
    bill.status = VendorBillStatus.PAID if bill.balance <= 0.01 else VendorBillStatus.PARTIALLY_PAID
    db.session.commit()
    # MARSOUD-ACTLOG-01
    try:
        from app.services.activity import log_action
        log_action(
            action_type="CREATE", entity_type="vendor_bill_payment",
            entity_id=payment.id if payment else None,
            entity_label=f"دفعة لفاتورة مورد {bill.number} — {amount:.2f}",
            company_id=bill.company_id,
            extra_data={"vendor_bill_id": bill.id, "amount": float(amount)},
        )
    except Exception:
        pass
    return payment


def post_vendor_bill_refund(bill, refund_type, amount=None,
                              reason=None, created_by=None):
    """MARSOUD-REFUNDS-01 — mirror of issue_refund() on the purchase side.

    Three shapes:
      FULL       → return everything at bill total, unwind inventory + VAT
      PARTIAL    → return a specified amount, requires bill was paid
                    to at least that amount (otherwise it's just a
                    vendor-balance reduction which the DEBIT_NOTE path
                    already handles cleanly).
      DEBIT_NOTE → open-ended balance the vendor owes us, applied against
                    a future bill from the same vendor. No inventory move.

    Journal (bill originally had CASH/BANK settlement — the settlement
    leg already sent money out; now we're getting it back):
      INVENTORY lines →  Cr 1300 (Inventory) at weighted-avg cost
      EXPENSE lines   →  Cr 5105 (Purchase Returns & Allowances)
      Input VAT       →  Cr 1280 (reverse the recoverable input VAT)
      Contra-side     →  Dr vendor sub (2110) OR Dr cash/bank
                          depending on whether it's a DEBIT_NOTE or
                          actual money coming back.
    """
    from app.models import VendorBillRefund, VendorRefundType, DebitNote
    from app.services.numbering import next_number
    from app.services.subsidiary import party_ap_account
    from app.services.inventory import record_purchase_return, InventoryError

    if bill.status not in (VendorBillStatus.PAID,
                            VendorBillStatus.POSTED,
                            VendorBillStatus.PARTIALLY_PAID,
                            # MARSOUD-VBILL-REFUND-STATUS — a partial
                            # refund now moves the bill to
                            # PARTIALLY_REFUNDED. Without this, the
                            # second partial refund on the same bill
                            # would start failing, which used to work.
                            VendorBillStatus.PARTIALLY_REFUNDED):
        raise LedgerError(
            f"لا يمكن عمل مرتجع لفاتورة بحالة {bill.status.value}"
        )

    total = float(bill.total or 0)
    paid = float(bill.paid_amount or 0)

    if refund_type == VendorRefundType.FULL:
        amount = total
    elif refund_type == VendorRefundType.PARTIAL:
        if not amount or float(amount) <= 0:
            raise LedgerError("حدد مبلغ المرتجع الجزئي")
        amount = float(amount)
        if amount > total + 0.01:
            raise LedgerError(
                "لا يمكن استرداد أكبر من قيمة الفاتورة الأصلية"
            )
    elif refund_type == VendorRefundType.DEBIT_NOTE:
        if not amount or float(amount) <= 0:
            raise LedgerError("حدد قيمة إشعار المدين")
        amount = float(amount)
    else:
        raise LedgerError("نوع المرتجع غير معروف")

    # Ratio the refund across VAT / net using the SAME ratio as the
    # original bill — matches issue_refund()'s handling for sales.
    tax_amount = float(bill.tax_amount or 0)
    if total > 0 and tax_amount > 0:
        tax_ratio = tax_amount / total
        refund_tax = round(amount * tax_ratio, 2)
        refund_net = round(amount - refund_tax, 2)
    else:
        refund_tax = 0.0
        refund_net = amount

    # Split refund_net across inventory-vs-expense in proportion to the
    # bill's own line-type mix (INVENTORY vs EXPENSE). Fixed-asset lines
    # are excluded per the ticket ("Not Included: Fixed asset refunds").
    inv_lines_total = sum(
        float(i.line_total or 0) for i in bill.items
        if i.line_type == BillLineType.INVENTORY
    )
    exp_lines_total = sum(
        float(i.line_total or 0) for i in bill.items
        if i.line_type == BillLineType.EXPENSE
    )
    base = inv_lines_total + exp_lines_total
    if base > 0:
        inv_share = round(refund_net * inv_lines_total / base, 2)
        exp_share = round(refund_net - inv_share, 2)
    else:
        inv_share = 0.0
        exp_share = refund_net

    # Build the CR (right) side of the journal.
    credit_lines = []
    if exp_share > 0.001:
        purchase_returns = get_account_by_code(bill.company_id, "5105")
        if not purchase_returns:
            raise LedgerError(
                "حساب مردودات المشتريات (5105) غير موجود — راجع شجرة الحسابات"
            )
        credit_lines.append({
            "account_id": purchase_returns.id, "debit": 0,
            "credit": exp_share, "memo": "مردودات مشتريات (مصروفات)",
        })
    inv_account = None
    if inv_share > 0.001:
        inv_account = get_account_by_code(bill.company_id, "1300")
        if not inv_account:
            raise LedgerError("حساب المخزون (1300) غير موجود")
        credit_lines.append({
            "account_id": inv_account.id, "debit": 0,
            "credit": inv_share, "memo": "خفض قيمة المخزون",
        })
    if refund_tax > 0.001:
        vat_input = get_account_by_code(bill.company_id, "1280")
        if not vat_input:
            raise LedgerError("حساب ضريبة المدخلات (1280) غير موجود")
        credit_lines.append({
            "account_id": vat_input.id, "debit": 0,
            "credit": refund_tax, "memo": "عكس ضريبة المدخلات",
        })

    # Build the DR (left) side. For DEBIT_NOTE we always land on the
    # vendor's AP sub-account (balance we can net against a future bill).
    # For FULL/PARTIAL: if the bill was actually paid, we take the money
    # back to cash/bank; otherwise we reduce the AP the same way as a
    # DEBIT_NOTE.
    ap = party_ap_account(bill) if bill.vendor_id else None
    debit_line = None
    receiving_account = None
    # MARSOUD-VBILL-REFUND-STATUS — these two were defined only inside
    # the `else` branch below and then read back through a
    # `'journal_ap_leg' in locals()` test. Initialise them up-front so
    # the flow is explicit and cash_return is readable afterwards (it
    # drives the paid_amount adjustment at the end of the function).
    cash_return = 0.0
    journal_ap_leg = None
    if refund_type == VendorRefundType.DEBIT_NOTE or paid < 0.01:
        if not ap:
            raise LedgerError(
                "المرتجع بدون سداد سابق يتطلب مورد مسجل"
            )
        debit_line = {
            "account_id": ap.id, "debit": amount, "credit": 0,
            "memo": (
                "إشعار مدين على المورد"
                if refund_type == VendorRefundType.DEBIT_NOTE
                else "خفض ذمم دائنة للمورد"
            ),
        }
    else:
        # Actual money coming back — pick the same funding side the bill
        # used (CASH → 1110; BANK → first bank account we find).
        if bill.payment_method == VendorBillPaymentMethod.CASH:
            receiving_account = get_account_by_code(bill.company_id, "1110")
        else:
            for code in ("1124", "1121", "1122", "1123", "1125"):
                receiving_account = get_account_by_code(bill.company_id, code)
                if receiving_account:
                    break
        if not receiving_account:
            raise LedgerError("حساب النقدية/البنك غير موجود")
        # Cap the cash return at what was actually paid so we don't
        # invent money — the balance goes to AP if we still owe.
        cash_return = min(amount, paid)
        credit_owed = amount - cash_return
        debit_line = {
            "account_id": receiving_account.id, "debit": cash_return,
            "credit": 0, "memo": "استرداد نقدي من المورد",
        }
        if credit_owed > 0.001 and ap:
            # Append the AP portion as a second debit line.
            journal_ap_leg = {
                "account_id": ap.id, "debit": credit_owed, "credit": 0,
                "memo": "خفض ذمم المورد بالمتبقي",
            }

    # Assemble the journal. Number the refund first so the reference
    # matches what shows in the ledger.
    ref_no = next_number(bill.company_id, "PURCHASE_REFUND")
    lines = credit_lines + [debit_line]
    if journal_ap_leg:
        lines.append(journal_ap_leg)

    vendor_name = bill.vendor.name if bill.vendor else "مورد"
    entry = post_journal(
        company_id=bill.company_id,
        description=(f"مرتجع مشتريات {ref_no} — "
                       f"فاتورة {bill.number} ({vendor_name})"),
        lines=lines,
        entry_date=date.today(),
        reference=ref_no,
        currency=bill.currency,
        created_by=created_by,
        source_type="vendor_bill_refund",
        source_id=bill.id,
    )

    vbr = VendorBillRefund(
        company_id=bill.company_id,
        number=ref_no,
        bill_id=bill.id,
        type=refund_type,
        amount=amount,
        reason=reason,
        journal_entry_id=entry.id,
    )
    db.session.add(vbr)
    db.session.flush()

    # Inventory unwind — only for FULL. A PARTIAL refund on an inventory
    # bill is ambiguous about WHICH line to unwind, so we punt and only
    # do the ledger side (the operator can adjust stock manually if
    # needed). Same policy as sales-side issue_refund().
    if refund_type == VendorRefundType.FULL and inv_account:
        for item in bill.items:
            if item.line_type != BillLineType.INVENTORY:
                continue
            if not item.variant_id or not item.warehouse_id:
                continue
            # MARSOUD-UNIT-CONVERSION-01 — return the same base qty
            # that was originally received. Fall back to display qty
            # for legacy rows written before this ticket.
            if item.base_quantity is not None:
                qty = float(item.base_quantity or 0)
            else:
                qty = float(item.quantity or 0)
            if qty <= 0:
                continue
            try:
                record_purchase_return(
                    variant=item.variant, warehouse=item.warehouse,
                    qty=qty, refund_id=vbr.id, line_id=item.id,
                    actor_id=created_by,
                    journal_entry_id=entry.id,
                )
            except InventoryError as e:
                raise LedgerError(str(e))

    # DEBIT_NOTE also opens a reusable balance against the vendor.
    if refund_type == VendorRefundType.DEBIT_NOTE and bill.vendor_id:
        dn = DebitNote(
            company_id=bill.company_id,
            vendor_id=bill.vendor_id,
            bill_id=bill.id,
            amount=amount,
            reason=reason,
        )
        db.session.add(dn)

    # MARSOUD-VBILL-REFUND-STATUS — the bill itself was never touched by
    # a refund: the journal was right but the row still looked live, so
    # its full value kept inflating the purchases totals and AP aging.
    #
    # Money that actually came back reduces what we've paid, mirroring
    # invoice.paid_amount -= amount in issue_refund() (invoicing.py).
    # cash_return is 0 for a DEBIT_NOTE and for an unpaid bill, so this
    # is a no-op in exactly the cases where no cash moved.
    if cash_return > 0.001:
        bill.paid_amount = float(bill.paid_amount or 0) - cash_return

    # Status, mirroring issue_refund() exactly. DEBIT_NOTE deliberately
    # leaves the status alone — the credit sits on the vendor's balance,
    # not on this bill's lifecycle (same as CREDIT_NOTE on the sales
    # side).
    if refund_type == VendorRefundType.FULL:
        bill.status = VendorBillStatus.REFUNDED
    elif refund_type == VendorRefundType.PARTIAL:
        bill.status = VendorBillStatus.PARTIALLY_REFUNDED

    db.session.commit()
    try:
        from app.services.activity import log_action
        log_action(
            action_type="CREATE", entity_type="vendor_bill_refund",
            entity_id=vbr.id,
            entity_label=f"مرتجع مشتريات {ref_no}",
            company_id=bill.company_id,
            extra_data={
                "vendor_bill_id": bill.id,
                "amount": float(amount),
                "type": refund_type.value,
            },
        )
    except Exception:
        pass
    return vbr


def update_overdue_vendor_bills(company_id):
    """Mark vendor bills as OVERDUE if past due_date and unpaid."""
    today = date.today()
    bills = VendorBill.query.filter(
        VendorBill.company_id == company_id,
        VendorBill.status.in_([VendorBillStatus.POSTED, VendorBillStatus.PARTIALLY_PAID]),
        VendorBill.due_date < today,
    ).all()
    for b in bills:
        b.status = VendorBillStatus.OVERDUE
    db.session.commit()
    return len(bills)
