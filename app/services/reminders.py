"""Invoice reminder processor — runs from the cron tick endpoint.

For each invoice, fire reminders according to its company's `reminder_config`:
  days_before: [int]   — N days before due_date (e.g., [7, 3])
  overdue_days: [int]  — N days after due_date  (e.g., [0, 7, 14])

Each (kind, days) pair fires at most once per invoice via InvoiceReminderSent.
"""
from datetime import date, datetime
from app import db
from app.models import (
    Invoice, InvoiceStatus, InvoiceReminderSent, Company,
    SubscriptionReminderSent,
)
from app.services.email import send_overdue_reminder, send_email
import logging
from flask import render_template, url_for

logger = logging.getLogger("ledgeros.reminders")


def _already_sent(invoice_id, kind, days):
    return InvoiceReminderSent.query.filter_by(
        invoice_id=invoice_id, threshold_kind=kind, threshold_days=days
    ).first() is not None


def _mark_sent(invoice_id, company_id, kind, days):
    db.session.add(InvoiceReminderSent(
        invoice_id=invoice_id, company_id=company_id,
        threshold_kind=kind, threshold_days=days,
        sent_at=datetime.utcnow(),
    ))


def process_invoice_reminders():
    """Single pass — call from a cron tick. Returns a summary dict.

    MARSOUD-TZ-01 — "today" is evaluated in each invoice's company
    timezone, not the server's local timezone. Cron runs UTC on the
    server, but a Cairo customer's Sep-30 invoice is due Sep-30 in
    Cairo, not Sep-30 UTC. Skipping the correction can fire reminders
    one day early or late for companies whose day-boundary hasn't
    crossed UTC's yet.
    """
    from app.services.time import today_in_company_tz
    sent_counts = {"before": 0, "overdue": 0, "skipped": 0}

    # Cache reminder configs + today-per-company (both are per-company
    # invariants inside a single tick).
    company_cfg = {}
    company_today = {}
    candidates = Invoice.query.filter(
        Invoice.send_reminders.is_(True),
        Invoice.status.in_([
            InvoiceStatus.SENT,
            InvoiceStatus.PARTIALLY_PAID,
            InvoiceStatus.OVERDUE,
        ]),
    ).all()

    for inv in candidates:
        if inv.balance <= 0.01:
            sent_counts["skipped"] += 1
            continue

        cfg = company_cfg.get(inv.company_id)
        today = company_today.get(inv.company_id)
        if cfg is None or today is None:
            company = db.session.get(Company, inv.company_id)
            cfg = company.reminders if company else {}
            today = today_in_company_tz(company) if company else date.today()
            company_cfg[inv.company_id] = cfg
            company_today[inv.company_id] = today

        if not cfg.get("enabled", True):
            sent_counts["skipped"] += 1
            continue

        days_until = (inv.due_date - today).days
        # before-due-date thresholds
        for d in cfg.get("days_before", []):
            if days_until == d and not _already_sent(inv.id, "before", d):
                if send_overdue_reminder(inv, f"before_{d}"):
                    _mark_sent(inv.id, inv.company_id, "before", d)
                    sent_counts["before"] += 1
        # overdue thresholds (days past due)
        days_overdue = -days_until  # positive = past due
        for d in cfg.get("overdue_days", []):
            if days_overdue == d and days_overdue >= 0 and not _already_sent(inv.id, "overdue", d):
                if send_overdue_reminder(inv, "overdue" if d == 0 else f"overdue_{d}"):
                    _mark_sent(inv.id, inv.company_id, "overdue", d)
                    if inv.status != InvoiceStatus.OVERDUE and days_overdue > 0:
                        inv.status = InvoiceStatus.OVERDUE
                    sent_counts["overdue"] += 1

    db.session.commit()
    logger.info("Reminders processed: %s", sent_counts)
    return sent_counts


# ─── MARSOUD-57.3: subscription expiry reminders ─────────────────────────
# Thresholds now come from platform_settings (via subscription service),
# default [7,5,3,1,0]. SUBSCRIPTION_THRESHOLDS retained only for legacy
# imports — DO NOT use it; call get_reminder_thresholds() instead.
SUBSCRIPTION_THRESHOLDS = [7, 5, 3, 1, 0]


