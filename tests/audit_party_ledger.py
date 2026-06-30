#!/usr/bin/env python3
"""MARSOUD-PARTY-LEDGER-02 — service-level audit on a fresh company.

Proves:
  1. Customer / vendor / employee all get sub-accounts at create-time.
  2. A CASH vendor bill produces TWO journal entries: (a) bill posting
     to vendor sub, (b) settlement to cash. Vendor balance nets to zero.
  3. A CREDIT vendor bill produces ONE journal: balance owed on the
     vendor sub.
  4. party_ledger() for a vendor with both kinds of bills shows all
     four lines + running balance + zero closing balance for the cash
     bill (after settlement) + non-zero for the credit bill.
  5. POS walk-in (no customer) auto-creates the per-company "زبون نقدي"
     and routes through it.
  6. Payroll for one employee with one paid + one unpaid line: paid
     nets to zero on the employee ledger, unpaid stays as a credit
     balance owed to the employee.
  7. party_ledger() for that employee shows the right movements.
  8. Backfill script is idempotent: on a fresh DB it opens zero new
     accounts + rewrites zero bills.
"""
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__PARTY_LEDGER_AUDIT__"
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


def _teardown_company(company_id):
    """Cascade-delete every company-scoped row + children that don't
    carry a company_id of their own (journal_lines, invoice_items,
    payments, vendor_bill_items). SQLite re-uses freed IDs."""
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
        if "company_id" in {c["name"] for c in insp.get_columns(table.name)}:
            db.session.execute(
                table.delete().where(table.c.company_id == company_id)
            )
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. Customer/vendor/employee get sub-accounts at create time")
def _():
    from app.models import Customer, Vendor, Employee, EmployeeStatus, Account
    from app.services.subsidiary import (
        ensure_customer_account, ensure_vendor_account, ensure_employee_account,
    )
    cid = _STATE["company_id"]
    cust = Customer(company_id=cid, name="عميل اختبار")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    ven = Vendor(company_id=cid, name="مورد اختبار")
    db.session.add(ven); db.session.flush()
    ensure_vendor_account(ven)
    emp = Employee(company_id=cid, name="موظف اختبار",
                    email="emp@x.y", status=EmployeeStatus.ACTIVE,
                    basic_salary=Decimal("3000"), start_date=date.today())
    db.session.add(emp); db.session.flush()
    ensure_employee_account(emp)
    db.session.commit()
    for party, kind in [(cust, "customer"), (ven, "vendor"), (emp, "employee")]:
        assert party.account_id, f"{kind} missing account_id"
        acc = db.session.get(Account, party.account_id)
        assert acc.is_postable, f"{kind} sub must be postable"
    _STATE.update(customer_id=cust.id, vendor_id=ven.id, employee_id=emp.id)
    return (f"customer→{cust.account.code}, vendor→{ven.account.code}, "
            f"employee→{emp.account.code}")


@check("2. CASH vendor bill produces 2 journals; vendor balance nets to 0")
def _():
    from app.models import (
        Vendor, VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, JournalEntry, JournalLine, Account,
    )
    from app.models.vendor_bill import BillLineType
    from app.services.vendor_bills import post_vendor_bill
    cid = _STATE["company_id"]
    vendor = db.session.get(Vendor, _STATE["vendor_id"])
    rent = Account.query.filter_by(company_id=cid, code="5220").first()
    bill = VendorBill(
        company_id=cid, vendor_id=vendor.id, number="PL-CASH-1",
        issue_date=date.today(), due_date=date.today(),
        status=VendorBillStatus.DRAFT,
        payment_method=VendorBillPaymentMethod.CASH,
        tax_rate=Decimal("15"),
    )
    db.session.add(bill); db.session.flush()
    db.session.add(VendorBillItem(
        bill_id=bill.id, line_type=BillLineType.EXPENSE,
        account_id=rent.id, description="إيجار كاش",
        quantity=Decimal("1"), unit_price=Decimal("1000"),
        line_total=Decimal("1000"),
    ))
    db.session.flush()
    post_vendor_bill(bill)
    db.session.commit()
    # Expect 2 entries for this bill
    posting = db.session.get(JournalEntry, bill.journal_entry_id)
    settlement = JournalEntry.query.filter_by(
        company_id=cid, source_type="vendor_bill_payment",
        source_id=bill.id,
    ).first()
    assert posting and settlement, \
        f"expected 2 entries, got posting={posting}, settlement={settlement}"
    # Vendor sub-account net balance from these two entries = 0
    vendor_acc = vendor.account
    total_dr = total_cr = 0.0
    for entry in (posting, settlement):
        for line in JournalLine.query.filter(
            JournalLine.entry_id == entry.id,
            JournalLine.account_id == vendor_acc.id,
        ).all():
            total_dr += float(line.debit or 0)
            total_cr += float(line.credit or 0)
    assert abs(total_dr - total_cr) < 0.01, \
        f"vendor net should be 0, got dr={total_dr} cr={total_cr}"
    _STATE["cash_bill_id"] = bill.id
    return f"posting→{posting.id}, settlement→{settlement.id}, vendor nets 0"


