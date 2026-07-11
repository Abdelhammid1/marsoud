"""MARSOUD-TASK-SCHEDULE (Abdelhamid 2026-07-11) — create + materialize
scheduled/recurring tasks.

Two responsibilities:

  create_schedule(...)
      Called by the /tasks/new route when the user picked a future
      start date or a DAILY recurrence. Persists a TaskSchedule row
      (the template) — no Task is created yet.

  materialize_due_schedules()
      Called from the daily cron tick. For every active schedule
      whose window includes today AND that hasn't fired today yet,
      creates a fresh Task from the template, wires assignees +
      activity log + notifications (identical to a hand-created
      task), and updates the schedule's bookkeeping.

The generated Task is a fully-independent record; deleting or
editing the schedule after generation doesn't touch already-created
tasks.
"""
from datetime import date, datetime

from app import db
from app.models import (
    Task, TaskStatus, TaskPriority,
    TaskSchedule, task_schedule_assignees,
    RECURRENCE_ONCE, RECURRENCE_DAILY, RECURRENCE_KINDS,
)


class ScheduleError(Exception):
    pass


def create_schedule(*, company_id, created_by_id, title, description,
                       priority, project_id, milestone_id, notes,
                       assignee_ids, recurrence, start_date, end_date):
    """Insert a TaskSchedule row. Callers should have validated the
    business inputs already (title present, assignees non-empty)
    because this function only enforces the invariants the model
    itself depends on (recurrence enum, date ordering)."""
    if recurrence not in RECURRENCE_KINDS:
        raise ScheduleError(f"نوع تكرار غير معروف: {recurrence}")
    if not assignee_ids:
        raise ScheduleError("يجب تحديد مكلَّف واحد على الأقل")
    if not start_date:
        raise ScheduleError("تاريخ البدء مطلوب")
    if recurrence == RECURRENCE_DAILY:
        if not end_date:
            raise ScheduleError("تاريخ الانتهاء مطلوب للتكرار اليومي")
        if end_date < start_date:
            raise ScheduleError("تاريخ الانتهاء قبل تاريخ البدء")
    else:
        # ONCE ignores end_date entirely — force NULL to avoid stale
        # data confusing the materializer.
        end_date = None

    # Priority: allow either raw enum name or a lower-case value.
    try:
        pri = TaskPriority[priority].name if priority else "MEDIUM"
    except KeyError:
        pri = "MEDIUM"

    s = TaskSchedule(
        company_id=company_id,
        title=title, description=description, priority=pri,
        project_id=project_id, milestone_id=milestone_id, notes=notes,
        assigned_to_id=int(assignee_ids[0]),
        created_by_id=created_by_id,
        recurrence=recurrence,
        start_date=start_date, end_date=end_date,
        active=True,
    )
    db.session.add(s); db.session.flush()

    # Assignees — mirror the tasks.task_assignees shape.
    for uid in assignee_ids:
        db.session.execute(task_schedule_assignees.insert().values(
            schedule_id=s.id, user_id=int(uid),
        ))
    db.session.commit()
    return s


def materialize_due_schedules(today=None):
    """Create Task rows for every schedule whose window includes
    today and that hasn't already fired today. Returns a small
    summary dict for the cron log.

    Idempotent: safe to call multiple times per day. The
    `last_generated_date == today` guard prevents duplicate
    generation if the cron double-fires."""
    from app.services.tasks_extras import set_assignees, log_activity
    from app.services.crm import CRMError

    today = today or date.today()
    fired = 0
    deactivated = 0
    errors = []

    schedules = TaskSchedule.query.filter_by(active=True).all()
    for s in schedules:
        try:
            # Window check — anything BEFORE start_date is waiting.
            if s.start_date > today:
                continue
            # DAILY schedules past their end date deactivate; ONCE
            # schedules deactivate the moment they fire (below).
            if (s.recurrence == RECURRENCE_DAILY
                    and s.end_date is not None
                    and today > s.end_date):
                s.active = False
                deactivated += 1
                continue
            # Dedupe — don't regenerate on repeat cron ticks the
            # same day.
            if (s.last_generated_date is not None
                    and s.last_generated_date >= today):
                continue

            _spawn_task_from_schedule(s)
            s.last_generated_date = today
            s.generated_count += 1
            fired += 1

            # ONCE schedules retire immediately.
            if s.recurrence == RECURRENCE_ONCE:
                s.active = False
                deactivated += 1
            db.session.commit()
        except (CRMError, Exception) as e:  # noqa: BLE001
            db.session.rollback()
            errors.append(f"schedule {s.id}: {type(e).__name__}: {e}")

    return {"fired": fired,
            "deactivated": deactivated,
            "errors": errors}


def _spawn_task_from_schedule(s):
    """Create a Task using the schedule's template. Assignees, activity
    log entry, and notifications are wired identically to a manually
    created task — the recipient can't tell it came from a schedule."""
    from app.services.tasks_extras import set_assignees, log_activity

    try:
        priority = TaskPriority[s.priority]
    except KeyError:
        priority = TaskPriority.MEDIUM

    t = Task(
        company_id=s.company_id,
        title=s.title,
        description=s.description,
        project_id=s.project_id,
        milestone_id=s.milestone_id,
        assigned_to_id=s.assigned_to_id,
        created_by_id=s.created_by_id,
        priority=priority,
        status=TaskStatus.TODO,
        notes=s.notes,
    )
    db.session.add(t); db.session.flush()

    # Snapshot assignee ids from the template's M2M.
    from sqlalchemy import select
    ids = [row[0] for row in db.session.execute(
        select(task_schedule_assignees.c.user_id)
        .where(task_schedule_assignees.c.schedule_id == s.id)
    ).fetchall()]
    if not ids:
        ids = [s.assigned_to_id]

    # set_assignees fires notifications to the freshly added user
    # ids — the schedule owner isn't excluded on purpose (they may
    # be assigning to themselves, which is fine).
    set_assignees(t, ids, actor_id=s.created_by_id)
    log_activity(
        t, "CREATED",
        after={"title": t.title,
               "ids": ids,
               "from_schedule_id": s.id},
        user_id=s.created_by_id,
    )
    # set_assignees already commits, but the activity log entry
    # was added after — flush it to make sure it's in the same TX.
    db.session.flush()
    return t
