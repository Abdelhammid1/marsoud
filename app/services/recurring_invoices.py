"""MARSOUD-RECURRING-INVOICE-01 (Abdelhamid 2026-07-24).

Direct mirror of `process_recurring_journals` — same skeleton,
same duplicate-run guard, but posts an Invoice + JE per period
instead of a JE alone.
"""
import logging
from datetime import date, timedelta
from decimal import Decimal
from dateutil.relativedelta import relativedelta
from app import db
from app.models import (
    RecurringInvoice, RecurringInvoiceLog,
    REC_INV_ACTION_EXECUTE, REC_INV_ACTION_FAIL,
    REC_INV_FREQ_DAILY, REC_INV_FREQ_WEEKLY,
    REC_INV_FREQ_MONTHLY, REC_INV_FREQ_YEARLY,
    Invoice, InvoiceItem, InvoiceStatus,
)
from app.services.numbering import next_number
from app.services.invoicing import post_invoice_to_ledger

log = logging.getLogger("marsoud.recurring_invoices")


def process_recurring_invoices():
    """Advance every due schedule, posting one Invoice per missed
    period. Idempotent — the unique index on (recurring_id,
    period_posted, action='EXECUTE') prevents duplicate creation if
    cron fires twice within the same day."""
    due = RecurringInvoice.query.filter(
        RecurringInvoice.is_active.is_(True),
        RecurringInvoice.is_deleted.is_(False),
    ).all()
    posted = 0
    failed = 0
    today = date.today()
    for sched in due:
        if sched.end_date and sched.next_run_date > sched.end_date:
            sched.is_active = False
            continue
        while sched.next_run_date <= today:
            if sched.end_date and sched.next_run_date > sched.end_date:
                sched.is_active = False
                break
            period = sched.next_run_date
            try:
                inv = _post_from_invoice_template(sched, period)
                db.session.add(RecurringInvoiceLog(
                    recurring_id=sched.id,
                    action=REC_INV_ACTION_EXECUTE,
                    period_posted=period,
                    invoice_id=inv.id,
                ))
                posted += 1
            except Exception as e:
                # Duplicate-run guard trip counts as a graceful skip,
                # not a real failure — the invoice for this period
                # already exists.
                if "recurring_id" in str(e) or "UNIQUE" in str(e).upper():
                    log.info("Skipping already-executed period %s "
                              "for schedule %s", period, sched.id)
                else:
                    log.exception(
                        "recurring invoice %s failed for %s",
                        sched.id, period)
                    db.session.rollback()
                    db.session.add(RecurringInvoiceLog(
                        recurring_id=sched.id,
                        action=REC_INV_ACTION_FAIL,
                        period_posted=period,
                        error_message=str(e)[:500],
                    ))
                    failed += 1
                    break
            sched.next_run_date = _advance_date(
                sched.next_run_date, sched.frequency)
            if sched.end_date and sched.next_run_date > sched.end_date:
                sched.is_active = False
                break
    db.session.commit()
    return {"posted": posted, "failed": failed}


def _post_from_invoice_template(sched, period):
    """Build one Invoice from the schedule's items_json and post it."""
    number = next_number(sched.company_id, "INVOICE")
    inv = Invoice(
        company_id=sched.company_id,
        customer_id=sched.customer_id,
        number=number,
        issue_date=period,
        due_date=period + timedelta(days=30),
        currency=(sched.customer.company.base_currency
                   if sched.customer and sched.customer.company
                   else "EGP"),
        tax_rate=sched.tax_rate,
        status=InvoiceStatus.DRAFT,
        notes=f"[متكررة] {sched.name}",
        created_by_id=sched.created_by_id,
    )
    for line in sched.items:
        inv.items.append(InvoiceItem(
            description=(line.get("description") or "")[:255],
            quantity=Decimal(str(line.get("quantity") or 0)),
            unit_price=Decimal(str(line.get("unit_price") or 0)),
        ))
    db.session.add(inv); db.session.flush()
    # Compute totals + write the JE. Same order the manual invoice
    # create flow uses.
    inv.recalc()
    post_invoice_to_ledger(inv, created_by=sched.created_by_id)
    return inv


def _advance_date(dt, freq):
    if freq == REC_INV_FREQ_DAILY:
        return dt + timedelta(days=1)
    if freq == REC_INV_FREQ_WEEKLY:
        return dt + timedelta(days=7)
    if freq == REC_INV_FREQ_MONTHLY:
        return dt + relativedelta(months=1)
    if freq == REC_INV_FREQ_YEARLY:
        return dt + relativedelta(years=1)
    # Unknown frequency → advance a day so we don't loop forever.
    return dt + timedelta(days=1)
