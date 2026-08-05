"""MARSOUD-VBILL-OVERDUE-01 (2026-08-06) — cron: materialise recurring
vendor-bill forecasts into real POSTED VendorBills.

Direct mirror of process_recurring_invoices at services/recurring_invoices.py:
same skeleton, same duplicate-run guard, but posts a VendorBill + JE per
missed occurrence instead of an Invoice. The ticket points to that file
by name and says "نفس الآلية بالظبط" — so this file's shape is bounded
by that mandate.

WHY THE FUNCTION EXISTS. The dashboard's "فواتير جايّة عليك" panel is a
live forecast — never materialised. When the projected date passed, the
row aged off the panel and no record existed anywhere: HR concluded
"must have been paid" and moved on. This cron converts the forecast
into a real POSTED bill the moment the day arrives, so the bill
either appears in the overdue panel (if unpaid) or in the ledger (if
someone paid it) — never silently disappears.

IDEMPOTENCY. Every insert lands with recurring_bill_id + occurrence_date
set; the unique index on that pair blocks a second insert. A cron run
that fires twice on the same day sees the second call raise
IntegrityError from materialize_from_recurring's commit, and we log +
skip cleanly — no double posting, no orphan half-DRAFT.
"""
import logging
from datetime import date

from app import db
from app.models import Company

log = logging.getLogger("marsoud.recurring_vendor_bills")


def process_recurring_vendor_bills():
    """Walk every active company, materialise every past-due
    unmaterialised forecast into a POSTED VendorBill. Returns a summary
    suitable for the /cron/tick response body."""
    from app.services.recurring_bills import unmaterialised_past_due
    from app.services.vendor_bills import materialize_from_recurring
    from app.models import RecurringBill

    posted = 0
    skipped_duplicate = 0
    failed = 0
    today = date.today()

    for co in Company.query.filter_by(is_active=True).all():
        occurrences = unmaterialised_past_due(co.id, as_of=today)
        for row in occurrences:
            rb = db.session.get(RecurringBill, row["recurring_bill_id"])
            if rb is None or not rb.active:
                continue
            try:
                materialize_from_recurring(
                    rb, row["date"], actor_id=None)
                posted += 1
            except Exception as e:
                # Idempotency trip: the second cron run on the same day
                # bumps into the (recurring_bill_id, occurrence_date)
                # unique index — treat as a graceful skip, matching
                # process_recurring_invoices' pattern at
                # services/recurring_invoices.py:59.
                if "UNIQUE" in str(e).upper() or "recurring_bill" in str(e):
                    log.info(
                        "Skip already-materialised recurring bill %s for %s",
                        rb.id, row["date"])
                    skipped_duplicate += 1
                    db.session.rollback()
                else:
                    log.exception(
                        "recurring vendor bill %s failed for %s",
                        rb.id, row["date"])
                    db.session.rollback()
                    failed += 1

    return {"posted": posted, "skipped_duplicate": skipped_duplicate,
            "failed": failed}
