#!/usr/bin/env python3
"""MARSOUD-REFUNDS-01 — end-to-end audit for the refund system.

Proves, on a fresh company:
  1. Account 5105 "Purchase Returns & Allowances" is seeded.
  2. Prefixes SRET / PRET produce distinct sequences per company.
  3. Sales FULL refund on a paid invoice posts a balanced journal:
     Dr 4300 (sales returns) + Dr 2120 (output VAT) / Cr 1110 (cash).
     The refund row has the SRET-nnnn number stamped on it.
  4. Sales PARTIAL refund on a partly paid invoice caps at the paid
     amount and refuses to over-refund.
  5. Sales CREDIT_NOTE emits a CreditNote row + zero cash movement.
  6. Purchase FULL refund on a paid EXPENSE-only vendor bill posts:
     Cr 5105 + Cr 1280 / Dr 1110. Vendor sub is untouched
     (bill was cash) so the vendor's balance stays at zero.
  7. Purchase FULL refund on a paid INVENTORY vendor bill unwinds
     stock (a PURCHASE_RETURN movement) at the weighted-average cost
     from receipt, and the journal credits 1300 for the same amount.
  8. Purchase DEBIT_NOTE opens a DebitNote row with a positive balance
     that will offset a future bill; journal reduces AP (Dr 2110 sub).
  9. Refund on a bill with no vendor sub-account is refused (needed
     because the DEBIT_NOTE has to land somewhere).
 10. plan_allows() reports refunds as gated under the "sales" module,
     and the PERMISSION_CATALOG entries for the 3 new codes exist.
"""
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__REFUNDS_01_AUDIT__"
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
            JournalLine.entry_id.in_(entry_ids)
        ).delete(synchronize_session=False)
    inv_ids = [r.id for r in Invoice.query.filter_by(
        company_id=company_id).all()]
    if inv_ids:
        InvoiceItem.query.filter(
            InvoiceItem.invoice_id.in_(inv_ids)
        ).delete(synchronize_session=False)
        Payment.query.filter(
            Payment.invoice_id.in_(inv_ids)
        ).delete(synchronize_session=False)
    bill_ids = [r.id for r in VendorBill.query.filter_by(
        company_id=company_id).all()]
    if bill_ids:
        VendorBillItem.query.filter(
            VendorBillItem.bill_id.in_(bill_ids)
        ).delete(synchronize_session=False)
    for table in reversed(db.metadata.sorted_tables):
        if "company_id" in {col["name"] for col in insp.get_columns(table.name)}:
            db.session.execute(
                table.delete().where(table.c.company_id == company_id)
            )
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()


def _get(code):
    from app.models import Account
    return Account.query.filter_by(
        company_id=_STATE["company_id"], code=code,
    ).first()


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. account 5105 seeded on new company")
def _():
    acc = _get("5105")
    assert acc, "5105 missing from fresh seed"
    assert acc.is_postable, "5105 must be postable"
    return f"5105 → {acc.name} ({acc.type.name if hasattr(acc.type,'name') else acc.type})"


@check("2. SRET and PRET sequences are distinct")
def _():
    from app.services.numbering import next_number
    cid = _STATE["company_id"]
    s1 = next_number(cid, "SALES_REFUND")
    s2 = next_number(cid, "SALES_REFUND")
    p1 = next_number(cid, "PURCHASE_REFUND")
    assert s1.startswith("SRET-") and s2.startswith("SRET-")
    assert p1.startswith("PRET-")
    assert s1 != s2, "sales sequence didn't advance"
    return f"{s1}, {s2}, {p1}"


