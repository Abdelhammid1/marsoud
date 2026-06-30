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


def rewrite_legacy_payroll_journals(company_id, dry_run=True):
    """MARSOUD-PAYROLL-LEDGER-03 — rewrite payroll runs posted before
    the per-employee fix.

    Legacy pattern (one journal): Dr 5210 total / Cr Cash <paid> /
    Cr <emp sub> <accrued only>. Paid-in-full employees never appeared
    on their ledger.

    New pattern (two journals): accrual (per-employee Cr by NET) +
    settlement (per-employee Dr by amount_paid + Cr Cash total_paid).

    This function:
      1. Finds every PayrollRun with a linked journal that ISN'T already
         in the two-journal layout.
      2. Adds the missing per-employee credits AND debits + cash credit
         so the trail is complete. Net-balance impact on every account
         is zero (we're only adding pass-through legs).
    """
    from app.models import (
        PayrollRun, PayrollLine, Employee, JournalEntry, JournalLine,
        Account,
    )
    from app.services.subsidiary import ensure_employee_account
    from app.services.ledger import post_journal

    rewritten = []
    runs = PayrollRun.query.filter_by(company_id=company_id).all()
    for run in runs:
        if not run.journal_entry_id:
            continue
        # Already migrated? Look for a settlement entry for this run.
        existing_settle = JournalEntry.query.filter_by(
            company_id=company_id, source_type="payroll_settlement",
            source_id=run.id,
        ).first()
        if existing_settle:
            continue
        # Look at the legacy journal — if it already has per-employee
        # credits for ALL employees (not just accrued ones), we'd skip,
        # but the legacy code only added credits for accrued employees,
        # so we'll always need to top it up.
        entry = db.session.get(JournalEntry, run.journal_entry_id)
        if not entry or not entry.is_active:
            continue

        lines = run.lines
        if not lines:
            continue

        # Figure out which employees were already credited (accrued only,
        # under legacy) so we don't double-count them when patching.
        existing_credits = {}   # employee_id → existing credit amount
        for jl in JournalLine.query.filter_by(entry_id=entry.id).all():
            acc = db.session.get(Account, jl.account_id)
            if not acc or not acc.code.startswith("2130-"):
                continue
            # Look up which employee owns this sub-account
            emp = Employee.query.filter_by(
                company_id=company_id, account_id=acc.id,
            ).first()
            if emp:
                existing_credits[emp.id] = existing_credits.get(emp.id, 0) + float(jl.credit or 0)

        total_paid_cash = 0.0
        missing_accrual_lines = []
        settle_lines = []

        for line in lines:
            net = float(line.net or 0)
            paid = float(line.amount_paid or 0)
            if net < 0.005:
                continue
            emp = line.employee
            if not emp:
                continue
            if not emp.account_id:
                ensure_employee_account(emp)
            emp_acct = emp.account
            already_credited = existing_credits.get(emp.id, 0)
            shortfall = round(net - already_credited, 2)
            if shortfall > 0.005:
                # Top-up accrual: bring legacy total up to NET.
                # Offset goes against the same cash account that was
                # incorrectly credited too early, so the new accrual leg
                # nets to zero against the cash line we add below.
                missing_accrual_lines.append((emp_acct, emp, shortfall))
            if paid > 0.005:
                settle_lines.append({
                    "account_id": emp_acct.id,
                    "debit": round(paid, 2), "credit": 0,
                    "memo": f"سداد راتب (backfill) — {emp.name}",
                })
                total_paid_cash += paid

        if not missing_accrual_lines and not settle_lines:
            continue

        rewritten.append({
            "run_id": run.id,
            "period": f"{run.period_month:02d}/{run.period_year}",
            "missing_credits": len(missing_accrual_lines),
            "settled_employees": len(settle_lines),
            "total_paid_cash": round(total_paid_cash, 2),
        })

        if dry_run:
            continue

        # Apply: post a "fix-up accrual" entry to add the missing
        # employee credits (offset = cash debit, since the legacy entry
        # already credited cash for paid amount but never gave the
        # employee a credit).
        if missing_accrual_lines:
            cash = get_account_for_code(company_id, "1110")
            top_up_lines = []
            total_shortfall = 0.0
            for emp_acct, emp, amount in missing_accrual_lines:
                top_up_lines.append({
                    "account_id": emp_acct.id,
                    "debit": 0, "credit": amount,
                    "memo": f"استحقاق راتب (backfill) — {emp.name}",
                })
                total_shortfall += amount
            top_up_lines.append({
                "account_id": cash.id,
                "debit": round(total_shortfall, 2), "credit": 0,
                "memo": "إعادة قيد نقدية (backfill — لم تكن مرت على الموظف)",
            })
            post_journal(
                company_id=company_id,
                description=(f"إعادة قيد رواتب (backfill) — "
                              f"{run.period_month:02d}/{run.period_year}"),
                lines=top_up_lines,
                reference=f"BF-PAYROLL-{run.period_year}-{run.period_month:02d}",
                source_type="payroll",
                source_id=run.id,
            )

        if settle_lines:
            cash = get_account_for_code(company_id, "1110")
            settle_lines.append({
                "account_id": cash.id,
                "debit": 0, "credit": round(total_paid_cash, 2),
                "memo": (f"إعادة قيد سداد نقدي (backfill) — "
                          f"{run.period_month:02d}/{run.period_year}"),
            })
            post_journal(
                company_id=company_id,
                description=(f"إعادة قيد سداد رواتب (backfill) — "
                              f"{run.period_month:02d}/{run.period_year}"),
                lines=settle_lines,
                reference=(f"BF-PAYROLL-PAY-"
                            f"{run.period_year}-{run.period_month:02d}"),
                source_type="payroll_settlement",
                source_id=run.id,
            )

    if not dry_run:
        db.session.commit()
    return rewritten


