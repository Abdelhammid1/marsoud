"""MARSOUD-TASK-ARCHIVE-01 — soft archive lifecycle for tasks.

Archiving is purely an `archived_at` flag — no data is destroyed.
Restoring NULLs both columns. The auto-archive cron tick walks all
companies and archives DONE tasks whose `completed_at` (or
`updated_at` fallback) is older than 30 days.

Helpers:
  archive_task(task, *, actor_id)
  unarchive_task(task, *, actor_id=None)          [T-ARCHIVE-MINE: widened]
  archive_all_done_in_company(company_id, *, actor_id) -> count
  auto_archive_old_done(threshold_days=30) -> {company_id: count, ...}
  my_archived_tasks(company_id, user_id) -> Query   [T-ARCHIVE-MINE]
  can_restore_mine(task, user_id) -> bool           [T-ARCHIVE-MINE]
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


def unarchive_task(task, *, actor_id=None):
    """Restore the task to the board. No-op if not archived.

    MARSOUD-TASK-ARCHIVE-MINE (2026-08-08) — widened to accept
    `actor_id` so the personal restore path records WHO restored
    (was anonymous). Backwards-compatible: callers that omit the
    kwarg get the old behaviour (no attribution on the log line).
    """
    if task.archived_at is None:
        return False
    task.archived_at = None
    task.archived_by_id = None
    try:
        from app.services.tasks_extras import log_activity
        log_activity(task, "UNARCHIVED",
                     after={"restored_at": datetime.utcnow().isoformat()},
                     user_id=actor_id)
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


# ─── MARSOUD-TASK-ARCHIVE-MINE (2026-08-08) — per-user archive ────
def my_archived_tasks(company_id, user_id):
    """Archived tasks visible to `user_id` in `company_id`. The
    "visible to me" scope is the exact same union tasks.py uses for
    non-owner Kanban views (legacy assignee OR m2m member OR
    creator) so a user's archive contains exactly the tasks they
    ever saw live.

    Returns a Query (not a list) so callers can .count()/.limit()
    without materialising the full row set.
    """
    from app.services.tasks_extras import visible_tasks_query
    return (visible_tasks_query(company_id, user_id,
                                 full_visibility=False)
            .filter(Task.archived_at.isnot(None))
            .order_by(Task.archived_at.desc()))


def can_restore_mine(task, user_id):
    """True iff `task` is archived AND visible to `user_id` under
    the personal-scope rules. Callers should return 404 (not 403)
    when this is False so we don't confirm a stranger's task id."""
    if task is None or task.archived_at is None:
        return False
    from app.services.tasks_extras import is_visible_to
    return is_visible_to(task, user_id, full_visibility=False)
