#!/usr/bin/env python3
"""MARSOUD-COA-REBUILD — end-to-end audit on a brand new test company.

Walks the full lifecycle Abdelhamid asked us to prove works:

  1. Seed a fresh company → new CoA, 98 accounts, 17 headers, payment
     methods point to a real bank leaf (not the 1120 header).
  2. CoA guard: verify_coa returns [] for the new company.
  3. Add a Customer → 1130-xxxxxx sub-account auto-created + linked.
  4. Add a Vendor → 2110-xxxxxx sub-account auto-created + linked.
  5. Add an Employee → 2130-xxxxxx sub-account auto-created + linked.
  6. Post an invoice → AR debit lands on the customer's sub-account,
     output VAT lands on 2120, revenue on 4100.
  7. Pay the invoice → cash hits 1110, customer's AR credits down.
  8. Issue a refund → debit lands on 4300 (Sales Returns), not 4100.
  9. Post a vendor bill → input VAT lands on 1280 (NOT 2120), AP
     credits the vendor's sub-account.
 10. Run a tiny payroll → salary expense to 5210, payable credit per
     employee sub-account (not parent 2130).
 11. VAT report computes net = output (2120) − input (1280).
 12. Attempt to manually post to a header (1130) → blocked with the
     clear is_postable error.

The fixture company is cleaned up at end of run so re-running is safe.
"""
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__COA_REBUILD_AUDIT__"


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ────────────────────────────────────────────────────────────
_STATE = {}


def _setup():
    from app.models import Company, Account, PaymentMethod
    # Wipe any leftover state from prior runs
    existing = Company.query.filter_by(name=COMPANY_NAME).first()
    if existing:
        _teardown_company(existing.id)
    c = Company(name=COMPANY_NAME, base_currency="SAR")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)
    _STATE["company_id"] = c.id


def _teardown_company(company_id):
    """Aggressively delete every company-scoped row + the company itself.

    Several "child" tables (journal_lines, invoice_items, vendor_bill_items,
    payments) have NO company_id — they're linked through a parent. The
    bulk reverse-walk skips them, so we MUST explicitly delete them
    before the parent rows go away. Otherwise SQLite reuses the freed
    IDs and orphan children attach to brand-new rows on the next run."""
    from app.models import (
        Company, JournalEntry, JournalLine, Invoice, InvoiceItem,
        Payment, VendorBill, VendorBillItem,
    )
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    # Step 1: wipe child rows that depend on company-scoped parents.
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
    # Step 2: walk every company-scoped table in reverse topological
    # order so FKs unwind cleanly.
    for table in reversed(db.metadata.sorted_tables):
        if "company_id" in {c["name"] for c in insp.get_columns(table.name)}:
            db.session.execute(
                table.delete().where(table.c.company_id == company_id)
            )
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()


# ─── 1-2: Seed + guard ─────────────────────────────────────────────────
@check("1. Fresh company seed creates 98 accounts (17 headers + 81 leaves)")
def _():
    from app.models import Account
    cid = _STATE["company_id"]
    accts = Account.query.filter_by(company_id=cid).all()
    headers = [a for a in accts if not a.is_postable]
    leaves = [a for a in accts if a.is_postable]
    assert len(accts) == 98, f"got {len(accts)} accounts"
    assert len(headers) == 17, f"got {len(headers)} headers"
    assert len(leaves) == 81, f"got {len(leaves)} leaves"
    return f"{len(accts)} accounts, {len(headers)} headers, {len(leaves)} leaves"


@check("2. verify_coa returns [] for the fresh company")
def _():
    from app.services.coa_guard import verify_coa
    missing = verify_coa(_STATE["company_id"])
    assert missing == [], f"missing: {missing}"
    return "no missing accounts"


@check("3. Payment methods wired to leaf accounts (cash 1110, bank 1124)")
def _():
    from app.models import PaymentMethod
    pms = PaymentMethod.query.filter_by(company_id=_STATE["company_id"]).all()
    codes = sorted([(p.name, p.account.code) for p in pms])
    expected = [("Bank Transfer", "1124"), ("Cash", "1110")]
    assert codes == expected, f"got {codes}"
    return f"{codes}"