def _company_owner_email(company):
    """Best-effort: pick an OWNER's email from the company's members."""
    from app.models import User
    from app.models.user import user_companies
    row = db.session.execute(
        user_companies.select().where(
            (user_companies.c.company_id == company.id) &
            (user_companies.c.role == "owner")
        )
    ).first()
    if not row:
        return None
    u = db.session.get(User, row.user_id)
    return u.email if u and u.email else None


def process_subscription_reminders():
    """Single pass — scan companies, fire expiry reminders for the
    highest threshold the company has reached + not yet been notified at.
    Returns a summary dict. Thresholds come from platform_settings.

    MARSOUD bug-fix — two issues from the original implementation:

      1. Strict `days_remaining == threshold` match.
         If the cron didn't tick on the exact day the company hit that
         threshold (host down, deploy in progress, etc.), the reminder
         was silently lost forever. Fixed to "<=" so a missed day
         catches up on the next tick.

      2. send_email returns True in logs-only mode (SMTP_HOST not set),
         and we used to write SubscriptionReminderSent on that True.
         The row then marked it "sent" permanently — so when SMTP got
         configured later, the reminder never re-fired. Fixed to only
         persist the sent-row when SMTP is actually configured, so the
         call self-heals once the env is corrected.
    """
    from app.services.subscription import get_reminder_thresholds
    from flask import current_app
    # Walk thresholds high → low so we send the "earliest-warning" first.
    # E.g. defaults [7, 5, 3, 1, 0] become [7, 5, 3, 1, 0] already; if a
    # site overrides them out of order, sort here.
    thresholds = sorted(get_reminder_thresholds(), reverse=True)
    today = date.today()
    smtp_configured = bool(current_app.config.get("SMTP_HOST"))
    sent_counts = {"sent": 0, "skipped_no_email": 0,
                   "skipped_no_expiry": 0,
                   "skipped_smtp_not_configured": 0,
                   "thresholds": thresholds,
                   "smtp_configured": smtp_configured}
    companies = Company.query.filter_by(is_active=True).all()
    for c in companies:
        if not c.subscription_expires_at:
            sent_counts["skipped_no_expiry"] += 1
            continue
        days_remaining = (c.subscription_expires_at.date() - today).days
        # Walk thresholds high → low. Fire the FIRST threshold the
        # company has reached AND hasn't been notified for under the
        # current expiry date. Stop after one — lower thresholds will
        # fire on later ticks as days_remaining decreases.
        for threshold in thresholds:
            if days_remaining > threshold:
                continue
            already = SubscriptionReminderSent.query.filter_by(
                company_id=c.id,
                threshold_days=threshold,
                expires_at_when_sent=c.subscription_expires_at,
            ).first()
            if already:
                continue
            email = _company_owner_email(c)
            if not email:
                sent_counts["skipped_no_email"] += 1
                break
            # Build the email body — display the ACTUAL days remaining
            # (not the threshold) so a catch-up reminder reads accurately.
            if days_remaining <= 0:
                subject = f"⛔ اشتراك {c.name} انتهى" if days_remaining < 0 else f"⏰ اشتراك {c.name} ينتهي اليوم"
            else:
                subject = f"🔔 تذكير: اشتراك {c.name} ينتهي خلال {days_remaining} يوم"
            try:
                html = render_template(
                    "emails/subscription_reminder.html",
                    company=c, days_remaining=days_remaining,
                    threshold=threshold,
                    expires_at=c.subscription_expires_at,
                )
            except Exception:
                # Template doesn't exist — fall back to plain HTML
                html = (f"<p>اشتراك شركتك <b>{c.name}</b> ينتهي خلال "
                        f"<b>{days_remaining} يوم</b> "
                        f"({c.subscription_expires_at.date().isoformat()}). "
                        f"يرجى التواصل لتجديد الاشتراك.</p>")
            sent = send_email(email, subject, html)
            if sent and smtp_configured:
                # Only persist when SMTP actually delivered. In dev /
                # log-only mode, the row is NOT written so the reminder
                # retries next tick once SMTP is wired.
                db.session.add(SubscriptionReminderSent(
                    company_id=c.id, threshold_days=threshold,
                    expires_at_when_sent=c.subscription_expires_at,
                    sent_at=datetime.utcnow(),
                ))
                sent_counts["sent"] += 1
            elif sent and not smtp_configured:
                sent_counts["skipped_smtp_not_configured"] += 1
            # Fired one reminder this tick — done with this company.
            break
    db.session.commit()
    logger.info("Subscription reminders processed: %s", sent_counts)
    return sent_counts
