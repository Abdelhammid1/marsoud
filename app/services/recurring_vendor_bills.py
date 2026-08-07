"""MARSOUD-VBILL-OVERDUE-01 (2026-08-06) — cron: materialise recurring
vendor-bill forecasts into real DRAFT VendorBills for human review.

Direct mirror of process_recurring_invoices at services/recurring_invoices.py:
same skeleton, same duplicate-run guard, but produces a VendorBill per
missed occurrence instead of an Invoice. The ticket points to that file
by name and says "نفس الآلية بالظبط" — so this file's shape is bounded
by that mandate.

WHY THE FUNCTION EXISTS. The dashboard's "فواتير جايّة عليك" panel is a
live forecast — never materialised. When the projected date passed, the
row aged off the panel and no record existed anywhere: HR concluded
"must have been paid" and moved on. This cron converts the forecast
into a real DRAFT bill the moment the day arrives, so the bill
appears in the overdue panel and a human decides whether to post + pay
it — never silently disappears.

⚠ MARSOUD-CRON-VBILL-NO-AUTOPAY-01 (2026-08-07). The 2026-08-06 cron run
after a 3-week outage materialised + auto-paid 4 CASH-template bills
totalling 5,526.93 EGP with no owner approval. Root cause: this file
called materialize_from_recurring() without status_target, which
defaulted to POSTED. post_vendor_bill() then posted a second journal
(Dr Vendor sub / Cr Cash|Bank) that drained the till and flipped the
bill to PAID. The fix is on line 53 below: explicit status_target=
"DRAFT" so cron NEVER auto-posts, regardless of the source template's
payment method. Payment is always a human step per ticket wording
"الدفع خطوة بشرية دايمًا، بغض النظر عن طريقة الدفع الأصلية في القالب."

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
    unmaterialised forecast into a DRAFT VendorBill (never POSTED —
    see module docstring). Returns a summary suitable for the
    /cron/tick response body.

    Summary keys:
      materialised       — count of DRAFT bills newly created (was
                            "posted" before MARSOUD-CRON-VBILL-NO-
                            AUTOPAY-01; renamed because "posted" was
                            factually wrong now that we never post
                            here).
      skipped_duplicate  — the unique-index catch (safe double-run).
      failed             — anything else (logged in full).
    """
    from app.services.recurring_bills import unmaterialised_past_due
    from app.services.vendor_bills import materialize_from_recurring
    from app.models import RecurringBill

    materialised = 0
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
                # ⚠ status_target="DRAFT" is the ENTIRE point of
                # MARSOUD-CRON-VBILL-NO-AUTOPAY-01. Do NOT change to
                # "POSTED" without explicit approval — see the module
                # docstring for the incident that motivated this.
                materialize_from_recurring(
                    rb, row["date"], actor_id=None,
                    status_target="DRAFT")
                materialised += 1
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

    return {"materialised": materialised,
            # Back-compat alias so any dashboard / audit that still
            # reads the old "posted" key doesn't 500 — matches the
            # DRAFT count (the number of new rows produced).
            "posted": materialised,
            "skipped_duplicate": skipped_duplicate,
            "failed": failed}