# ─── 3-5: Sub-account auto-creation on party create ────────────────────
@check("4. Creating a customer opens 1130-xxxxxx sub-account")
def _():
    from app.models import Customer, Account
    from app.services.subsidiary import ensure_customer_account
    cid = _STATE["company_id"]
    c = Customer(company_id=cid, name="عميل اختبار 1",
                  email="c1@audit.local", phone="010")
    db.session.add(c); db.session.flush()
    ensure_customer_account(c)
    db.session.flush()
    assert c.account_id is not None, "account_id not set"
    acc = db.session.get(Account, c.account_id)
    assert acc.code.startswith("1130-"), f"unexpected code: {acc.code}"
    assert acc.is_postable is True, "sub-account must be postable"
    assert acc.parent.code == "1130", "wrong parent"
    _STATE["customer_id"] = c.id
    return f"customer #{c.id} → account {acc.code}"


@check("5. Creating a vendor opens 2110-xxxxxx sub-account")
def _():
    from app.models import Vendor, Account
    from app.services.subsidiary import ensure_vendor_account
    cid = _STATE["company_id"]
    v = Vendor(company_id=cid, name="مورد اختبار", email="v1@audit.local")
    db.session.add(v); db.session.flush()
    ensure_vendor_account(v)
    db.session.flush()
    acc = db.session.get(Account, v.account_id)
    assert acc.code.startswith("2110-"), f"unexpected code: {acc.code}"
    assert acc.parent.code == "2110"
    _STATE["vendor_id"] = v.id
    return f"vendor #{v.id} → account {acc.code}"


@check("6. Creating an employee opens 2130-xxxxxx sub-account")
def _():
    from app.models import Employee, Account, EmployeeStatus
    from app.services.subsidiary import ensure_employee_account
    cid = _STATE["company_id"]
    e = Employee(company_id=cid, name="موظف اختبار",
                  email="e1@audit.local", status=EmployeeStatus.ACTIVE,
                  basic_salary=Decimal("5000"), start_date=date.today())
    db.session.add(e); db.session.flush()
    ensure_employee_account(e)
    db.session.flush()
    acc = db.session.get(Account, e.account_id)
    assert acc.code.startswith("2130-"), f"unexpected code: {acc.code}"
    assert acc.parent.code == "2130"
    _STATE["employee_id"] = e.id
    return f"employee #{e.id} → account {acc.code}"


# ─── 7: Invoice posts to sub-accounts + correct VAT ────────────────────
@check("7. Posting an invoice debits customer sub-account, credits 4100 + 2120")
def _():
    from app.models import (
        Customer, Invoice, InvoiceItem, InvoiceStatus, JournalLine, Account,
    )
    from app.services.invoicing import post_invoice_to_ledger
    cid = _STATE["company_id"]
    customer = db.session.get(Customer, _STATE["customer_id"])
    inv = Invoice(
        company_id=cid, customer_id=customer.id,
        number="AUDIT-INV-001",
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="SAR",
        status=InvoiceStatus.DRAFT,
        subtotal=Decimal("1000"),
        tax_amount=Decimal("150"),
        total=Decimal("1150"),
        taxable_base=Decimal("1000"),
    )
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="خدمة استشارية",
        quantity=1, unit_price=Decimal("1000"),
        line_total=Decimal("1000"),
    ))
    db.session.flush()
    entry = post_invoice_to_ledger(inv)
    db.session.commit()
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    by_code = {}
    for l in lines:
        acc = db.session.get(Account, l.account_id)
        if acc is None:
            raise AssertionError(
                f"journal line {l.id} on entry {entry.id} references "
                f"missing account_id {l.account_id}"
            )
        by_code[acc.code] = l
    # Refresh customer in case the relationship cache was expired by
    # log_action committing between flush and assert.
    db.session.refresh(customer)
    cust_acc = db.session.get(Account, customer.account_id)
    customer_code = cust_acc.code
    assert customer_code in by_code, f"AR not on customer sub-account: {list(by_code)}"
    assert float(by_code[customer_code].debit) == 1150.0, \
        f"AR debit {by_code[customer_code].debit}"
    assert float(by_code["4100"].credit) == 1000.0, "revenue credit wrong"
    assert float(by_code["2120"].credit) == 150.0, "output VAT credit wrong"
    _STATE["invoice_id"] = inv.id
    return f"AR→{customer_code} 1150, 4100 cr 1000, 2120 cr 150"


