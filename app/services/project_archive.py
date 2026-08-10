"""MARSOUD-PROJECT-ARCHIVE (2026-08-10) — soft archive lifecycle
for projects.

Mirrors app/services/task_archive.py structurally: archived_at
+ archived_by_id columns, both cleared on unarchive, no data
destroyed. Two things differ from the task-side:

  1. **Orthogonal to status.** A project can be archived at
     any status (Planning, In Progress, Delivered — whatever).
     This deliberately routes around the AC-09 gate on
     CLIENT_FEEDBACK → CLOSED (app/services/crm.py:236-240):
     a project delivered internally without a customer-feedback
     loop can still be "put away" without invoking that
     transition. Users' "finished" isn't the same as "client
     feedback approved".

  2. **Audit line goes on ProjectStatusEvent, not a new
     activity table.** The project timeline reads from
     ProjectStatusEvent already; slotting the archive line in
     there (from_status=to_status=current, note='__ARCHIVED__')
     keeps everything in one feed without inventing a fake
     status enum value.
"""
from datetime import datetime
from app import db
from app.models import Project, ProjectStatusEvent


class ProjectArchiveError(Exception):
    pass


def archive_project(project, *, actor_id=None):
    """Flip archived_at on the project. No-op if already
    archived (returns False so callers can distinguish)."""
    if project.archived_at is not None:
        return False
    project.archived_at = datetime.utcnow()
    project.archived_by_id = actor_id
    _log_event(project, actor_id, "ARCHIVED")
    db.session.commit()
    return True


def unarchive_project(project, *, actor_id=None):
    """Restore the project to the active list. No-op if not
    archived. Doesn't touch status, progress_pct, or any
    other field — pure reversal of archive_project."""
    if project.archived_at is None:
        return False
    project.archived_at = None
    project.archived_by_id = None
    _log_event(project, actor_id, "UNARCHIVED")
    db.session.commit()
    return True


def archived_projects_for(user, company_id, *, full_view=False):
    """Return archived, non-soft-deleted projects the user
    can see, most-recently-archived first.

    full_view=True (owner/admin) → every archived project in
    the company. full_view=False (project_manager and below)
    → only projects the user personally manages, matching the
    existing _user_can_see_project scope at projects.py:80.
    """
    q = Project.query.filter(
        Project.company_id == company_id,
        Project.archived_at.isnot(None),
        Project.deleted_at.is_(None),
    )
    if not full_view:
        q = q.filter(Project.manager_id == user.id)
    return q.order_by(Project.archived_at.desc()).all()


def _log_event(project, actor_id, note):
    """Write a ProjectStatusEvent row so the archive/unarchive
    action shows up in the project's timeline alongside status
    changes. from_status=to_status=current keeps the enum
    values honest; the sentinel note ('__ARCHIVED__' /
    '__UNARCHIVED__') is what the feed template keys on to
    render the archive line differently from a real status
    move. Best-effort — a logging failure never blocks the
    archive commit."""
    try:
        ev = ProjectStatusEvent(
            project_id=project.id,
            from_status=project.status,
            to_status=project.status,
            changed_by_id=actor_id,
            note=f"__{note}__",
        )
        db.session.add(ev)
    except Exception:
        pass