def get_account_for_code(company_id, code):
    """Small helper — same as services.ledger.get_account_by_code but
    avoids the circular import inside this module."""
    return Account.query.filter_by(company_id=company_id, code=code).first()


def run(company_id, dry_run=True):
    """Run all three backfill steps for one company. Returns a summary."""
    opened = _open_subaccounts(company_id)
    rewritten_bills = _rewrite_legacy_cash_bills(company_id, dry_run=dry_run)
    rewritten_payroll = rewrite_legacy_payroll_journals(
        company_id, dry_run=dry_run,
    )
    if not dry_run:
        db.session.commit()
    return {
        "company_id": company_id,
        "dry_run": dry_run,
        "subaccounts_opened": opened,
        "bills_rewritten": len(rewritten_bills),
        "bill_sample": rewritten_bills[:5],
        "payroll_runs_rewritten": len(rewritten_payroll),
        "payroll_sample": rewritten_payroll[:5],
    }


@click.command("backfill-party-ledger")
@click.argument("company_id", type=int)
@click.option("--apply", is_flag=True, help="Actually rewrite (default = dry-run)")
@with_appcontext
def backfill_cli(company_id, apply):
    """Open sub-accounts for every party + rewrite legacy cash vendor
    bills + legacy payroll journals so every party transaction shows
    on their statement."""
    result = run(company_id, dry_run=not apply)
    print(f"\nCompany {result['company_id']} ({'APPLIED' if apply else 'DRY-RUN'}):")
    print(f"  sub-accounts opened: {result['subaccounts_opened']}")
    print(f"  cash vendor bills to rewrite: {result['bills_rewritten']}")
    if result["bill_sample"]:
        print(f"    first 5 samples:")
        for s in result["bill_sample"]:
            print(f"      - {s['bill_number']} → {s['vendor']} "
                  f"({s['amount']:.2f} from {s['cash_code']})")
    print(f"  payroll runs to rewrite:      {result['payroll_runs_rewritten']}")
    if result["payroll_sample"]:
        print(f"    first 5 samples:")
        for s in result["payroll_sample"]:
            print(f"      - run #{s['run_id']} ({s['period']}) "
                  f"add {s['missing_credits']} credits, "
                  f"{s['settled_employees']} settlements, "
                  f"cash {s['total_paid_cash']:.2f}")
    if apply:
        print("\nDone — verify with `flask check-coa` then re-run the audits.")
    else:
        print("\nThis was a dry-run. Add --apply to write changes.")