# ─── 8: Payment hits 1110 (cash) and credits AR sub-account ────────────
@check("8. Recording a cash payment debits 1110 + credits customer AR")
def _():
    from app.models import (
        Invoice, Customer, JournalLine, JournalEntry, Account,
    )
    from app.services.invoicing import record_payment
    inv = db.session.get(Invoice, _STATE["invoice_id"])
    cust = db.session.get(Customer, _STATE["customer_id"])
    payment = record_payment(inv, 1150.0, method="cash")
    db.session.commit()
    entry = db.session.get(JournalEntry, payment.journal_entry_id)
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    by_code = {db.session.get(Account, l.account_id).code: l for l in lines}
    assert "1110" in by_code, f"cash missing: {list(by_code)}"
    assert float(by_code["1110"].debit) == 1150.0
    assert cust.account.code in by_code, "AR sub-account missing"
    assert float(by_code[cust.account.code].credit) == 1150.0
    return f"1110 dr 1150, {cust.account.code} cr 1150"


# ─── 9: Refund hits 4300 (sales returns), not 4100 ─────────────────────
@check("9. Issuing a refund debits 4300 (Sales Returns), not 4100")
def _():
    from app.models import Invoice, JournalLine, Account
    from app.services.invoicing import issue_refund
    from app.models.refund import RefundType
    inv = db.session.get(Invoice, _STATE["invoice_id"])
    refund = issue_refund(inv, RefundType.FULL)
    db.session.commit()
    # Find the refund's journal entry — newest one for this invoice
    from app.models import JournalEntry
    entry = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"],
        source_type="refund",
    ).order_by(JournalEntry.id.desc()).first()
    assert entry, "no refund journal entry"
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    by_code = {db.session.get(Account, l.account_id).code: l for l in lines}
    assert "4300" in by_code, f"sales-returns 4300 not in journal: {list(by_code)}"
    assert float(by_code["4300"].debit) > 0, "4300 should be debited"
    assert "4100" not in by_code, \
        "refund should NOT touch 4100 directly"
    return f"4300 dr {float(by_code['4300'].debit):.2f}, 4100 untouched"


# ─── 10: Vendor bill input VAT to 1280, not 2120 ────────────────────────
@check("10. Vendor bill posts input VAT to 1280 (NOT 2120)")
def _():
    from app.models import (
        Vendor, VendorBill, VendorBillItem, JournalLine, Account,
        VendorBillPaymentMethod, VendorBillStatus,
    )
    from app.services.vendor_bills import post_vendor_bill
    cid = _STATE["company_id"]
    vendor = db.session.get(Vendor, _STATE["vendor_id"])
    # Pick a real expense account leaf for the item account.
    rent_acc = Account.query.filter_by(
        company_id=cid, code="5220").first()
    bill = VendorBill(
        company_id=cid, vendor_id=vendor.id,
        number="AUDIT-BILL-001",
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        status=VendorBillStatus.DRAFT,
        payment_method=VendorBillPaymentMethod.CREDIT,
        subtotal=Decimal("2000"),
        tax_rate=Decimal("15"),    # recalc() uses rate × subtotal
        tax_amount=Decimal("300"),
        total=Decimal("2300"),
    )
    db.session.add(bill); db.session.flush()
    from app.models.vendor_bill import BillLineType
    db.session.add(VendorBillItem(
        bill_id=bill.id,
        line_type=BillLineType.EXPENSE,
        account_id=rent_acc.id,
        description="إيجار شهري",
        quantity=Decimal("1"), unit_price=Decimal("2000"),
        line_total=Decimal("2000"),
    ))
    db.session.flush()
    from app.models import JournalEntry
    post_vendor_bill(bill)
    db.session.commit()
    entry = db.session.get(JournalEntry, bill.journal_entry_id)
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    by_code = {db.session.get(Account, l.account_id).code: l for l in lines}
    assert "1280" in by_code, \
        f"input VAT (1280) missing: {list(by_code)}"
    assert float(by_code["1280"].debit) == 300.0, \
        f"1280 debit {by_code['1280'].debit}, expected 300"
    # 2120 is OUTPUT VAT — must NOT receive purchases' input VAT
    if "2120" in by_code:
        assert float(by_code["2120"].debit) == 0, \
            "input VAT leaked into output (2120)"
    # AP credit lands on the vendor's sub-account, not the parent 2110
    vendor_code = vendor.account.code
    assert vendor_code in by_code, f"vendor sub-account missing: {list(by_code)}"
    return f"1280 dr 300 (not 2120), AP→{vendor_code}"


