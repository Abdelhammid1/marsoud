#!/usr/bin/env python3
"""MARSOUD-COMM-ACCRUAL — backfill commission accruals dated to the
INVOICE date.

Walks every invoice in the target company whose customer has a
sales_rep + commission_rate configured, and makes sure:

  1. A SalesCommission row exists for the invoice (positive, non-carry
     forward). If missing, creates one at invoice.issue_date.
  2. If a row exists but is dated wrong (period_year/month ≠ the
     invoice's month, or the journal entry is dated wrong), it gets
     RE-DATED to the invoice's month and its journal entry gets its
     date corrected. The row's status is preserved (UNPAID stays
     UNPAID, PAID stays PAID).

Never creates duplicate rows. Never touches carry-forward or negative
(refund clawback) rows.

Usage:
    flask backfill-commission-accrual <company_id>            # dry-run
    flask backfill-commission-accrual <company_id> --apply    # actually write
"""
from datetime import date

import click
from flask.cli import with_appcontext

from app import db
from app.models import Invoice, SalesCommission, JournalEntry


def _plan_for_company(company_id):
    """Compute what would change without writing anything.

    Returns a list of dicts describing each planned action so both
    dry-run + apply can share the same walk logic."""
    plan = []
    invoices = Invoice.query.filter_by(company_id=company_id).order_by(
        Invoice.issue_date, Invoice.id
    ).all()
    for inv in invoices:
        cust = inv.customer
        if not cust or not cust.sales_rep_id or not cust.commission_rate:
            continue
        try:
            rate = float(cust.commission_rate)
        except (TypeError, ValueError):
            continue
        if rate <= 0:
            continue
        subtotal = float(inv.subtotal or 0)
        if subtotal <= 0:
            continue
        expected_amount = round(subtotal * rate / 100, 4)
        expected_year = inv.issue_date.year if inv.issue_date else date.today().year
        expected_month = inv.issue_date.month if inv.issue_date else date.today().month

        existing = SalesCommission.query.filter(
            SalesCommission.invoice_id == inv.id,
            SalesCommission.is_carry_forward.is_(False),
            SalesCommission.amount > 0,
        ).first()

        if not existing:
            plan.append({
                "action": "create",
                "invoice_id": inv.id,
                "invoice_number": inv.number,
                "issue_date": inv.issue_date.isoformat() if inv.issue_date else None,
                "rep_id": cust.sales_rep_id,
                "amount": expected_amount,
                "period": f"{expected_year}-{expected_month:02d}",
            })
            continue

        # Row exists. Check whether it needs re-dating.
        needs_reperiod = (
            existing.period_year != expected_year or
            existing.period_month != expected_month
        )
        needs_journal_redate = False
        if existing.journal_entry_id:
            je = db.session.get(JournalEntry, existing.journal_entry_id)
            if je and inv.issue_date and je.date != inv.issue_date:
                needs_journal_redate = True
        if needs_reperiod or needs_journal_redate:
            plan.append({
                "action": "redate",
                "invoice_id": inv.id,
                "invoice_number": inv.number,
                "commission_id": existing.id,
                "old_period": f"{existing.period_year}-{existing.period_month:02d}",
                "new_period": f"{expected_year}-{expected_month:02d}",
                "old_journal_date": (
                    db.session.get(JournalEntry, existing.journal_entry_id).date.isoformat()
                    if existing.journal_entry_id else None),
                "new_journal_date": inv.issue_date.isoformat() if inv.issue_date else None,
            })
    return plan


def _apply_plan(company_id, plan, created_by=None):
    """Execute every action in the plan. Called only when --apply is set."""
    from app.services.sales_commissions import (
        record_commission_accrual_for_invoice,
    )
    created = 0
    redated = 0
    for step in plan:
        inv = db.session.get(Invoice, step["invoice_id"])
        if step["action"] == "create":
            record_commission_accrual_for_invoice(inv, created_by=created_by)
            created += 1
        elif step["action"] == "redate":
            row = db.session.get(SalesCommission, step["commission_id"])
            inv_date = inv.issue_date or date.today()
            row.period_year = inv_date.year
            row.period_month = inv_date.month
            if row.journal_entry_id:
                je = db.session.get(JournalEntry, row.journal_entry_id)
                if je:
                    je.date = inv_date
            redated += 1
    db.session.commit()
    return {"created": created, "redated": redated}


def run(company_id, dry_run=True):
    """Public helper — invoked from the CLI or the audit."""
    plan = _plan_for_company(company_id)
    result = {
        "company_id": company_id,
        "dry_run": dry_run,
        "planned_creates": sum(1 for s in plan if s["action"] == "create"),
        "planned_redates": sum(1 for s in plan if s["action"] == "redate"),
        "plan_sample": plan[:10],
    }
    if not dry_run and plan:
        result["applied"] = _apply_plan(company_id, plan)
    return result


@click.command("backfill-commission-accrual")
@click.argument("company_id", type=int)
@click.option("--apply", is_flag=True,
              help="Actually write changes (default = dry-run)")
@with_appcontext
def backfill_cli(company_id, apply):
    """Ensure every invoice with a sales rep has an accrual dated to
    the invoice date. Fixes mis-dated commission rows in place."""
    result = run(company_id, dry_run=not apply)
    tag = "APPLIED" if apply else "DRY-RUN"
    print(f"\nCompany {result['company_id']} ({tag}):")
    print(f"  commissions to CREATE:  {result['planned_creates']}")
    print(f"  commissions to RE-DATE: {result['planned_redates']}")
    if result["plan_sample"]:
        print("\n  first 10 planned actions:")
        for step in result["plan_sample"]:
            if step["action"] == "create":
                print(f"    + CREATE  inv {step['invoice_number']} "
                      f"({step['issue_date']}) → {step['amount']:.2f} on "
                      f"{step['period']}")
            else:
                print(f"    ↔ REDATE  inv {step['invoice_number']} "
                      f"comm #{step['commission_id']}: "
                      f"{step['old_period']} → {step['new_period']}")
    if apply and "applied" in result:
        a = result["applied"]
        print(f"\nDone — created {a['created']}, re-dated {a['redated']}.")
    elif not apply:
        print("\nThis was a dry-run. Add --apply to write changes.")
