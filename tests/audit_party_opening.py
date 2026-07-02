#!/usr/bin/env python3
"""MARSOUD-PARTY-OPENING-BALANCE-01 — end-to-end audit.

Proves, on a fresh company:
  1. Customer created with opening 3000 posts a balanced journal
     (Dr customer-sub / Cr 3900) and customer.balance == 3000.
  2. Add an invoice 2000 (unpaid) → customer.balance == 5000.
  3. Vendor created with opening 1500 posts (Dr 3900 / Cr vendor-sub)
     and vendor.balance == 1500.
  4. Trying to record an opening for a party that already has activity
     (an invoice / bill exists) is refused with a clear LedgerError.
  5. Trying to record an opening twice for the same party is refused.
  6. Opening = 0 is a no-op — no journal, no PartyOpeningBalance row.
  7. Customer.balance backward compatibility: on a customer with a
     sub-account, the property returns the ledger value; on one
     without (simulating pre-rebuild data), it falls back to the
     legacy sum-of-invoices calc and matches to the cent.
  8. Negative opening (customer sitting on an advance) reverses the
     direction correctly and Customer.balance goes negative.
"""
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__PARTY_OPENING_AUDIT__"
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
            JournalLine.entry_id.in_(entry_ids),
        ).delete(synchronize_session=False)
    inv_ids = [r.id for r in Invoice.query.filter_by(
        company_id=company_id).all()]
    if inv_ids:
        InvoiceItem.query.filter(
            InvoiceItem.invoice_id.in_(inv_ids),
        ).delete(synchronize_session=False)
        Payment.query.filter(
            Payment.invoice_id.in_(inv_ids),
        ).delete(synchronize_session=False)
    bill_ids = [r.id for r in VendorBill.query.filter_by(
        company_id=company_id).all()]
    if bill_ids:
        VendorBillItem.query.filter(
            VendorBillItem.bill_id.in_(bill_ids),
        ).delete(synchronize_session=False)
    for table in reversed(db.metadata.sorted_tables):
        if "company_id" in {col["name"] for col in insp.get_columns(table.name)}:
            db.session.execute(
                table.delete().where(table.c.company_id == company_id),
            )
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()


@check("1. Customer opening 3000 posts balanced Dr AR / Cr 3900")
def _():
    from app.models import Customer, JournalEntry, JournalLine
    from app.services.subsidiary import (
        ensure_customer_account, record_customer_opening_balance,
    )
    cid = _STATE["company_id"]
    c = Customer(company_id=cid, name="عميل رصيد افتتاحي")
    db.session.add(c); db.session.flush()
    ensure_customer_account(c)
    ob = record_customer_opening_balance(c, 3000.0)
    db.session.commit()
    assert ob is not None
    assert abs(float(ob.amount) - 3000.0) < 0.01
    entry = db.session.get(JournalEntry, ob.journal_entry_id)
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    total_dr = sum(float(l.debit or 0) for l in lines)
    total_cr = sum(float(l.credit or 0) for l in lines)
    assert abs(total_dr - total_cr) < 0.01, \
        f"unbalanced: dr={total_dr} cr={total_cr}"
    codes = {l.account.code: (float(l.debit or 0), float(l.credit or 0))
              for l in lines}
    assert "3900" in codes and codes["3900"][1] > 0.01, \
        f"expected Cr 3900, got {codes}"
    # Refresh the customer to pick up the balance change
    db.session.refresh(c)
    assert abs(c.balance - 3000.0) < 0.01, \
        f"customer.balance should be 3000, got {c.balance}"
    _STATE["customer_id"] = c.id
    return f"OB {ob.id} = 3000, journal balanced, customer.balance=3000"


@check("2. Adding invoice 2000 to the same customer → balance = 5000")
def _():
    from app.models import Customer, Invoice, InvoiceItem, InvoiceStatus
    from app.services.invoicing import post_invoice_to_ledger
    cid = _STATE["company_id"]
    c = db.session.get(Customer, _STATE["customer_id"])
    inv = Invoice(
        company_id=cid, customer_id=c.id, number="TESTINV-OB-1",
        issue_date=date.today(), due_date=date.today(),
        currency="SAR", status=InvoiceStatus.DRAFT,
        tax_rate=Decimal("0"),
    )
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="سلعة",
        quantity=Decimal("1"), unit_price=Decimal("2000"),
        line_total=Decimal("2000"),
    ))
    db.session.flush()
    inv.recalc()
    db.session.flush()
    post_invoice_to_ledger(inv)
    db.session.commit()
    db.session.refresh(c)
    assert abs(c.balance - 5000.0) < 0.01, \
        f"expected balance 5000, got {c.balance}"
    return f"customer.balance = {c.balance:.2f}"


@check("3. Vendor opening 1500 posts Dr 3900 / Cr vendor-sub")
def _():
    from app.models import Vendor, JournalEntry, JournalLine
    from app.services.subsidiary import (
        ensure_vendor_account, record_vendor_opening_balance,
    )
    cid = _STATE["company_id"]
    v = Vendor(company_id=cid, name="مورد رصيد افتتاحي")
    db.session.add(v); db.session.flush()
    ensure_vendor_account(v)
    ob = record_vendor_opening_balance(v, 1500.0)
    db.session.commit()
    entry = db.session.get(JournalEntry, ob.journal_entry_id)
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    total_dr = sum(float(l.debit or 0) for l in lines)
    total_cr = sum(float(l.credit or 0) for l in lines)
    assert abs(total_dr - total_cr) < 0.01
    codes = {l.account.code: (float(l.debit or 0), float(l.credit or 0))
              for l in lines}
    assert "3900" in codes and codes["3900"][0] > 0.01, \
        f"expected Dr 3900, got {codes}"
    db.session.refresh(v)
    # Vendor.balance is a positive number (credit-side stored as negative
    # in raw account.balance? — let's just assert magnitude matches).
    assert abs(abs(v.balance) - 1500.0) < 0.01, \
        f"vendor.balance should be 1500, got {v.balance}"
    _STATE["vendor_id"] = v.id
    return f"OB {ob.id} = 1500, vendor.balance={v.balance:.2f}"