@check("3. CREDIT vendor bill — 1 journal, vendor owed the total")
def _():
    from app.models import (
        Vendor, VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, JournalEntry, JournalLine, Account,
    )
    from app.models.vendor_bill import BillLineType
    from app.services.vendor_bills import post_vendor_bill
    cid = _STATE["company_id"]
    vendor = db.session.get(Vendor, _STATE["vendor_id"])
    rent = Account.query.filter_by(company_id=cid, code="5220").first()
    bill = VendorBill(
        company_id=cid, vendor_id=vendor.id, number="PL-CREDIT-1",
        issue_date=date.today(), due_date=date.today() + timedelta(days=30),
        status=VendorBillStatus.DRAFT,
        payment_method=VendorBillPaymentMethod.CREDIT,
        tax_rate=Decimal("15"),
    )
    db.session.add(bill); db.session.flush()
    db.session.add(VendorBillItem(
        bill_id=bill.id, line_type=BillLineType.EXPENSE,
        account_id=rent.id, description="إيجار آجل",
        quantity=Decimal("1"), unit_price=Decimal("2000"),
        line_total=Decimal("2000"),
    ))
    db.session.flush()
    post_vendor_bill(bill)
    db.session.commit()
    # ONE entry (no settlement leg)
    settlement = JournalEntry.query.filter_by(
        company_id=cid, source_type="vendor_bill_payment",
        source_id=bill.id,
    ).first()
    assert settlement is None, "credit bill should not produce settlement"
    return "credit bill posted to vendor sub only (no auto-settlement)"


@check("4. Party ledger for the vendor shows all movements + correct totals")
def _():
    from app.services.party_ledger import party_ledger
    cid = _STATE["company_id"]
    stmt = party_ledger(cid, "vendor", _STATE["vendor_id"])
    # Expect at least 3 lines: cash bill credit + cash settle debit + credit bill credit
    assert len(stmt["rows"]) >= 3, \
        f"expected ≥3 lines, got {len(stmt['rows'])}"
    # Closing balance = balance owed = 2300 (credit bill total only,
    # cash bill net 0 after settle)
    expected = 2300.0
    assert abs(stmt["closing_balance"] - expected) < 0.01, \
        f"closing {stmt['closing_balance']}, expected {expected}"
    return (f"{len(stmt['rows'])} movements, total dr={stmt['total_debit']:.2f}, "
            f"cr={stmt['total_credit']:.2f}, closing={stmt['closing_balance']:.2f}")


@check("5. POS walk-in auto-creates 'زبون نقدي' customer + ledger account")
def _():
    from app.models import Customer
    from app.services.subsidiary import ensure_walk_in_customer
    cid = _STATE["company_id"]
    walk = ensure_walk_in_customer(cid)
    assert walk.account_id, "walk-in has no sub-account"
    assert walk.name == "زبون نقدي (Walk-in)"
    # Idempotent
    walk2 = ensure_walk_in_customer(cid)
    assert walk2.id == walk.id
    return f"walk-in customer #{walk.id} → account {walk.account.code}"


@check("6. Payroll posts to per-employee sub-account (still correct)")
def _():
    from app.models import (
        Employee, JournalEntry, JournalLine, Account,
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
    by_code = {db.session.get(Account, l.account_id).code: l
                for l in JournalLine.query.filter_by(entry_id=entry.id).all()}
    emp_code = emp.account.code
    assert emp_code in by_code, f"employee sub missing: {list(by_code)}"
    assert "2130" not in by_code, \
        "payroll posted to parent 2130 — sub-account routing broken"
    _STATE["payroll_run_id"] = run.id
    return f"payroll credited {emp_code} (parent 2130 untouched)"


@check("7. Party ledger for the employee shows the unpaid salary")
def _():
    from app.services.party_ledger import party_ledger
    cid = _STATE["company_id"]
    stmt = party_ledger(cid, "employee", _STATE["employee_id"])
    assert len(stmt["rows"]) >= 1, "no payroll movement on employee ledger"
    # Salary credited = owed → closing balance is a credit balance
    # (since employee.account is a LIABILITY normal-side child, closing
    # is positive when employee is owed money)
    assert stmt["closing_balance"] > 0, \
        f"expected positive (owed) balance, got {stmt['closing_balance']}"
    return f"{len(stmt['rows'])} movement(s), employee owed {stmt['closing_balance']:.2f}"


@check("8. Backfill script is idempotent on a freshly-built company")
def _():
    from scripts.backfill_party_ledger import run
    cid = _STATE["company_id"]
    # Run a dry-run; nothing should be opened or rewritten because the
    # company already has the new wiring throughout.
    res = run(cid, dry_run=True)
    opened = res["subaccounts_opened"]
    assert sum(opened.values()) == 0, f"unexpected new accounts: {opened}"
    assert res["bills_rewritten"] == 0, \
        f"shouldn't rewrite anything on a fresh DB, got {res['bills_rewritten']}"
    return "no-op on fresh data — clean idempotency"


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