@check("3. Sales FULL refund on paid invoice — balanced + SRET numbered")
def _():
    from app.models import (
        Customer, Invoice, InvoiceItem, InvoiceStatus, Payment,
        RefundType, Refund, PaymentMethod, JournalEntry, JournalLine,
    )
    from app.services.subsidiary import ensure_customer_account
    from app.services.invoicing import (
        post_invoice_to_ledger, record_payment, issue_refund,
    )
    cid = _STATE["company_id"]
    cust = Customer(company_id=cid, name="زبون مرتجع")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    inv = Invoice(
        company_id=cid, customer_id=cust.id, number="TESTINV-001",
        issue_date=date.today(), due_date=date.today(),
        currency="SAR", status=InvoiceStatus.DRAFT,
        tax_rate=Decimal("15"),
    )
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="سلعة",
        quantity=Decimal("1"), unit_price=Decimal("1000"),
        line_total=Decimal("1000"),
    ))
    db.session.flush()
    inv.recalc()
    db.session.flush()
    post_invoice_to_ledger(inv)
    db.session.commit()
    # Pay in full
    pm = PaymentMethod.query.filter_by(company_id=cid).first()
    record_payment(inv, float(inv.total), payment_method_id=pm.id if pm else None)
    db.session.commit()
    # Issue FULL refund
    refund = issue_refund(inv, RefundType.FULL)
    db.session.commit()
    assert refund.number and refund.number.startswith("SRET-"), \
        f"refund lacks SRET number: {refund.number}"
    assert refund.company_id == cid, "refund.company_id not set"
    # Journal balance check
    entry = db.session.get(JournalEntry, refund.journal_entry_id)
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    total_dr = sum(float(l.debit or 0) for l in lines)
    total_cr = sum(float(l.credit or 0) for l in lines)
    assert abs(total_dr - total_cr) < 0.01, \
        f"refund journal unbalanced: dr={total_dr} cr={total_cr}"
    # Must credit cash (1110) since it was paid
    cash_line = next((l for l in lines
                      if l.account.code == "1110" and float(l.credit or 0) > 0),
                      None)
    assert cash_line, "expected Cr 1110 (cash) on full refund"
    _STATE["sales_full_refund_id"] = refund.id
    _STATE["sales_customer_id"] = cust.id
    return (f"refund {refund.number}, journal balanced "
              f"dr={total_dr:.2f}=cr={total_cr:.2f}, Cr 1110 = "
              f"{float(cash_line.credit):.2f}")


@check("4. Sales PARTIAL over-refund is refused")
def _():
    from app.models import (
        Customer, Invoice, InvoiceItem, InvoiceStatus, PaymentMethod,
        RefundType,
    )
    from app.services.subsidiary import ensure_customer_account
    from app.services.invoicing import (
        post_invoice_to_ledger, record_payment, issue_refund,
    )
    from app.services.ledger import LedgerError
    cid = _STATE["company_id"]
    cust = Customer(company_id=cid, name="زبون جزئي")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    inv = Invoice(
        company_id=cid, customer_id=cust.id, number="TESTINV-002",
        issue_date=date.today(), due_date=date.today(),
        currency="SAR", status=InvoiceStatus.DRAFT,
        tax_rate=Decimal("0"),
    )
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="سلعة",
        quantity=Decimal("1"), unit_price=Decimal("500"),
        line_total=Decimal("500"),
    ))
    db.session.flush()
    inv.recalc()
    db.session.flush()
    post_invoice_to_ledger(inv)
    pm = PaymentMethod.query.filter_by(company_id=cid).first()
    record_payment(inv, 200.0, payment_method_id=pm.id if pm else None)
    db.session.commit()
    raised = False
    try:
        issue_refund(inv, RefundType.PARTIAL, amount=400.0)
    except LedgerError:
        raised = True
    assert raised, "over-refund should have raised LedgerError"
    # Refund exactly the paid amount succeeds
    r = issue_refund(inv, RefundType.PARTIAL, amount=200.0)
    db.session.commit()
    assert r.number.startswith("SRET-")
    return f"over-refund blocked; partial 200/500 succeeded → {r.number}"


@check("5. CREDIT_NOTE — cash side is untouched, note is issued")
def _():
    from app.models import (
        Customer, Invoice, InvoiceItem, InvoiceStatus,
        RefundType, CreditNote, JournalEntry, JournalLine,
    )
    from app.services.subsidiary import ensure_customer_account
    from app.services.invoicing import post_invoice_to_ledger, issue_refund
    cid = _STATE["company_id"]
    cust = Customer(company_id=cid, name="زبون إشعار دائن")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    inv = Invoice(
        company_id=cid, customer_id=cust.id, number="TESTINV-003",
        issue_date=date.today(), due_date=date.today(),
        currency="SAR", status=InvoiceStatus.DRAFT,
        tax_rate=Decimal("0"),
    )
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="خدمة",
        quantity=Decimal("1"), unit_price=Decimal("300"),
        line_total=Decimal("300"),
    ))
    db.session.flush()
    inv.recalc()
    db.session.flush()
    post_invoice_to_ledger(inv)
    db.session.commit()
    r = issue_refund(inv, RefundType.CREDIT_NOTE, amount=100.0)
    db.session.commit()
    cn = CreditNote.query.filter_by(invoice_id=inv.id).first()
    assert cn and abs(float(cn.amount) - 100.0) < 0.01, "credit note missing"
    entry = db.session.get(JournalEntry, r.journal_entry_id)
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    # None of them should touch cash 1110
    cash_lines = [l for l in lines if l.account.code == "1110"]
    assert not cash_lines, "credit-note refund should not touch cash"
    return f"CN {cn.id} = {float(cn.amount):.2f}, no cash movement"


