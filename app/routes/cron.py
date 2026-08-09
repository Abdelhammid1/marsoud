"""Cron tick — call this from an external scheduler (e.g. system cron, GitHub Actions).

POST /cron/tick   — runs all scheduled tasks once

Every job is wrapped in `_run(name, fn)` — a small helper that
combines the existing "log + summary[k] = {'error': ...} on
failure" convention with the T11 tracker (writes one row to
platform_cron_runs per (job, tick) for the ops-health page).
The outer `__tick__` wrap gives the page a "did a full tick
complete recently?" liveness signal even when a single job
failed.
"""
import logging
from flask import Blueprint, jsonify, request, current_app
from app.services.reminders import process_invoice_reminders
from app.services.invoicing import update_overdue_statuses
from app.services.journals import process_recurring_journals
from app.services.hr import check_expiring_contracts
from app.services.cron_tracking import track_cron_job
from app.models import Company

bp = Blueprint("cron", __name__)


def _authorized():
    token = current_app.config.get("CRON_TOKEN") or ""
    if not token:
        return True   # no token configured → allow (dev)
    provided = request.headers.get("X-Cron-Token") or request.args.get("token", "")
    return provided == token


def _run(name, fn):
    """MARSOUD-SUPERADMIN-CONTROL-01 T11 (2026-08-08) — collapses
    the try/except + logging + summary-error dict pattern used by
    every job in this file into one call, and wires the T11 tracker
    so `platform_cron_runs` gets one row per (job, tick) with the
    result attached (dict / int / None). The tracker never raises;
    a bookkeeping crash keeps the actual job intact.

    Returns whatever `fn()` returned on success, or
    `{"error": "…"}` on failure — matching the existing summary
    shape that /admin/cron consumers rely on.
    """
    try:
        with track_cron_job(name) as ctx:
            result = fn()
            if result is not None:
                ctx.summary(result)
            return result
    except Exception as e:
        logging.getLogger("ledgeros.cron").exception(
            "%s failed: %s", name, e)
        return {"error": str(e)[:200]}