# ─── 11: Payroll posts per-employee, not bulk to 2130 ──────────────────
@check("11. Payroll credits each employee's sub-account (not parent 2130)")
def _():
    from app.models import (
        Employee, PayrollRun, JournalLine, Account, JournalEntry,
    )
    from app.services.payroll import run_payroll
    cid = _STATE["company_id"]
    emp = db.session.get(Employee, _STATE["employee_id"])
    today = date.today()
    run = run_payroll(
        company_id=cid, year=today.year, month=today.month,
        line_inputs={emp.id: {"amount_paid": 0}},  # fully accrued
        send_emails=False,
    )
    db.session.commit()
    entry = db.session.get(JournalEntry, run.journal_entry_id)
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    by_code = {db.session.get(Account, l.account_id).code: l for l in lines}
    emp_code = emp.account.code
    assert emp_code in by_code, \
        f"employee sub-account missing: {list(by_code)}"
    # 2130 (parent) must NOT appear directly in the journal lines
    assert "2130" not in by_code, \
        "payroll posted to parent 2130 instead of sub-account"
    return f"per-employee credit to {emp_code}"


# ─── 12: VAT report nets output − input ─────────────────────────────────
@check("12. vat_report() returns net = output − input")
def _():
    from app.services.reports import vat_report
    cid = _STATE["company_id"]
    report = vat_report(cid)
    # We posted 150 output VAT (invoice) and 300 input VAT (bill).
    # Refund reverses some output VAT too. Just check the keys are
    # present + math is consistent.
    assert "output_vat" in report
    assert "input_vat" in report
    assert "net" in report
    computed = round(report["output_vat"] - report["input_vat"], 2)
    assert abs(computed - report["net"]) < 0.01, \
        f"net != output-input: {report}"
    return (f"output={report['output_vat']:.2f}, "
            f"input={report['input_vat']:.2f}, "
            f"net={report['net']:.2f}")


# ─── 13: Posting to a header is blocked with a clear error ─────────────
@check("13. Manually posting to header 1130 raises LedgerError(is_postable)")
def _():
    from app.models import Account
    from app.services.ledger import post_journal, LedgerError
    cid = _STATE["company_id"]
    ar_header = Account.query.filter_by(company_id=cid, code="1130").first()
    # Need a balanced counter-line on a real leaf
    cash = Account.query.filter_by(company_id=cid, code="1110").first()
    try:
        post_journal(
            company_id=cid,
            description="audit-test direct post to header",
            lines=[
                {"account_id": ar_header.id, "debit": 100, "credit": 0},
                {"account_id": cash.id, "debit": 0, "credit": 100},
            ],
        )
        db.session.rollback()
    except LedgerError as e:
        msg = str(e)
        assert "1130" in msg, f"error didn't mention 1130: {msg}"
        assert "رئيسي" in msg or "header" in msg.lower(), \
            f"error message unclear: {msg}"
        return f"blocked correctly: {msg[:60]}"
    raise AssertionError("post_journal accepted a header — guard broken!")


# ─── Run ────────────────────────────────────────────────────────────────
def main():
    app = create_app()
    passed = failed = 0
    failures = []
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}  ⇒ {result}")
                    passed += 1
                except Exception as e:
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failures.append((label, repr(e)))
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            # Always clean up — never leave a fixture company sitting around
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