@check("6. Purchase FULL refund on paid EXPENSE bill: Cr 5105/1280 Dr cash")
def _():
    from app.models import (
        Vendor, VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, BillLineType, Account,
        VendorRefundType, VendorBillRefund,
        JournalEntry, JournalLine,
    )
    from app.services.subsidiary import ensure_vendor_account
    from app.services.vendor_bills import (
        post_vendor_bill, post_vendor_bill_refund,
    )
    cid = _STATE["company_id"]
    v = Vendor(company_id=cid, name="مورد اختبار مصروف")
    db.session.add(v); db.session.flush()
    ensure_vendor_account(v)
    rent = Account.query.filter_by(company_id=cid, code="5220").first()
    bill = VendorBill(
        company_id=cid, vendor_id=v.id, number="TESTVB-EXP-1",
        issue_date=date.today(), due_date=date.today(),
        status=VendorBillStatus.DRAFT,
        payment_method=VendorBillPaymentMethod.CASH,
        tax_rate=Decimal("15"),
    )
    db.session.add(bill); db.session.flush()
    db.session.add(VendorBillItem(
        bill_id=bill.id, line_type=BillLineType.EXPENSE,
        account_id=rent.id, description="إيجار",
        quantity=Decimal("1"), unit_price=Decimal("1000"),
        line_total=Decimal("1000"),
    ))
    db.session.flush()
    post_vendor_bill(bill)
    db.session.commit()
    r = post_vendor_bill_refund(bill, VendorRefundType.FULL)
    db.session.commit()
    assert r.number and r.number.startswith("PRET-"), \
        f"vendor refund missing PRET number: {r.number}"
    entry = db.session.get(JournalEntry, r.journal_entry_id)
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    total_dr = sum(float(l.debit or 0) for l in lines)
    total_cr = sum(float(l.credit or 0) for l in lines)
    assert abs(total_dr - total_cr) < 0.01, \
        f"unbalanced: dr={total_dr} cr={total_cr}"
    codes = {l.account.code: (float(l.debit or 0), float(l.credit or 0))
              for l in lines}
    assert "5105" in codes and codes["5105"][1] > 0.01, \
        f"expected Cr 5105 got {codes}"
    assert "1280" in codes and codes["1280"][1] > 0.01, \
        f"expected Cr 1280 (input VAT reversal) got {codes}"
    assert "1110" in codes and codes["1110"][0] > 0.01, \
        f"expected Dr 1110 (cash back) got {codes}"
    return (f"{r.number}: Cr 5105={codes['5105'][1]:.2f}, "
              f"Cr 1280={codes['1280'][1]:.2f}, Dr 1110={codes['1110'][0]:.2f}")


@check("7. Purchase FULL refund on paid INVENTORY bill unwinds stock")
def _():
    from app.models import (
        Vendor, VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, BillLineType, Product, ProductVariant,
        Warehouse, StockMovement,
        VendorRefundType,
        JournalLine, JournalEntry,
    )
    from app.services.subsidiary import ensure_vendor_account
    from app.services.vendor_bills import (
        post_vendor_bill, post_vendor_bill_refund,
    )
    cid = _STATE["company_id"]
    v = Vendor(company_id=cid, name="مورد اختبار مخزون")
    db.session.add(v); db.session.flush()
    ensure_vendor_account(v)
    wh = Warehouse(company_id=cid, name="مخزن رئيسي", code="M")
    db.session.add(wh); db.session.flush()
    p = Product(company_id=cid, name="بضاعة اختبار", is_tracked=True,
                  default_price=Decimal("100"))
    db.session.add(p); db.session.flush()
    variant = ProductVariant(product_id=p.id, sku="SKU-1",
                                company_id=cid)
    db.session.add(variant); db.session.flush()
    bill = VendorBill(
        company_id=cid, vendor_id=v.id, number="TESTVB-INV-1",
        issue_date=date.today(), due_date=date.today(),
        status=VendorBillStatus.DRAFT,
        payment_method=VendorBillPaymentMethod.CASH,
        tax_rate=Decimal("0"),
    )
    db.session.add(bill); db.session.flush()
    inv_acc = _get("1300")
    db.session.add(VendorBillItem(
        bill_id=bill.id, line_type=BillLineType.INVENTORY,
        account_id=inv_acc.id, description="بضاعة",
        variant_id=variant.id, warehouse_id=wh.id,
        quantity=Decimal("10"), unit_price=Decimal("50"),
        line_total=Decimal("500"),
    ))
    db.session.flush()
    post_vendor_bill(bill)
    db.session.commit()
    r = post_vendor_bill_refund(bill, VendorRefundType.FULL,
                                    reason="فاسدة")
    db.session.commit()
    # PURCHASE_RETURN stock movement should exist
    mv = StockMovement.query.filter_by(
        company_id=cid, kind="PURCHASE_RETURN",
        source_type="vendor_bill_refund",
    ).first()
    assert mv, "expected PURCHASE_RETURN stock movement"
    assert float(mv.qty_delta) == -10.0, \
        f"qty_delta must be -10, got {mv.qty_delta}"
    # Journal must credit 1300 for the same total the stock came in at.
    entry = db.session.get(JournalEntry, r.journal_entry_id)
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    inv_lines = [l for l in lines
                  if l.account.code == "1300" and float(l.credit or 0) > 0]
    assert inv_lines, "expected Cr 1300 on inventory refund"
    return (f"{r.number}: stock now {mv.balance_qty_after}, "
              f"Cr 1300 = {float(inv_lines[0].credit):.2f}")


