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


def _mark_sent(invoice_id, kind, days):
    db.session.add(InvoiceReminderSent(
        invoice_id=invoice_id, threshold_kind=kind, threshold_days=days,
        sent_at=datetime.utcnow(),
    ))


def process_invoice_reminders():
    """Single pass — call from a cron tick. Returns a summary dict."""
    today = date.today()
    sent_counts = {"before": 0, "overdue": 0, "skipped": 0}

    # Cache reminder configs per company to avoid N+1
    company_cfg = {}
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
        if cfg is None:
            company = db.session.get(Company, inv.company_id)
            cfg = company.reminders if company else {}
            company_cfg[inv.company_id] = cfg

        if not cfg.get("enabled", True):
            sent_counts["skipped"] += 1
            continue

        days_until = (inv.due_date - today).days
        # before-due-date thresholds
        for d in cfg.get("days_before", []):
            if days_until == d and not _already_sent(inv.id, "before", d):
                if send_overdue_reminder(inv, f"before_{d}"):
                    _mark_sent(inv.id, "before", d)
                    sent_counts["before"] += 1
        # overdue thresholds (days past due)
        days_overdue = -days_until  # positive = past due
        for d in cfg.get("overdue_days", []):
            if days_overdue == d and days_overdue >= 0 and not _already_sent(inv.id, "overdue", d):
                if send_overdue_reminder(inv, "overdue" if d == 0 else f"overdue_{d}"):
                    _mark_sent(inv.id, "overdue", d)
                    if inv.status != InvoiceStatus.OVERDUE and days_overdue > 0:
                        inv.status = InvoiceStatus.OVERDUE
                    sent_counts["overdue"] += 1

    db.session.commit()
    logger.info("Reminders processed: %s", sent_counts)
    return sent_counts


# ─── MARSOUD-57.3: subscription expiry reminders ─────────────────────────
SUBSCRIPTION_THRESHOLDS = [7, 3, 1, 0]


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
    """Single pass — scan companies, fire expiry reminders for thresholds
    that match their days-remaining and haven't been sent yet. Returns a
    summary dict."""
    today = date.today()
    sent_counts = {"sent": 0, "skipped_no_email": 0,
                   "skipped_already_sent": 0, "skipped_no_expiry": 0}
    companies = Company.query.filter_by(is_active=True).all()
    for c in companies:
        if not c.subscription_expires_at:
            sent_counts["skipped_no_expiry"] += 1
            continue
        days_remaining = (c.subscription_expires_at.date() - today).days
        for threshold in SUBSCRIPTION_THRESHOLDS:
            if days_remaining != threshold:
                continue
            already = SubscriptionReminderSent.query.filter_by(
                company_id=c.id,
                threshold_days=threshold,
                expires_at_when_sent=c.subscription_expires_at,
            ).first()
            if already:
                sent_counts["skipped_already_sent"] += 1
                continue
            email = _company_owner_email(c)
            if not email:
                sent_counts["skipped_no_email"] += 1
                continue
            # Build the email body
            if threshold == 0:
                subject = f"اشتراك {c.name} ينتهي اليوم"
            else:
                subject = f"اشتراك {c.name} ينتهي خلال {threshold} يوم"
            try:
                html = render_template(
                    "emails/subscription_reminder.html",
                    company=c, days_remaining=threshold,
                    expires_at=c.subscription_expires_at,
                )
            except Exception:
                # Template doesn't exist — fall back to plain HTML
                html = (f"<p>اشتراك شركتك <b>{c.name}</b> ينتهي خلال "
                        f"<b>{threshold} يوم</b> ({c.subscription_expires_at.date().isoformat()}). "
                        f"يرجى التواصل لتجديد الاشتراك.</p>")
            if send_email(email, subject, html):
                db.session.add(SubscriptionReminderSent(
                    company_id=c.id, threshold_days=threshold,
                    expires_at_when_sent=c.subscription_expires_at,
                    sent_at=datetime.utcnow(),
                ))
                sent_counts["sent"] += 1
    db.session.commit()
    logger.info("Subscription reminders processed: %s", sent_counts)
    return sent_counts