@bp.route("/tick", methods=["POST", "GET"])
def tick():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401

    summary = {}

    # Outer wrap → the "was there a full tick recently?" liveness
    # answer for the ops-health page survives any single-job failure.
    with track_cron_job("__tick__") as tick_ctx:

        # Mark overdue invoices across all companies.
        def _marked_overdue():
            total = 0
            for c in Company.query.filter_by(is_active=True).all():
                total += update_overdue_statuses(c.id)
            return total
        summary["marked_overdue"] = _run("marked_overdue", _marked_overdue)

        # MARSOUD-VBILL-OVERDUE-01 (2026-08-06) — the vendor side of
        # the same story. Previously, a vendor bill only flipped to
        # OVERDUE when someone opened the vendor-bills index page;
        # a company with no one browsing that page could carry
        # unflagged overdue bills for weeks. Emits VENDOR_BILL_OVERDUE
        # bell notifications inside the service, one-shot per bill.
        def _vendor_bill_overdue():
            from app.services.vendor_bills import update_overdue_vendor_bills
            total = 0
            for c in Company.query.filter_by(is_active=True).all():
                total += update_overdue_vendor_bills(c.id)
            return total
        summary["vendor_bill_overdue"] = _run(
            "vendor_bill_overdue", _vendor_bill_overdue)

        # MARSOUD-CASH-CUSTODY-01 (2026-08-07, slice 3) — bell every
        # custody past its settlement_due_date, once per custody per
        # company. Dedup lives on custody_overdue_notified_at, cleared
        # by close/cancel so a re-issued row can re-notify.
        def _custody_overdue():
            from app.services.cash_custody import sweep_overdue_custodies
            total = 0
            for c in Company.query.filter_by(is_active=True).all():
                total += sweep_overdue_custodies(c.id)
            return total
        summary["custody_overdue"] = _run(
            "custody_overdue", _custody_overdue)

        # MARSOUD-ITEM-CUSTODY-01 (2026-08-07) — bell for item
        # custodies that have been ACTIVE longer than the threshold
        # (default 90d). Ticket asked for "أسبوعية بالعهد النشطة أكتر
        # من مدة معينة". One-shot per custody via
        # overdue_notified_at, cleared on any settlement.
        def _item_custody_long_active():
            from app.services.item_custody import (
                sweep_long_active_custodies,
            )
            total = 0
            for c in Company.query.filter_by(is_active=True).all():
                total += sweep_long_active_custodies(c.id)
            return total
        summary["item_custody_long_active"] = _run(
            "item_custody_long_active", _item_custody_long_active)

        # MARSOUD-VBILL-OVERDUE-01 (2026-08-06) — materialise
        # recurring vendor-bill forecasts into real POSTED bills the
        # moment their date arrives. Idempotent via the unique index
        # on (recurring_bill_id, recurring_occurrence_date) — a
        # double-firing cron cannot double-post.
        def _recurring_vendor_bills():
            from app.services.recurring_vendor_bills import (
                process_recurring_vendor_bills,
            )
            return process_recurring_vendor_bills()
        summary["recurring_vendor_bills"] = _run(
            "recurring_vendor_bills", _recurring_vendor_bills)

        # Send reminder emails.
        summary["reminders"] = _run("reminders", process_invoice_reminders)

        # MARSOUD-57.3 — subscription expiry reminders
        def _subscription_reminders():
            from app.services.reminders import process_subscription_reminders
            return process_subscription_reminders()
        summary["subscription_reminders"] = _run(
            "subscription_reminders", _subscription_reminders)

        # Post any due recurring journal entries.
        summary["recurring"] = _run("recurring", process_recurring_journals)

        # MARSOUD-RECURRING-INVOICE-01 (Abdelhamid 2026-07-24) — post
        # any due recurring invoices. Idempotent via the unique index
        # on (recurring_id, period_posted, action).
        def _recurring_invoices():
            from app.services.recurring_invoices import process_recurring_invoices
            return process_recurring_invoices()
        summary["recurring_invoices"] = _run(
            "recurring_invoices", _recurring_invoices)

        # MARSOUD-SAAS-DEFERRED-INVOICE-01 (Batch 8 Ticket 2,
        # 2026-07-30) — create deferred SaaS next-cycle invoices for
        # any tenant whose next_billing_date has arrived. Idempotent
        # because the sweep clears the date after creating.
        def _saas_next_invoices():
            from app.services.saas_billing import process_saas_next_invoices
            return process_saas_next_invoices()
        summary["saas_next_invoices"] = _run(
            "saas_next_invoices", _saas_next_invoices)

        # MARSOUD-INSTALLMENT-PLAN-01 (Abdelhamid 2026-07-24) — flip
        # PENDING → OVERDUE for installments past their due date.
        def _installment_overdue():
            from app.services.installments import refresh_installment_overdue_flags
            return {"flipped": refresh_installment_overdue_flags()}
        summary["installment_overdue"] = _run(
            "installment_overdue", _installment_overdue)

        # MARSOUD-INSTALLMENT-PLAN-01 pt 2 (Abdelhamid 2026-07-25) —
        # per-installment reminders. Same policy as invoice
        # reminders (company.reminders config), but fires one email
        # per DUE installment.
        def _installment_reminders():
            from app.services.reminders import process_installment_reminders
            return process_installment_reminders()
        summary["installment_reminders"] = _run(
            "installment_reminders", _installment_reminders)

        # HR-03: contract expiry alerts (30 / 60 days).
        summary["contract_alerts"] = _run(
            "contract_alerts", check_expiring_contracts)

        # Cycle 7 gap-close — task deadline 24h reminders.
        def _task_deadlines():
            from app.services.opsflow_extras import remind_task_deadlines_24h
            return remind_task_deadlines_24h()
        summary["task_deadlines"] = _run("task_deadlines", _task_deadlines)

        # MARSOUD-TASK-SCHEDULE — spawn Task rows from any
        # TaskSchedule whose window includes today. Idempotent —
        # last_generated_date is checked inside the service.
        def _task_schedules():
            from app.services.task_schedules import materialize_due_schedules
            return materialize_due_schedules()
        summary["task_schedules"] = _run("task_schedules", _task_schedules)

        # MARSOUD-TASK-ARCHIVE-01 — auto-archive DONE tasks > 30
        # days old.
        def _task_auto_archive():
            from app.services.task_archive import auto_archive_old_done
            return auto_archive_old_done()
        summary["task_auto_archive"] = _run(
            "task_auto_archive", _task_auto_archive)

        # MARSOUD-ACTLOG-01 — flip idle / ended user sessions.
        def _session_cleanup():
            from app.services.activity import cleanup_idle_sessions
            return cleanup_idle_sessions(
                idle_minutes=10, ended_minutes=30)
        summary["session_cleanup"] = _run(
            "session_cleanup", _session_cleanup)

        # HR-05: monthly leave accrual (only credits when the day is
        # the 1st; cron is meant to run daily but this work should
        # fire once per month).
        from datetime import date as _date
        if request.args.get("force_accrual") == "1" or _date.today().day == 1:
            def _leave_accrual():
                from app.services.leave import monthly_leave_accrual
                return monthly_leave_accrual()
            summary["leave_accrual"] = _run("leave_accrual", _leave_accrual)
        else:
            summary["leave_accrual"] = {"skipped": "not the 1st of month"}

        # MARSOUD-EMPLOYEE-DAILY-REPORTS — build DRAFT digests for
        # yesterday. Runs on every tick; build_digest is idempotent.
        def _daily_digests():
            from app.services.daily_digest import run_daily_digest_for_company
            from app.services.plan_gating import plan_allows
            out = {}
            for c in Company.query.filter_by(is_active=True).all():
                if not plan_allows("employee_reports.view", c):
                    continue
                out[c.id] = run_daily_digest_for_company(c.id)
            return out
        summary["daily_digests"] = _run("daily_digests", _daily_digests)

        # MARSOUD-ATTENDANCE-AUTO (2026-08-05) — mark absent anyone
        # who never checked in. Looks at YESTERDAY, never today.
        def _attendance_absences():
            from app.services.attendance import sweep_absences
            return sweep_absences()
        summary["attendance_absences"] = _run(
            "attendance_absences", _attendance_absences)

        # MARSOUD-METRIC-AUTOMATION (2026-08-05) — two jobs, in this
        # order on purpose: the cycle has to exist (and its targets
        # with it) before anything can be scored into it.
        def _evaluation_cycles():
            from app.services.metric_automation import open_monthly_cycles
            return open_monthly_cycles()
        summary["evaluation_cycles"] = _run(
            "evaluation_cycles", _evaluation_cycles)

        def _metric_entries():
            from app.services.metric_automation import award_metric_entries
            return award_metric_entries()
        summary["metric_entries"] = _run("metric_entries", _metric_entries)

        # MARSOUD-AGENT-MEMORY-05 (2026-08-06) — hard-delete
        # conversations older than the PlatformSetting retention
        # window (default 90; 0 = never expire).
        def _agent_conversation_expiry():
            from app.services.agent_conversations import expire_old_conversations
            return expire_old_conversations()
        summary["agent_conversation_expiry"] = _run(
            "agent_conversation_expiry", _agent_conversation_expiry)

        # Attach the full summary to the __tick__ tracker row so a
        # human reading /admin/ops-health can see the whole payload
        # at a glance.
        tick_ctx.summary(summary)

    return jsonify(summary)