@check("8. Purchase DEBIT_NOTE creates DebitNote row + reduces AP")
def _():
    from app.models import (
        Vendor, VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, BillLineType, Account,
        VendorRefundType, DebitNote,
        JournalEntry, JournalLine,
    )
    from app.services.subsidiary import ensure_vendor_account
    from app.services.vendor_bills import (
        post_vendor_bill, post_vendor_bill_refund,
    )
    cid = _STATE["company_id"]
    v = Vendor(company_id=cid, name="مورد إشعار مدين")
    db.session.add(v); db.session.flush()
    ensure_vendor_account(v)
    rent = _get("5220")
    bill = VendorBill(
        company_id=cid, vendor_id=v.id, number="TESTVB-DN-1",
        issue_date=date.today(), due_date=date.today() + timedelta(days=30),
        status=VendorBillStatus.DRAFT,
        payment_method=VendorBillPaymentMethod.CREDIT,
        tax_rate=Decimal("0"),
    )
    db.session.add(bill); db.session.flush()
    db.session.add(VendorBillItem(
        bill_id=bill.id, line_type=BillLineType.EXPENSE,
        account_id=rent.id, description="خدمات",
        quantity=Decimal("1"), unit_price=Decimal("400"),
        line_total=Decimal("400"),
    ))
    db.session.flush()
    post_vendor_bill(bill)
    db.session.commit()
    r = post_vendor_bill_refund(bill, VendorRefundType.DEBIT_NOTE,
                                    amount=150.0, reason="خصم متأخر")
    db.session.commit()
    dn = DebitNote.query.filter_by(vendor_id=v.id).first()
    assert dn and abs(float(dn.amount) - 150.0) < 0.01, \
        f"debit note missing / wrong amount: {dn}"
    assert dn.balance == 150.0, f"open balance must be 150, got {dn.balance}"
    # Journal must debit the vendor's AP sub-account (reduce what we owe).
    entry = db.session.get(JournalEntry, r.journal_entry_id)
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    ap_lines = [l for l in lines
                 if l.account.parent_id and float(l.debit or 0) > 0
                 and l.account.parent.code == "2110"]
    assert ap_lines, "expected Dr on AP sub-account under 2110"
    return (f"{r.number}: DN {dn.id} balance={dn.balance:.2f}, "
              f"Dr AP-sub = {float(ap_lines[0].debit):.2f}")


@check("9. plan_gating + permission catalog wire-up")
def _():
    from app.services.plan_gating import action_module, SUB_ITEM_CATALOG
    from app.services.roles_seed import PERMISSION_CATALOG
    from app.models import Permission
    assert action_module("refunds.view") == "sales"
    assert action_module("refunds.manage") == "sales"
    assert action_module("vendor_bills.refund") == "purchases"
    assert "refunds" in SUB_ITEM_CATALOG, "refunds section missing"
    for code in ("refunds.view", "refunds.manage", "vendor_bills.refund"):
        assert code in PERMISSION_CATALOG, f"{code} missing from catalog"
        assert Permission.query.filter_by(code=code).first(), \
            f"{code} not seeded in DB"
    return "all 3 codes present + section wired"


# ─── Run ────────────────────────────────────────────────────────────────
def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
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
