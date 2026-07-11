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

    return jsonify(summary)
