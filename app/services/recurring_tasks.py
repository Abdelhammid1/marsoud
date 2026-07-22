"""MARSOUD-RECURRING-TASKS (Abdelhamid 2026-07-22).

Turn an existing Task into a recurring series + generate future
occurrences from cron.
"""
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from app import db
from app.models import (
    Task, RecurringTaskSeries, RecurringTaskException,
    FREQUENCIES, END_CONDITIONS,
    FREQ_DAILY, FREQ_WEEKLY, FREQ_MONTHLY, FREQ_YEARLY, FREQ_CUSTOM,
    END_NEVER, END_AFTER_N, END_ON_DATE,
)


class RecurringTaskError(Exception):
    """Domain-specific so route code can catch it separately."""


APPLY_THIS = "THIS"
APPLY_THIS_AND_FUTURE = "THIS_AND_FUTURE"
APPLY_ALL = "ALL"
APPLY_MODES = (APPLY_THIS, APPLY_THIS_AND_FUTURE, APPLY_ALL)


def promote_to_recurring(task, *, frequency, interval_count=1,
                         end_condition=END_NEVER,
                         end_count=None, end_date=None,
                         exception_dates=None):
    """Convert an existing Task into the first occurrence of a series.

    The task's deadline becomes the anchor date. If no deadline is
    set, today is used. Fails if the task is already part of a series.
    """
    if task.recurring_series_id:
        raise RecurringTaskError("المهمة تنتمي لسلسلة تكرار بالفعل.")
    if frequency not in FREQUENCIES:
        raise RecurringTaskError(f"وتيرة غير معروفة: {frequency}")
    if end_condition not in END_CONDITIONS:
        raise RecurringTaskError(f"شرط إنهاء غير معروف: {end_condition}")
    if end_condition == END_AFTER_N and (
            not end_count or end_count < 1):
        raise RecurringTaskError("عدد مرات التكرار يجب أن يكون رقم موجب.")
    if end_condition == END_ON_DATE and not end_date:
        raise RecurringTaskError("تاريخ الإنهاء مطلوب.")
    if interval_count < 1:
        raise RecurringTaskError("الفاصل بين التكرارات يجب أن يكون 1 على الأقل.")

    anchor = task.deadline or date.today()
    series = RecurringTaskSeries(
        company_id=task.company_id,
        template_task_id=task.id,
        frequency=frequency,
        interval_count=interval_count,
        end_condition=end_condition,
        end_count=end_count,
        end_date=end_date,
        start_date=anchor,
        last_generated_date=anchor,
        generated_count=1,
    )
    db.session.add(series); db.session.flush()

    task.recurring_series_id = series.id
    task.occurrence_index = 1

    # Skip-date exceptions.
    for d in (exception_dates or []):
        db.session.add(RecurringTaskException(
            series_id=series.id, skip_date=d))

    db.session.flush()
    return series


def _next_date(current, frequency, interval_count):
    if frequency == FREQ_DAILY:
        return current + timedelta(days=interval_count)
    if frequency == FREQ_WEEKLY:
        return current + timedelta(days=7 * interval_count)
    if frequency == FREQ_MONTHLY:
        return current + relativedelta(months=interval_count)
    if frequency == FREQ_YEARLY:
        return current + relativedelta(years=interval_count)
    if frequency == FREQ_CUSTOM:
        # Same as DAILY but the number of days IS the interval.
        return current + timedelta(days=interval_count)
    raise RecurringTaskError(f"وتيرة غير معروفة: {frequency}")


def generate_due_occurrences(today=None):
    """Cron entry point. For each active series whose next occurrence
    is ≤ today, materialise a new Task and stamp it with the series
    link. Skip dates in the exception list.
    """
    today = today or date.today()
    made = []
    series_rows = RecurringTaskSeries.query.filter_by(active=True).all()
    for s in series_rows:
        # Guard against runaway loops.
        for _ in range(50):
            nxt = _next_date(s.last_generated_date, s.frequency,
                             s.interval_count)
            if nxt > today:
                break
            if _series_end_reached(s, nxt):
                s.active = False
                break
            skip = _is_skipped(s.id, nxt)
            if not skip:
                new_task = _clone_task(s, nxt)
                made.append(new_task)
                s.generated_count += 1
            s.last_generated_date = nxt
            if _series_end_reached(s, nxt):
                s.active = False
                break
    db.session.commit()
    return made


def _series_end_reached(series, next_date):
    if series.end_condition == END_NEVER:
        return False
    if series.end_condition == END_AFTER_N:
        return series.generated_count >= (series.end_count or 0)
    if series.end_condition == END_ON_DATE:
        return series.end_date and next_date > series.end_date
    return False


def _is_skipped(series_id, d):
    return RecurringTaskException.query.filter_by(
        series_id=series_id, skip_date=d).first() is not None


def _clone_task(series, deadline_date):
    template = series.template_task
    new_task = Task(
        company_id=template.company_id,
        title=template.title,
        description=template.description,
        project_id=template.project_id,
        milestone_id=template.milestone_id,
        assigned_to_id=template.assigned_to_id,
        created_by_id=template.created_by_id,
        priority=template.priority,
        # New occurrence starts fresh, not DONE.
        deadline=deadline_date,
        notes=template.notes,
        recurring_series_id=series.id,
        occurrence_index=series.generated_count + 1,
    )
    db.session.add(new_task); db.session.flush()
    return new_task


def delete_series(series, *, mode=APPLY_ALL, from_task=None):
    """Delete side of the recurring flow.
      · APPLY_THIS       — delete only the passed Task (leave series).
      · APPLY_THIS_AND_FUTURE — delete this occurrence + stop future
        generations (set active=False + set end_condition=END_DATE
        anchored yesterday).
      · APPLY_ALL        — delete every Task in the series + the
        series row itself.
    """
    if mode not in APPLY_MODES:
        raise RecurringTaskError(f"وضع غير معروف: {mode}")
    if mode == APPLY_THIS:
        if not from_task:
            raise RecurringTaskError("مهمة مطلوبة للحذف الفردي.")
        db.session.delete(from_task)
    elif mode == APPLY_THIS_AND_FUTURE:
        if not from_task:
            raise RecurringTaskError("مهمة مطلوبة.")
        series.active = False
        series.end_condition = END_ON_DATE
        series.end_date = from_task.deadline or date.today()
        # Delete the passed task and every occurrence AFTER it.
        cutoff = from_task.occurrence_index or 0
        Task.query.filter(
            Task.recurring_series_id == series.id,
            Task.occurrence_index >= cutoff,
        ).delete(synchronize_session=False)
    else:   # APPLY_ALL
        Task.query.filter_by(
            recurring_series_id=series.id).delete(synchronize_session=False)
        db.session.delete(series)
    db.session.commit()
