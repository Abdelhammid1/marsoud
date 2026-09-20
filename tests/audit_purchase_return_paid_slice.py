#!/usr/bin/env python3
"""MARSOUD-PRET-PAID-SLICE-01 (Abdelhamid 2026-09-20) — purchase
return on a partially-paid vendor bill must split the reversal JE
between Cash (for the actually-paid slice) and AP (for the unpaid
balance).  Same bug pattern as MARSOUD-VOID-PAID-SLICE-01 on the
sales side.

The code-side fix landed on 2026-07-02 (commit e5fe1c7) but had no
dedicated audit.  A production incident on VB-0088 (2500 total,
1501 paid across 3 payments, 999 ghost balance on the vendor) was
reconciled by hand via JE-0344 and the ticket explicitly asked
for regression coverage so it never regresses.

Post-fix, `post_vendor_bill_refund(FULL)` produces:
    Cr Inventory/PurchaseReturns/InputVAT   (bill.total split)
      Dr Cash     min(amount, paid)
      Dr AP       amount - min(amount, paid)
plus `bill.paid_amount -= cash_return`.

Six checks lifted from the ticket ACs:
  1. FULL void of unpaid bill        → cash untouched, AP debited fully.
  2. FULL void of fully-paid bill    → cash debited for total, AP untouched.
  3. FULL void of partial-paid (one payment)   → cash + AP split.
  4. FULL void of partial-paid (multi-payment, VB-0088 pattern) → same.
  5. Balance guard on every purchase-return JE.
  6. bill.paid_amount is never negative across any voided bill.
"""
import sys
from pathlib import Path
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"),
                     {"c": company_id})
        # Orphan-prone child tables — no `company_id` column, so the
        # generic loop skips them.  Left behind, they re-attach to
        # reused parent ids in the next run and produce ghost rows
        # (7 items on a fresh bill, 20 lines on a fresh JE...).  Clean
        # each one explicitly against the current parent set BEFORE
        # the parent is deleted.
        conn.execute(text(
            "DELETE FROM journal_lines WHERE entry_id IN "
            "(SELECT id FROM journal_entries WHERE company_id = :c)"
        ), {"c": company_id})
        conn.execute(text(
            "DELETE FROM vendor_bill_items WHERE bill_id IN "
            "(SELECT id FROM vendor_bills WHERE company_id = :c)"
        ), {"c": company_id})
        conn.execute(text(
            "DELETE FROM vendor_bill_payments WHERE bill_id IN "
            "(SELECT id FROM vendor_bills WHERE company_id = :c)"
        ), {"c": company_id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'prps-%@x.test'"))


def _setup():
    from app.models import Company, User, user_companies, Vendor
    from werkzeug.security import generate_password_hash

    for name in ("__PRET_PAID_SLICE__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__PRET_PAID_SLICE__", base_currency="SAR",
                 vat_rate=0)
    db.session.add(a); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(a.id)

    u = User(email="prps-owner@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="prps-owner")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=a.id, role="owner"))
    vendor = Vendor(company_id=a.id, name="PRPS-Vendor")
    db.session.add(vendor); db.session.commit()
    _STATE.update(a_id=a.id, owner_id=u.id, vendor_id=vendor.id)


def _fresh_bill(gross):
    """Post a fresh CREDIT vendor bill with one INVENTORY line at
    `gross` (no VAT, so gross == total).  Returns the VendorBill."""
    from app.models import (
        VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, BillLineType,
    )
    from app.services.vendor_bills import post_vendor_bill
    from app.services.numbering import next_number
    number = next_number(_STATE["a_id"], "VENDOR_BILL")
    bill = VendorBill(
        company_id=_STATE["a_id"],
        number=number,
        vendor_id=_STATE["vendor_id"],
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="SAR", tax_rate=0,
        payment_method=VendorBillPaymentMethod.CREDIT,
        status=VendorBillStatus.DRAFT,
    )
    db.session.add(bill); db.session.flush()
    # An EXPENSE line needs an expense account.  Cost of Sales (5100)
    # is guaranteed to be seeded by seed_default_coa() and is the
    # closest analogue to the purchase-cost accounts a real bill hits.
    from app.services.ledger import get_account_by_code
    exp_acc = get_account_by_code(bill.company_id, "5100")
    assert exp_acc, "expense account 5100 missing from seeded CoA"
    db.session.add(VendorBillItem(
        bill_id=bill.id, account_id=exp_acc.id,
        description="widget", line_type=BillLineType.EXPENSE,
        quantity=1, unit_price=gross, line_total=gross,
    ))
    bill.recalc()
    db.session.commit()
    post_vendor_bill(bill, created_by=_STATE["owner_id"])
    return bill


def _pay(bill, amount):
    from app.services.vendor_bills import record_bill_payment
    record_bill_payment(bill, float(amount),
                         created_by=_STATE["owner_id"])


def _return(bill):
    from app.models import VendorRefundType
    from app.services.vendor_bills import post_vendor_bill_refund
    return post_vendor_bill_refund(
        bill, VendorRefundType.FULL,
        reason="test-return",
        created_by=_STATE["owner_id"],
    )


def _cash_account_ids():
    """The set of receiving accounts the return code path can pick.
    `post_vendor_bill_refund` looks at bill.payment_method:
      CASH → 1110
      anything else → first of 1124/1121/1122/1123/1125 that exists
    Test bills use CREDIT, so the refund lands on the first bank
    (usually 1124).  Return the union so the assertion is method-
    agnostic."""
    from app.models import Account
    codes = ("1110", "1124", "1121", "1122", "1123", "1125")
    ids = []
    for code in codes:
        acc = Account.query.filter_by(
            company_id=_STATE["a_id"], code=code).first()
        if acc:
            ids.append(acc.id)
    assert ids, "no cash/bank accounts seeded"
    return set(ids)


def _ap_account_id_for(bill):
    from app.services.subsidiary import party_ap_account
    return party_ap_account(bill).id


def _return_je(bill):
    """The one VendorBillRefund/JE for this bill's latest return."""
    from app.models import VendorBillRefund, JournalEntry
    r = VendorBillRefund.query.filter_by(bill_id=bill.id).order_by(
        VendorBillRefund.id.desc()).first()
    assert r is not None, f"no VendorBillRefund for {bill.number}"
    je = db.session.get(JournalEntry, r.journal_entry_id)
    assert je is not None, "refund JE missing"
    return je


def _debit_lines(je, account_id_or_set):
    """Return debit amounts for `je` restricted to `account_id_or_set`.
    `account_id_or_set` may be one int or a set of ints."""
    ids = (account_id_or_set
            if isinstance(account_id_or_set, set)
            else {account_id_or_set})
    return [float(ln.debit)
             for ln in je.lines
             if ln.account_id in ids and float(ln.debit) > 0]


def _ap_net(bill):
    """Vendor sub-account net (Cr - Dr, i.e. how much we owe)."""
    from app.models import JournalLine, JournalEntry
    ap_id = _ap_account_id_for(bill)
    rows = (db.session.query(JournalLine)
             .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
             .filter(JournalLine.account_id == ap_id,
                      JournalEntry.company_id == _STATE["a_id"],
                      JournalEntry.is_active.is_(True))
             .all())
    return round(sum(float(r.credit) - float(r.debit) for r in rows), 2)


# ─── Checks ────────────────────────────────────────────────────────
@check("1. Unpaid FULL return: cash untouched, AP debited for total")
def _():
    bill = _fresh_bill(500)
    total = float(bill.total)
    ap_before = _ap_net(bill)
    _return(bill)
    je = _return_je(bill)
    cash_debits = _debit_lines(je, _cash_account_ids())
    ap_debits = _debit_lines(je, _ap_account_id_for(bill))
    assert cash_debits == [], f"cash should be untouched, got {cash_debits}"
    assert len(ap_debits) == 1 and abs(ap_debits[0] - total) < 0.01, \
        f"AP debit expected {total}, got {ap_debits}"
    ap_after = _ap_net(bill)
    assert abs(ap_after - (ap_before - total)) < 0.01, \
        f"AP net delta expected -{total}, got {ap_after - ap_before}"
    return (f"AP debited {total:.2f}; cash untouched; "
            f"vendor AP {ap_before:+.2f}→{ap_after:+.2f}")


@check("2. Fully-paid FULL return: cash debited for total, AP untouched")
def _():
    bill = _fresh_bill(700)
    total = float(bill.total)
    _pay(bill, total)
    ap_before = _ap_net(bill)
    _return(bill)
    je = _return_je(bill)
    cash_debits = _debit_lines(je, _cash_account_ids())
    ap_debits = _debit_lines(je, _ap_account_id_for(bill))
    assert len(cash_debits) == 1 and abs(cash_debits[0] - total) < 0.01, \
        f"cash debit expected {total}, got {cash_debits}"
    assert ap_debits == [], f"AP should be untouched, got {ap_debits}"
    ap_after = _ap_net(bill)
    assert abs(ap_before - ap_after) < 0.01, \
        f"AP net unchanged expected, got {ap_before}→{ap_after}"
    assert float(bill.paid_amount or 0) == 0.0
    return f"cash debited {total:.2f}; vendor AP net unchanged"


@check("3. Partial-paid single-payment FULL return: split cash + AP")
def _():
    bill = _fresh_bill(1000)
    total = float(bill.total)
    paid = 300.0
    _pay(bill, paid)
    ap_before = _ap_net(bill)
    _return(bill)
    je = _return_je(bill)
    cash_debits = _debit_lines(je, _cash_account_ids())
    ap_debits = _debit_lines(je, _ap_account_id_for(bill))
    assert len(cash_debits) == 1 and abs(cash_debits[0] - paid) < 0.01, \
        f"cash debit expected {paid}, got {cash_debits}"
    assert len(ap_debits) == 1 and abs(ap_debits[0] - (total - paid)) < 0.01, \
        f"AP debit expected {total - paid}, got {ap_debits}"
    ap_after = _ap_net(bill)
    # AC1: the return debits AP by exactly (total - paid), so this
    # bill's remaining slice on vendor AP closes to zero after return.
    # `ap_before` may carry contributions from PRIOR bills in the
    # same fixture, so check the delta not the absolute.
    assert abs((ap_before - ap_after) - (total - paid)) < 0.01, \
        f"vendor AP delta expected {total - paid}, got {ap_before - ap_after}"
    assert float(bill.paid_amount or 0) == 0.0
    return f"cash {paid:.2f} + AP {total - paid:.2f}; bill closed"


@check("4. Multi-payment partial-paid FULL return (VB-0088 pattern)")
def _():
    # 2500 total, 3 payments summing to 1501 (from ticket).
    bill = _fresh_bill(2500)
    total = float(bill.total)
    _pay(bill, 1250)
    _pay(bill, 250)
    _pay(bill, 1)
    paid = 1501.0
    ap_before = _ap_net(bill)
    _return(bill)
    je = _return_je(bill)
    cash_debits = _debit_lines(je, _cash_account_ids())
    ap_debits = _debit_lines(je, _ap_account_id_for(bill))
    assert len(cash_debits) == 1 and abs(cash_debits[0] - paid) < 0.01, \
        f"cash debit expected {paid}, got {cash_debits}"
    assert len(ap_debits) == 1 and abs(ap_debits[0] - (total - paid)) < 0.01, \
        f"AP debit expected {total - paid}, got {ap_debits}"
    ap_after = _ap_net(bill)
    assert abs((ap_before - ap_after) - (total - paid)) < 0.01, \
        f"vendor AP delta expected {total - paid}, got {ap_before - ap_after}"
    assert float(bill.paid_amount or 0) == 0.0
    return (f"3 payments summing {paid:.2f}; "
            f"cash {paid:.2f} + AP {total - paid:.2f}; "
            f"ghost balance prevented; paid_amount = 0")


@check("5. Balance guard: every purchase-return JE has Σ debits = Σ credits")
def _():
    from app.models import JournalEntry
    entries = JournalEntry.query.filter(
        JournalEntry.company_id == _STATE["a_id"],
        JournalEntry.source_type == "vendor_bill_refund",
    ).all()
    assert entries, "no purchase-return JEs recorded"
    for je in entries:
        d = sum(float(ln.debit or 0) for ln in je.lines)
        c = sum(float(ln.credit or 0) for ln in je.lines)
        assert abs(d - c) < 0.01, \
            f"unbalanced JE #{je.id}: Dr {d} vs Cr {c}"
    return f"{len(entries)} return JEs, all balanced"


@check("6. bill.paid_amount is never negative across any voided bill")
def _():
    from app.models import VendorBill, VendorBillStatus
    bills = VendorBill.query.filter(
        VendorBill.company_id == _STATE["a_id"],
        VendorBill.status.in_((VendorBillStatus.REFUNDED,
                                 VendorBillStatus.PARTIALLY_REFUNDED)),
    ).all()
    assert bills, "no returned bills to inspect"
    for b in bills:
        assert float(b.paid_amount or 0) >= 0, \
            f"bill {b.number} paid_amount = {b.paid_amount}"
    return f"{len(bills)} returned bills, all paid_amount >= 0"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}  => {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  => {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "a_id" in _STATE:
                    _teardown(_STATE["a_id"])
                print("\n(cleaned up fixture company)")
            except Exception as e:
                print(f"\n(teardown failed: {e})")
    print()
    print(f"----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
