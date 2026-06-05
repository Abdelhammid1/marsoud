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
    BillLineType.INVENTORY: "1140",   # only the inventory account
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
            Account.code == "1140",
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
    if line.line_type == BillLineType.INVENTORY and acc.code != "1140":
        raise LedgerError(f"البند '{line.description}' من نوع مخزون لكن الحساب ليس حساب المخزون")


def post_vendor_bill(bill, created_by=None):
    """Post a vendor bill: validate, journal, create assets, set status."""
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

    # Resolve funding-source account
    if bill.payment_method == VendorBillPaymentMethod.CASH:
        funding = get_account_by_code(bill.company_id, "1110")
        funding_label = "نقدي"
    elif bill.payment_method == VendorBillPaymentMethod.BANK:
        funding = get_account_by_code(bill.company_id, "1120")
        funding_label = "بنك"
    else:
        funding = get_account_by_code(bill.company_id, "2110")
        funding_label = "موردون دائنون"
    if not funding:
        raise LedgerError("حساب التمويل غير موجود في شجرة الحسابات")

    # Build journal lines: one debit per item + one credit for the total
    journal_lines = []
    for item in bill.items:
        journal_lines.append({
            "account_id": item.account_id,
            "debit": float(item.line_total),
            "credit": 0,
            "memo": f"{item.line_type.value}: {item.description}",
        })

    # Input VAT: debit 2120 (VAT Payable) so it nets against output VAT from sales.
    # The VAT report reads 2120 debits as "VAT paid to suppliers".
    tax_amount = float(bill.tax_amount or 0)
    if tax_amount > 0.001:
        vat = get_account_by_code(bill.company_id, "2120")
        if not vat:
            raise LedgerError("حساب ضريبة القيمة المضافة (2120) غير موجود")
        journal_lines.append({
            "account_id": vat.id,
            "debit": tax_amount,
            "credit": 0,
            "memo": f"ضريبة القيمة المضافة على المشتريات — فاتورة {bill.number}",
        })

    journal_lines.append({
        "account_id": funding.id,
        "debit": 0,
        "credit": float(bill.total),
        "memo": f"دفع {funding_label} لفاتورة المورد {bill.number}",
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

    ap = get_account_by_code(bill.company_id, "2110")
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
    return payment


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
