"""Journal management beyond posting: pause, reactivate, templates, recurring."""
from datetime import datetime, date, timedelta
from app import db
from app.models import (
    JournalEntry, JournalLine, JournalAudit, JournalAction,
    RecurringJournalLog, RecurringAction,
)
from app.services.ledger import LedgerError, post_journal
from app.services.time import today_in_company_tz


def pause_entry(entry, reason, user_id):
    if not entry.is_active:
        raise LedgerError("القيد موقوف بالفعل")
    if entry.is_reversal:
        raise LedgerError("لا يمكن إيقاف قيد عكسي")
    if not (reason or "").strip():
        raise LedgerError("سبب الإيقاف مطلوب")

    entry.is_active = False
    entry.pause_reason = reason.strip()
    entry.paused_by_id = user_id
    entry.paused_at = datetime.utcnow()
    db.session.add(JournalAudit(
        entry_id=entry.id, user_id=user_id,
        action=JournalAction.PAUSED, reason=reason.strip(),
    ))
    db.session.commit()
    try:
        from app.services.activity import log_action
        log_action(action_type="UPDATE", entity_type="journal",
                   entity_id=entry.id,
                   entity_label=f"إيقاف قيد {entry.number or entry.id}",
                   company_id=entry.company_id,
                   extra_data={"action": "PAUSED", "reason": reason})
    except Exception:
        pass
    return entry


def reactivate_entry(entry, reason, user_id):
    if entry.is_active:
        raise LedgerError("القيد نشط بالفعل")
    if not (reason or "").strip():
        raise LedgerError("سبب إعادة التنشيط مطلوب")

    entry.is_active = True
    entry.reactivate_reason = reason.strip()
    entry.reactivated_by_id = user_id
    entry.reactivated_at = datetime.utcnow()
    db.session.add(JournalAudit(
        entry_id=entry.id, user_id=user_id,
        action=JournalAction.REACTIVATED, reason=reason.strip(),
    ))
    db.session.commit()
    try:
        from app.services.activity import log_action
        log_action(action_type="UPDATE", entity_type="journal",
                   entity_id=entry.id,
                   entity_label=f"إعادة تنشيط قيد {entry.number or entry.id}",
                   company_id=entry.company_id,
                   extra_data={"action": "REACTIVATED", "reason": reason})
    except Exception:
        pass
    return entry


def post_from_template(company_id, template, entry_date=None, description=None, created_by=None):
    """Create a balanced journal from a saved template."""
    lines = [
        {"account_id": tl.account_id, "debit": float(tl.debit), "credit": float(tl.credit), "memo": tl.memo}
        for tl in template.lines
    ]
    return post_journal(
        company_id=company_id,
        description=description or f"من قالب: {template.name}",
        lines=lines,
        entry_date=entry_date,
        created_by=created_by,
        source_type="template",
        source_id=template.id,
    )


def _advance_date(current, frequency):
    from app.models import RecurrenceFrequency
    if frequency == RecurrenceFrequency.DAILY:
        return current + timedelta(days=1)
    if frequency == RecurrenceFrequency.WEEKLY:
        return current + timedelta(weeks=1)
    if frequency == RecurrenceFrequency.MONTHLY:
        # Naive month math: same day next month, snap to 28 if needed
        m = current.month + 1
        y = current.year + (1 if m > 12 else 0)
        m = ((m - 1) % 12) + 1
        d = min(current.day, 28)
        return date(y, m, d)
    if frequency == RecurrenceFrequency.YEARLY:
        return date(current.year + 1, current.month, current.day)
    return current


def process_recurring_journals():
    """Run every due recurring schedule, catching up ALL missed periods in one tick.

    Called from /cron/tick. For each active schedule:
      - Use `today_in_company_tz(sched.company)` so the boundary respects the
        company's timezone (default Asia/Riyadh), not the server's.
      - Inner `while` loop posts every missed period until `next_run_date > today`
        or the schedule's `end_date` is reached.
      - Each iteration writes a RecurringJournalLog (EXECUTE on success, FAIL on
        exception so the user can see what broke).
    """
    from app.models import RecurringJournal
    import logging
    logger = logging.getLogger("ledgeros.recurring")

    due = RecurringJournal.query.filter(
        RecurringJournal.is_active.is_(True),
        RecurringJournal.is_deleted.is_(False),
    ).all()

    posted_total = 0
    failed_total = 0
    for sched in due:
        today = today_in_company_tz(sched.company)
        # If schedule ended already, deactivate quietly
        if sched.end_date and sched.next_run_date > sched.end_date:
            sched.is_active = False
            continue

        while sched.next_run_date <= today:
            if sched.end_date and sched.next_run_date > sched.end_date:
                sched.is_active = False
                break

            period = sched.next_run_date
            try:
                entry = post_from_template(
                    sched.company_id, sched.template,
                    entry_date=period,
                    description=f"{sched.name} ({period.isoformat()})",
                )
                db.session.add(RecurringJournalLog(
                    recurring_id=sched.id,
                    action=RecurringAction.EXECUTE,
                    period_posted=period,
                    journal_entry_id=entry.id,
                ))
                posted_total += 1
            except Exception as e:
                logger.exception("Failed to post recurring journal %s for %s", sched.id, period)
                db.session.add(RecurringJournalLog(
                    recurring_id=sched.id,
                    action=RecurringAction.FAIL,
                    period_posted=period,
                    error_message=str(e)[:500],
                ))
                failed_total += 1
                # Don't advance on failure — we'll retry next tick. Break out to
                # avoid infinite loop within this tick.
                break

            sched.next_run_date = _advance_date(sched.next_run_date, sched.frequency)
            if sched.end_date and sched.next_run_date > sched.end_date:
                sched.is_active = False
                break

    db.session.commit()
    return {"posted": posted_total, "failed": failed_total, "due_schedules": len(due)}


def log_recurring_action(schedule, action, user_id, reason=None):
    """Convenience: append a log row for edit/stop/resume/delete actions."""
    db.session.add(RecurringJournalLog(
        recurring_id=schedule.id,
        action=action,
        reason=reason,
        created_by=user_id,
    ))
