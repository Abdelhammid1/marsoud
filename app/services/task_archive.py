"""MARSOUD-TASK-ARCHIVE-01 — soft archive lifecycle for tasks.

Archiving is purely an `archived_at` flag — no data is destroyed.
Restoring NULLs both columns. The auto-archive cron tick walks all
companies and archives DONE tasks whose `completed_at` (or
`updated_at` fallback) is older than 30 days.

Helpers:
  archive_task(task, *, actor_id)
  unarchive_task(task)
  archive_all_done_in_company(company_id, *, actor_id) -> count
  auto_archive_old_done(threshold_days=30) -> {company_id: count, ...}
"""
from datetime import datetime, timedelta
from app import db
from app.models import Task, TaskStatus, Company


AUTO_ARCHIVE_DAYS = 30


def archive_task(task, *, actor_id=None):
    """Flip archived_at on the task. No-op if already archived."""
    if task.archived_at is not None:
        return False
    task.archived_at = datetime.utcnow()
    task.archived_by_id = actor_id
    # Log to activity (best-effort — never blocks)
    try:
        from app.services.tasks_extras import log_activity
        log_activity(task, "ARCHIVED",
                     after={"archived_at": task.archived_at.isoformat()},
                     user_id=actor_id)
    except Exception:
        pass
    db.session.commit()
    return True


def unarchive_task(task):
    """Restore the task to the board. No-op if not archived."""
    if task.archived_at is None:
        return False
    task.archived_at = None
    task.archived_by_id = None
    try:
        from app.services.tasks_extras import log_activity
        log_activity(task, "UNARCHIVED",
                     after={"restored_at": datetime.utcnow().isoformat()})
    except Exception:
        pass
    db.session.commit()
    return True


def archive_all_done_in_company(company_id, *, actor_id=None):
    """Archive every DONE + non-archived task in the company at once.
    Returns the count archived. Used by the "Archive all done" button
    at the top of the DONE column."""
    rows = Task.query.filter(
        Task.company_id == company_id,
        Task.status == TaskStatus.DONE,
        Task.archived_at.is_(None),
    ).all()
    n = 0
    now = datetime.utcnow()
    for t in rows:
        t.archived_at = now
        t.archived_by_id = actor_id
        n += 1
    if n:
        db.session.commit()
    return n


def auto_archive_old_done(threshold_days=AUTO_ARCHIVE_DAYS):
    """Walked by the cron tick. Archives DONE tasks whose completion
    date is older than `threshold_days` across every active company.

    The completion timestamp falls back to `updated_at` when
    `completed_at` is missing (older rows pre-MARSOUD-TASKS-02).
    Returns {company_id: count_archived}."""
    cutoff = datetime.utcnow() - timedelta(days=threshold_days)
    summary = {}
    rows = Task.query.filter(
        Task.status == TaskStatus.DONE,
        Task.archived_at.is_(None),
    ).all()
    now = datetime.utcnow()
    for t in rows:
        ts = t.completed_at or t.updated_at
        if not ts or ts > cutoff:
            continue
        t.archived_at = now
        t.archived_by_id = None   # system action
        summary[t.company_id] = summary.get(t.company_id, 0) + 1
    if summary:
        db.session.commit()
    return summary
