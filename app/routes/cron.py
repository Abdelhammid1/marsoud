"""Cron tick — call this from an external scheduler (e.g. system cron, GitHub Actions).

POST /cron/tick   — runs all scheduled tasks once
"""
from flask import Blueprint, jsonify, request, current_app
from app.services.reminders import process_invoice_reminders
from app.services.invoicing import update_overdue_statuses
from app.services.journals import process_recurring_journals
from app.services.hr import check_expiring_contracts
from app.models import Company

bp = Blueprint("cron", __name__)


def _authorized():
    token = current_app.config.get("CRON_TOKEN") or ""
    if not token:
        return True   # no token configured → allow (dev)
    provided = request.headers.get("X-Cron-Token") or request.args.get("token", "")
    return provided == token


@bp.route("/tick", methods=["POST", "GET"])
def tick():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    summary = {}

    # Mark overdue invoices across all companies
    overdue_total = 0
    for c in Company.query.filter_by(is_active=True).all():
        overdue_total += update_overdue_statuses(c.id)
    summary["marked_overdue"] = overdue_total

    # MARSOUD-VBILL-OVERDUE-01 (2026-08-06) — the vendor side of the
    # same story. Previously, a vendor bill only flipped to OVERDUE
    # when someone opened the vendor-bills index page; a company with
    # no one browsing that page could carry unflagged overdue bills
    # for weeks. Emits VENDOR_BILL_OVERDUE bell notifications inside
    # the service, one-shot per bill (see the docstring for the
    # dedup story).
    vb_overdue_total = 0
    try:
        from app.services.vendor_bills import update_overdue_vendor_bills
        for c in Company.query.filter_by(is_active=True).all():
            vb_overdue_total += update_overdue_vendor_bills(c.id)
        summary["vendor_bill_overdue"] = vb_overdue_total
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception(
            "vendor bill overdue sweep failed: %s", e)
        summary["vendor_bill_overdue"] = {"error": str(e)[:200]}

    # MARSOUD-VBILL-OVERDUE-01 (2026-08-06) — materialise recurring
    # vendor-bill forecasts into real POSTED bills the moment their
    # date arrives. Idempotent via the unique index on
    # (recurring_bill_id, recurring_occurrence_date) — a double-firing
    # cron cannot double-post. Mirrors the customer side
    # (process_recurring_invoices) as the ticket mandates.
    try:
        from app.services.recurring_vendor_bills import (
            process_recurring_vendor_bills,
        )
        summary["recurring_vendor_bills"] = process_recurring_vendor_bills()
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception(
            "recurring vendor bills failed: %s", e)
        summary["recurring_vendor_bills"] = {"error": str(e)[:200]}

    # Send reminder emails
    summary["reminders"] = process_invoice_reminders()

    # MARSOUD-57.3 — subscription expiry reminders
    try:
        from app.services.reminders import process_subscription_reminders
        summary["subscription_reminders"] = process_subscription_reminders()
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception(
            "subscription reminders failed: %s", e)
        summary["subscription_reminders"] = {"error": str(e)[:200]}

    # Post any due recurring journal entries
    summary["recurring"] = process_recurring_journals()

    # MARSOUD-RECURRING-INVOICE-01 (Abdelhamid 2026-07-24) — post any
    # due recurring invoices. Idempotent via the unique index on
    # (recurring_id, period_posted, action) — safe if cron double-
    # fires on the same day.
    try:
        from app.services.recurring_invoices import process_recurring_invoices
        summary["recurring_invoices"] = process_recurring_invoices()
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception(
            "recurring invoices failed: %s", e)
        summary["recurring_invoices"] = {"error": str(e)[:200]}

    # MARSOUD-SAAS-DEFERRED-INVOICE-01 (Batch 8 Ticket 2,
    # 2026-07-30) — create deferred SaaS next-cycle invoices for
    # any tenant whose next_billing_date has arrived. Idempotent
    # because the sweep clears the date after creating; re-runs
    # find nothing to do.
    try:
        from app.services.saas_billing import process_saas_next_invoices
        summary["saas_next_invoices"] = process_saas_next_invoices()
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception(
            "SaaS next-invoice sweep failed: %s", e)
        summary["saas_next_invoices"] = {"error": str(e)[:200]}

    # MARSOUD-INSTALLMENT-PLAN-01 (Abdelhamid 2026-07-24) — flip
    # PENDING → OVERDUE for installments past their due date. Cheap
    # single-query update, safe on any cadence.
    try:
        from app.services.installments import refresh_installment_overdue_flags
        summary["installment_overdue"] = {
            "flipped": refresh_installment_overdue_flags(),
        }
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception(
            "installment overdue refresh failed: %s", e)
        summary["installment_overdue"] = {"error": str(e)[:200]}

    # MARSOUD-INSTALLMENT-PLAN-01 pt 2 (Abdelhamid 2026-07-25) —
    # per-installment reminders. Same policy as invoice reminders
    # (company.reminders config), but fires one email per DUE
    # installment (not the whole invoice) so a 3-installment plan
    # gets 3 separate customer touchpoints.
    try:
        from app.services.reminders import process_installment_reminders
        summary["installment_reminders"] = process_installment_reminders()
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception(
            "installment reminders failed: %s", e)
        summary["installment_reminders"] = {"error": str(e)[:200]}

    # HR-03: contract expiry alerts (30 / 60 days)
    try:
        summary["contract_alerts"] = check_expiring_contracts()
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception("contract alerts failed: %s", e)
        summary["contract_alerts"] = {"error": str(e)[:200]}

    # Cycle 7 gap-close — task deadline 24h reminders
    try:
        from app.services.opsflow_extras import remind_task_deadlines_24h
        summary["task_deadlines"] = remind_task_deadlines_24h()
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception("task deadlines failed: %s", e)
        summary["task_deadlines"] = {"error": str(e)[:200]}

    # MARSOUD-TASK-SCHEDULE — spawn Task rows from any TaskSchedule
    # whose window includes today. Idempotent — last_generated_date
    # is checked inside the service so double-firing this route is
    # safe.
    try:
        from app.services.task_schedules import materialize_due_schedules
        summary["task_schedules"] = materialize_due_schedules()
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception(
            "task schedule materialization failed: %s", e)
        summary["task_schedules"] = {"error": str(e)[:200]}

    # MARSOUD-TASK-ARCHIVE-01 — auto-archive DONE tasks > 30 days old
    try:
        from app.services.task_archive import auto_archive_old_done
        summary["task_auto_archive"] = auto_archive_old_done()
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception("auto-archive failed: %s", e)
        summary["task_auto_archive"] = {"error": str(e)[:200]}

    # MARSOUD-ACTLOG-01 — flip idle / ended user sessions. Safe to run
    # at any cadence; the 10-min wall-clock is enforced by the operator
    # cron entry, not by this code.
    try:
        from app.services.activity import cleanup_idle_sessions
        summary["session_cleanup"] = cleanup_idle_sessions(
            idle_minutes=10, ended_minutes=30,
        )
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception("session cleanup failed: %s", e)
        summary["session_cleanup"] = {"error": str(e)[:200]}

    # HR-05: monthly leave accrual (only credits when the day is the 1st;
    # cron is meant to run daily but this work should fire once per month).
    from datetime import date as _date
    if request.args.get("force_accrual") == "1" or _date.today().day == 1:
        try:
            from app.services.leave import monthly_leave_accrual
            summary["leave_accrual"] = monthly_leave_accrual()
        except Exception as e:
            import logging
            logging.getLogger("ledgeros.cron").exception("leave accrual failed: %s", e)
            summary["leave_accrual"] = {"error": str(e)[:200]}
    else:
        summary["leave_accrual"] = {"skipped": "not the 1st of month"}

    # MARSOUD-EMPLOYEE-DAILY-REPORTS — build DRAFT digests for yesterday.
    # Runs on every tick; build_digest is idempotent (unique constraint
    # per employee+day + skip-if-submitted logic guarantee no dupes).
    try:
        from app.services.daily_digest import run_daily_digest_for_company
        rep_summary = {}
        for c in Company.query.filter_by(is_active=True).all():
            # Only companies whose plan includes the module. Cheap
            # check: if plan_allows("employee_reports.view", c) is
            # false, skip that company entirely.
            from app.services.plan_gating import plan_allows
            if not plan_allows("employee_reports.view", c):
                continue
            rep_summary[c.id] = run_daily_digest_for_company(c.id)
        summary["daily_digests"] = rep_summary
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception(
            "daily digest failed: %s", e)
        summary["daily_digests"] = {"error": str(e)[:200]}

    # MARSOUD-ATTENDANCE-AUTO (2026-08-05) — mark absent anyone who never
    # checked in. Looks at YESTERDAY, never today: a day can only be
    # judged once it is over, or everyone who has not arrived yet would
    # be marked absent. Idempotent — create_exception refuses a second
    # exception for the same day.
    try:
        from app.services.attendance import sweep_absences
        summary["attendance_absences"] = sweep_absences()
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception(
            "absence sweep failed: %s", e)
        summary["attendance_absences"] = {"error": str(e)[:200]}

    # MARSOUD-METRIC-AUTOMATION (2026-08-05) — two jobs, in this order
    # on purpose: the cycle has to exist (and its targets with it) before
    # anything can be scored into it. On the 1st of the month the cycle
    # opens and the same tick starts awarding against it.
    #
    # Both are idempotent, so a cron that double-fires — or a retry after
    # a timeout — creates nothing extra.
    try:
        from app.services.metric_automation import open_monthly_cycles
        summary["evaluation_cycles"] = open_monthly_cycles()
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception(
            "monthly cycle open failed: %s", e)
        summary["evaluation_cycles"] = {"error": str(e)[:200]}

    try:
        from app.services.metric_automation import award_metric_entries
        summary["metric_entries"] = award_metric_entries()
    except Exception as e:
        import logging
        logging.getLogger("ledgeros.cron").exception(
            "metric awarding failed: %s", e)
        summary["metric_entries"] = {"error": str(e)[:200]}

    return jsonify(summary)