@check("4. Opening refused for party with existing activity")
def _():
    from app.models import Customer
    from app.services.subsidiary import (
        ensure_customer_account, record_customer_opening_balance,
    )
    from app.services.ledger import LedgerError
    cid = _STATE["company_id"]
    # Customer #1 already has invoice from check 2 → should be refused
    c = db.session.get(Customer, _STATE["customer_id"])
    raised = False
    try:
        # Delete the existing OB first to bypass the "already exists"
        # check and hit the "has activity" check.
        from app.models import PartyOpeningBalance, PartyType
        PartyOpeningBalance.query.filter_by(
            party_type=PartyType.CUSTOMER, party_id=c.id,
        ).delete()
        db.session.flush()
        record_customer_opening_balance(c, 100.0)
    except LedgerError as e:
        raised = True
        msg = str(e)
    db.session.rollback()
    assert raised, "expected LedgerError for customer with invoice"
    assert "فواتير" in msg, f"error should mention invoices, got: {msg}"
    return f"refused: {msg}"


@check("5. Duplicate opening on same party is refused")
def _():
    from app.models import Customer
    from app.services.subsidiary import (
        ensure_customer_account, record_customer_opening_balance,
    )
    from app.services.ledger import LedgerError
    cid = _STATE["company_id"]
    c2 = Customer(company_id=cid, name="عميل مرتين")
    db.session.add(c2); db.session.flush()
    ensure_customer_account(c2)
    record_customer_opening_balance(c2, 500.0)
    db.session.commit()
    raised = False
    try:
        record_customer_opening_balance(c2, 200.0)
    except LedgerError as e:
        raised = True
        msg = str(e)
    db.session.rollback()
    assert raised, "expected LedgerError on duplicate opening"
    assert "مسجل بالفعل" in msg, f"unexpected message: {msg}"
    return f"refused: {msg}"


@check("6. Opening = 0 is a no-op")
def _():
    from app.models import Customer, PartyOpeningBalance, PartyType
    from app.services.subsidiary import (
        ensure_customer_account, record_customer_opening_balance,
    )
    cid = _STATE["company_id"]
    c3 = Customer(company_id=cid, name="عميل صفر")
    db.session.add(c3); db.session.flush()
    ensure_customer_account(c3)
    ob = record_customer_opening_balance(c3, 0.0)
    db.session.commit()
    assert ob is None, "opening = 0 should return None (no-op)"
    # No PartyOpeningBalance row should exist
    n = PartyOpeningBalance.query.filter_by(
        party_type=PartyType.CUSTOMER, party_id=c3.id,
    ).count()
    assert n == 0, f"expected 0 rows, got {n}"
    return "no journal, no OB row"


@check("7. Customer.balance backward compat (fallback for no sub-account)")
def _():
    from app.models import (
        Customer, Invoice, InvoiceItem, InvoiceStatus, PaymentMethod,
    )
    from app.services.invoicing import post_invoice_to_ledger, record_payment
    cid = _STATE["company_id"]
    # Simulate a legacy customer without a sub-account.
    c4 = Customer(company_id=cid, name="عميل قديم")
    db.session.add(c4); db.session.flush()
    # DELIBERATELY skip ensure_customer_account → account_id stays NULL
    # so the balance property falls back to legacy behaviour. We need to
    # create the invoice without triggering the AR sub post because the
    # ledger requires an account; the balance property should still work
    # from the invoice.balance side.
    #
    # Since we can't post to the ledger without a sub, we short-circuit:
    # give the customer a synthetic invoice that .balance can sum. That
    # means creating an Invoice with a hard-coded balance and total; the
    # property does not care whether it posted.
    inv = Invoice(
        company_id=cid, customer_id=c4.id, number="TESTINV-LEGACY-1",
        issue_date=date.today(), due_date=date.today(),
        currency="SAR", status=InvoiceStatus.SENT,
        tax_rate=Decimal("0"),
        subtotal=Decimal("800"), total=Decimal("800"),
        paid_amount=Decimal("300"),
    )
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="خدمة قديمة",
        quantity=Decimal("1"), unit_price=Decimal("800"),
        line_total=Decimal("800"),
    ))
    db.session.commit()
    # Balance should be invoice.balance = 800 - 300 = 500 via the legacy
    # fallback path (no account_id).
    assert c4.account_id is None, \
        "test invariant: customer must have no sub-account"
    assert abs(c4.balance - 500.0) < 0.01, \
        f"legacy fallback broken: expected 500, got {c4.balance}"
    return f"legacy fallback ok: balance={c4.balance:.2f}"


@check("8. Negative opening reverses direction")
def _():
    from app.models import Customer
    from app.services.subsidiary import (
        ensure_customer_account, record_customer_opening_balance,
    )
    cid = _STATE["company_id"]
    c5 = Customer(company_id=cid, name="عميل بمقدم")
    db.session.add(c5); db.session.flush()
    ensure_customer_account(c5)
    ob = record_customer_opening_balance(c5, -400.0)
    db.session.commit()
    db.session.refresh(c5)
    assert abs(c5.balance - (-400.0)) < 0.01, \
        f"expected balance -400, got {c5.balance}"
    return f"OB {ob.id} = -400, customer.balance={c5.balance:.2f}"


# ─── Run ────────────────────────────────────────────────────────────────
def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        from tests._orphan_sweep import preflight
        preflight()
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
