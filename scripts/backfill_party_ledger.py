#!/usr/bin/env python3
"""MARSOUD-PARTY-LEDGER-02 — one-time backfill for companies that have
legacy data from BEFORE this ticket.

What it does (idempotent):

  1. Opens a sub-account under 1130 / 2110 / 2130 for every customer /
     vendor / employee in the company that doesn't have one yet.

  2. Finds every PAID/POSTED vendor bill whose JournalEntry credits cash
     or a bank leaf directly (instead of the vendor's sub-account) and
     rewrites the entry into the new two-step pattern:
        BEFORE:  Dr Expense+VAT / Cr Cash
        AFTER:   Dr Expense+VAT / Cr Vendor sub
                 + new "settlement" entry: Dr Vendor sub / Cr Cash

     The rewrite never touches the per-account NET balance, just adds
     the vendor as a pass-through so his statement is complete.

Usage:
    flask shell -c "from scripts.backfill_party_ledger import run; run(company_id=1, dry_run=True)"

or as a CLI:
    flask backfill-party-ledger <company_id>            # dry-run by default
    flask backfill-party-ledger <company_id> --apply    # actually rewrite
"""
from datetime import date
import click
from flask.cli import with_appcontext

from app import db
from app.models import (
    Customer, Vendor, Employee, JournalEntry, JournalLine, Account,
    VendorBill, VendorBillPaymentMethod,
)
from app.services.subsidiary import (
    ensure_customer_account, ensure_vendor_account, ensure_employee_account,
)


def _open_subaccounts(company_id):
    """Step 1: every party gets a sub-account if missing."""
    opened = {"customer": 0, "vendor": 0, "employee": 0}
    for c in Customer.query.filter_by(company_id=company_id).all():
        if not c.account_id:
            ensure_customer_account(c)
            opened["customer"] += 1
    for v in Vendor.query.filter_by(company_id=company_id).all():
        if not v.account_id:
            ensure_vendor_account(v)
            opened["vendor"] += 1
    for e in Employee.query.filter_by(company_id=company_id).all():
        if not e.account_id:
            ensure_employee_account(e)
            opened["employee"] += 1
    return opened


_CASH_CODES = ("1110", "1121", "1122", "1123", "1124", "1125")


def _rewrite_legacy_cash_bills(company_id, dry_run=True):
    """Step 2: rewrite PAID cash/bank bills that posted straight to
    cash without the vendor sub-account leg.

    Returns (rewritten_count, sample_of_first_5)."""
    rewritten = []
    bills = VendorBill.query.filter(
        VendorBill.company_id == company_id,
        VendorBill.vendor_id.isnot(None),
        VendorBill.payment_method.in_([
            VendorBillPaymentMethod.CASH, VendorBillPaymentMethod.BANK,
        ]),
        VendorBill.journal_entry_id.isnot(None),
    ).all()

    for bill in bills:
        entry = db.session.get(JournalEntry, bill.journal_entry_id)
        if not entry or not entry.is_active:
            continue
        # Make sure the vendor has a sub-account.
        if not bill.vendor.account_id:
            ensure_vendor_account(bill.vendor)
        vendor_acc = bill.vendor.account

        lines = JournalLine.query.filter_by(entry_id=entry.id).all()
        cash_lines = []
        for l in lines:
            acc = db.session.get(Account, l.account_id)
            if acc and acc.code in _CASH_CODES and float(l.credit or 0) > 0:
                cash_lines.append((l, acc))
        # If the journal already has a credit to the vendor's sub, skip
        if any(l.account_id == vendor_acc.id for l in lines):
            continue
        if not cash_lines:
            continue   # nothing to rewrite

        # Total credited to cash equals the bill total (by construction).
        cash_line, cash_acc = cash_lines[0]
        amount = float(cash_line.credit or 0)
        rewritten.append({
            "bill_number": bill.number,
            "vendor": bill.vendor.name,
            "amount": amount,
            "cash_code": cash_acc.code,
            "entry_id": entry.id,
        })
        if dry_run:
            continue

        # Rewrite the original entry's cash-credit line to credit the
        # vendor sub-account instead. Then add a separate "settlement"
        # entry that debits the vendor sub and credits cash.
        cash_line.account_id = vendor_acc.id
        cash_line.credit_base = float(cash_line.credit_base or 0)
        cash_line.memo = (cash_line.memo or "") + " (rewritten via backfill)"

        # New settlement entry — same date as the bill, marked as a
        # backfill so we can find them later.
        from app.services.ledger import post_journal
        post_journal(
            company_id=company_id,
            description=(f"سداد فوري (backfill) لفاتورة المورد "
                          f"{bill.number} — {bill.vendor.name}"),
            lines=[
                {"account_id": vendor_acc.id, "debit": amount,
                 "credit": 0, "memo": f"سداد فاتورة {bill.number}"},
                {"account_id": cash_acc.id, "debit": 0,
                 "credit": amount,
                 "memo": f"دفع نقدي/بنكي (rewritten)"},
            ],
            entry_date=bill.issue_date,
            reference=f"BF-PAY-{bill.number}",
            source_type="vendor_bill_payment",
            source_id=bill.id,
        )

    if not dry_run:
        db.session.commit()
    return rewritten


def run(company_id, dry_run=True):
    """Run both steps for one company. Returns a summary dict."""
    opened = _open_subaccounts(company_id)
    rewritten = _rewrite_legacy_cash_bills(company_id, dry_run=dry_run)
    if not dry_run:
        db.session.commit()
    return {
        "company_id": company_id,
        "dry_run": dry_run,
        "subaccounts_opened": opened,
        "bills_rewritten": len(rewritten),
        "bill_sample": rewritten[:5],
    }


@click.command("backfill-party-ledger")
@click.argument("company_id", type=int)
@click.option("--apply", is_flag=True, help="Actually rewrite (default = dry-run)")
@with_appcontext
def backfill_cli(company_id, apply):
    """Open sub-accounts for every party + rewrite legacy cash vendor bills."""
    result = run(company_id, dry_run=not apply)
    print(f"\nCompany {result['company_id']} ({'APPLIED' if apply else 'DRY-RUN'}):")
    print(f"  sub-accounts opened: {result['subaccounts_opened']}")
    print(f"  bills to rewrite:    {result['bills_rewritten']}")
    if result["bill_sample"]:
        print(f"  first 5 samples:")
        for s in result["bill_sample"]:
            print(f"    - {s['bill_number']} → {s['vendor']} "
                  f"({s['amount']:.2f} from {s['cash_code']})")
    if apply:
        print("\nDone — verify with `flask check-coa` then re-run the audit.")
    else:
        print("\nThis was a dry-run. Add --apply to write changes.")
