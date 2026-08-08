"""MARSOUD-TASKS-02 — task helpers.

Multi-assignee management, activity logging, comment posting, deadline
helpers, and notification fan-out for the tasks module.

The legacy single-assignee field ``tasks.assigned_to_id`` is preserved
as the "primary assignee" (used by older code paths) and is mirrored
into / from the new ``task_assignees`` table:

  - When a primary assignee is set, they are always also a member of
    ``task_assignees`` (idempotent insert).
  - When the primary assignee is removed from the multi-assignee set,
    the next member is promoted to primary (so the legacy queries that
    still read ``assigned_to_id`` keep working). If the set becomes
    empty we fall back to the original primary (assigned_to_id stays
    set; UI shows them as the sole assignee).

The activity log captures every state-changing operation on the task
so the detail page can render a chronological feed. Notification
fan-out hooks into create, assign, comment, and status-change events.
"""
import json
from datetime import datetime, date, timedelta
from flask import g, current_app
from flask_login import current_user
from sqlalchemy import select, or_, and_, func

from app import db
from app.models import (
    Task, TaskStatus, TaskPriority, TaskComment, TaskActivityLog,
    task_assignees, User, NotificationKind, Notification,
)
from app.models.user import user_companies


class TaskError(ValueError):
    """Raised when an operation cannot be applied to a task."""


# ─── Internal helpers ─────────────────────────────────────────────────────
def _safe_user_full_name(user_id):
    u = db.session.get(User, user_id) if user_id else None
    return u.full_name if u else "—"


def _company_id_for_task(task):
    return task.company_id


def _notify(user_id, *, company_id, kind, title, body=None, link_url=None,
            task=None):
    """Best-effort notification insert + email fan-out.

    MARSOUD — every in-app task notification is now mirrored as an email
    to the recipient (when their address is set and SMTP is configured).
    The email send is wrapped in try/except so a transient SMTP failure
    never blocks the task workflow — the in-app bell still ticks.
    """
    try:
        n = Notification(
            user_id=user_id, company_id=company_id,
            kind=kind.value if hasattr(kind, "value") else kind,
            title=title, body=body, link_url=link_url,
        )
        db.session.add(n)
    except Exception:
        current_app.logger.exception("notification insert failed")

    # MARSOUD-NOTIF-EMAIL-SCOPE (Abdelhamid 2026-07-15) — restrict
    # email fan-out to the two events he actually wants inbox spam
    # for: (1) getting assigned to a task, (2) being @-mentioned in
    # a comment (mentions are emailed on their own path in
    # app/services/mentions.py, unchanged here). Everything else —
    # status changes, comments, updates, deadline pings — stays
    # in-app-only via the bell. In-app notifications for all kinds
    # still fire above (unchanged).
    _EMAIL_ALLOWED_KINDS = {"TASK_ASSIGNED"}
    try:
        kind_value = kind.value if hasattr(kind, "value") else str(kind)
        if kind_value not in _EMAIL_ALLOWED_KINDS or task is None:
            return
        from app.models import User
        recipient = User.query.get(user_id)
        if not recipient:
            return
        from app.services.email import send_task_notification_email
        send_task_notification_email(
            recipient, task, kind=kind_value, title=title, body_text=body,
        )
    except Exception:
        current_app.logger.exception(
            "task notification email send failed (in-app notification "
            "still recorded)"
        )


# ─── Multi-assignee management ────────────────────────────────────────────
def assignee_ids_for(task):
    rows = db.session.execute(
        select(task_assignees.c.user_id)
        .where(task_assignees.c.task_id == task.id)
    ).fetchall()
    ids = {r[0] for r in rows}
    if task.assigned_to_id:
        ids.add(task.assigned_to_id)
    return ids


def watchers_for(task, *, exclude=None):
    """MARSOUD-TASK-NOTIFY-CREATOR — one central function that returns
    every user who should be pinged when a task changes: the current
    assignee set PLUS the person who created the task, minus any
    ids the caller wants filtered out (typically the actor
    themselves).

    Motivation: previously updates only pinged assignees, so a
    project manager who created a task and handed it off never
    heard back when the assignee moved it forward. Every notify
    site in this module (status flip, inline edit, comment, full
    edit) now routes through this helper.
    """
    exclude = set(exclude or [])
    recipients = assignee_ids_for(task)
    if task.created_by_id:
        recipients.add(task.created_by_id)
    return recipients - exclude


def set_assignees(task, user_ids, *, actor_id=None):
    """Replace the assignee set, sync primary, log, and notify new members."""
    new_ids = {int(x) for x in user_ids if x}
    if not new_ids:
        raise TaskError("يجب تحديد مكلَّف واحد على الأقل")

    cid = task.company_id
    valid_ids = {
        r[0] for r in db.session.execute(
            select(user_companies.c.user_id)
            .where(user_companies.c.company_id == cid)
        ).fetchall()
    }
    invalid = new_ids - valid_ids
    if invalid:
        raise TaskError("بعض المستخدمين لا ينتمون لهذه الشركة")

    # old_ids من جدول task_assignees مباشرةً (مش assignee_ids_for) — عشان
    # ميتضمّش assigned_to_id الـ legacy، اللي بيتحط وقت الإنشاء ويخلي added فاضية.
    old_ids = {
        r[0] for r in db.session.execute(
            select(task_assignees.c.user_id)
            .where(task_assignees.c.task_id == task.id)
        ).fetchall()
    }
    added = new_ids - old_ids
    removed = old_ids - new_ids

    # Wipe existing rows then insert the new set.
    db.session.execute(
        task_assignees.delete().where(task_assignees.c.task_id == task.id)
    )
    for uid in new_ids:
        db.session.execute(task_assignees.insert().values(
            task_id=task.id, user_id=uid,
            assigned_by_id=actor_id or (
                current_user.id if current_user and current_user.is_authenticated else None
            ),
        ))

    # Sync legacy primary — keep current if still in set; else pick smallest id.
    if task.assigned_to_id not in new_ids:
        task.assigned_to_id = sorted(new_ids)[0]

    log_activity(task, "ASSIGNEES_CHANGED",
                 before={"ids": sorted(old_ids)},
                 after={"ids": sorted(new_ids)})

    # Notify newly added users (skip the actor themselves).
    for uid in added:
        if actor_id and uid == actor_id:
            continue
        _notify(uid, company_id=cid,
                kind=NotificationKind.TASK_ASSIGNED,
                title=f"📌 تم تكليفك بمهمة: {task.title}",
                body=(task.description or "")[:200],
                link_url=f"/tasks/{task.id}",
                task=task)

    db.session.commit()
    return new_ids, added, removed


# ─── Activity log ─────────────────────────────────────────────────────────
def log_activity(task, action, *, before=None, after=None, user_id=None):
    entry = TaskActivityLog(
        task_id=task.id,
        company_id=task.company_id,
        user_id=user_id or (
            current_user.id if current_user and current_user.is_authenticated else None
        ),
        action=action,
        before_json=json.dumps(before, ensure_ascii=False, default=str)
            if before is not None else None,
        after_json=json.dumps(after, ensure_ascii=False, default=str)
            if after is not None else None,
    )
    db.session.add(entry)
    return entry


def _user_link_html(user_id, display_name):
    """MARSOUD-CLICKABLE-ASSIGNEE (Abdelhamid 2026-07-16) — wrap a
    user's display name in an anchor pointing at their tasks board
    (/tasks/?scope=employees&user_id=<id>). System-authored entries
    keep the plain "النظام" label — no id, no link."""
    from markupsafe import escape
    if not user_id:
        return str(escape(display_name))
    return (
        f'<a href="/tasks/?scope=employees&user_id={int(user_id)}" '
        f'class="text-brand-600 hover:underline font-semibold">'
        f'{escape(display_name)}</a>'
    )


def activity_description(entry):
    """Render an activity entry as a short Arabic phrase.

    MARSOUD-CLICKABLE-ASSIGNEE — the actor name + any referenced
    assignee names are wrapped in anchors pointing at their tasks
    board. Returns Markup so Jinja auto-escapes surrounding text
    while trusting our anchors.
    """
    from markupsafe import escape, Markup
    a = entry.action
    user_name = entry.user.full_name if entry.user else "النظام"
    user_link = _user_link_html(
        entry.user.id if entry.user else None, user_name)
    try:
        before = json.loads(entry.before_json) if entry.before_json else {}
        after = json.loads(entry.after_json) if entry.after_json else {}
    except (TypeError, ValueError):
        before, after = {}, {}

    def _esc(x):
        return str(escape(x)) if x is not None else ""

    if a == "CREATED":
        return Markup(f"{user_link} أنشأ هذه المهمة")
    if a == "STATUS_CHANGED":
        try:
            o = TaskStatus[before.get("status", "TODO")].label_ar
            n = TaskStatus[after.get("status", "TODO")].label_ar
        except KeyError:
            o, n = before.get("status"), after.get("status")
        return Markup(
            f"{user_link} غيّر الحالة من «{_esc(o)}» إلى «{_esc(n)}»")
    if a == "PRIORITY_CHANGED":
        try:
            o = TaskPriority[before.get("priority", "MEDIUM")].label_ar
            n = TaskPriority[after.get("priority", "MEDIUM")].label_ar
        except KeyError:
            o, n = before.get("priority"), after.get("priority")
        return Markup(
            f"{user_link} غيّر الأولوية من «{_esc(o)}» إلى «{_esc(n)}»")
    if a == "DEADLINE_CHANGED":
        return Markup(
            f"{user_link} حدّث الموعد النهائي إلى "
            f"{_esc(after.get('deadline') or '—')}")
    if a == "TITLE_CHANGED":
        return Markup(f"{user_link} حدّث العنوان")
    if a == "DESCRIPTION_CHANGED":
        return Markup(f"{user_link} حدّث الوصف")
    if a == "ASSIGNEES_CHANGED":
        added_ids = set(after.get("ids", [])) - set(before.get("ids", []))
        removed_ids = set(before.get("ids", [])) - set(after.get("ids", []))
        # Link each added/removed name so the reader can jump to
        # their tasks board directly.
        def _linked_name(uid):
            return _user_link_html(uid, _safe_user_full_name(uid))
        bits = []
        if added_ids:
            bits.append("أضاف " + "، ".join(_linked_name(i) for i in added_ids))
        if removed_ids:
            bits.append("أزال " + "، ".join(_linked_name(i) for i in removed_ids))
        tail = " و".join(bits) if bits else "حدّث المكلَّفين"
        return Markup(f"{user_link} {tail}")
    if a == "COMMENT_ADDED":
        return Markup(f"{user_link} علّق على المهمة")
    return Markup(f"{user_link}: {_esc(a)}")


# ─── Comments ─────────────────────────────────────────────────────────────
def add_comment(task, content, *, user_id=None,
                attachment_url=None, attachment_name=None):
    text = (content or "").strip()
    if not text:
        raise TaskError("التعليق فارغ")
    uid = user_id or (current_user.id if current_user.is_authenticated else None)
    if not uid:
        raise TaskError("يجب تسجيل الدخول")

    c = TaskComment(
        task_id=task.id, user_id=uid, company_id=task.company_id,
        content=text,
        attachment_url=attachment_url, attachment_name=attachment_name,
    )
    db.session.add(c)
    log_activity(task, "COMMENT_ADDED",
                 after={"preview": text[:100]}, user_id=uid)

    # Notify everyone watching the task except the commenter — this
    # now includes the CREATOR so a PM who opened the task hears back
    # every time the assignee posts an update.
    recipients = watchers_for(task, exclude={uid})
    for rid in recipients:
        _notify(rid, company_id=task.company_id,
                kind=NotificationKind.TASK_COMMENT,
                title=f"💬 تعليق جديد على: {task.title}",
                body=text[:200],
                link_url=f"/tasks/{task.id}",
                task=task)

    db.session.commit()
    return c


# ─── Status / inline edits ────────────────────────────────────────────────
def apply_inline_edit(task, *, title=None, description=None,
                      priority=None, deadline=None, status=None,
                      user_id=None):
    """Apply a partial update + log only the fields that actually changed."""
    uid = user_id or (current_user.id if current_user.is_authenticated else None)
    changed = False

    if title is not None and title.strip() and title.strip() != task.title:
        log_activity(task, "TITLE_CHANGED",
                     before={"title": task.title},
                     after={"title": title.strip()}, user_id=uid)
        task.title = title.strip()
        changed = True

    if description is not None:
        new_desc = (description or "").strip() or None
        if new_desc != task.description:
            log_activity(task, "DESCRIPTION_CHANGED",
                         before={"description": (task.description or "")[:200]},
                         after={"description": (new_desc or "")[:200]},
                         user_id=uid)
            task.description = new_desc
            changed = True

    if priority is not None:
        try:
            p_enum = TaskPriority[priority]
        except KeyError:
            raise TaskError("أولوية غير صالحة")
        if p_enum != task.priority:
            log_activity(task, "PRIORITY_CHANGED",
                         before={"priority": task.priority.value},
                         after={"priority": p_enum.value}, user_id=uid)
            task.priority = p_enum
            changed = True

    if deadline is not None:
        if deadline == "":
            new_dl = None
        else:
            try:
                new_dl = datetime.strptime(deadline, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                raise TaskError("صيغة التاريخ غير صحيحة (YYYY-MM-DD)")
        if new_dl != task.deadline:
            log_activity(task, "DEADLINE_CHANGED",
                         before={"deadline": str(task.deadline) if task.deadline else None},
                         after={"deadline": str(new_dl) if new_dl else None},
                         user_id=uid)
            task.deadline = new_dl
            changed = True

    if status is not None:
        try:
            s_enum = TaskStatus[status]
        except KeyError:
            raise TaskError("حالة غير صالحة")
        if s_enum != task.status:
            log_activity(task, "STATUS_CHANGED",
                         before={"status": task.status.value},
                         after={"status": s_enum.value}, user_id=uid)
            old = task.status
            task.status = s_enum
            if s_enum == TaskStatus.DONE:
                task.completed_at = datetime.utcnow()
            else:
                task.completed_at = None
            # Notify every watcher (assignees + creator) except the
            # actor — the creator now hears about status flips too.
            for rid in watchers_for(task, exclude={uid}):
                _notify(rid, company_id=task.company_id,
                        kind=NotificationKind.TASK_STATUS_CHANGED,
                        title=f"📊 تحديث حالة: {task.title}",
                        body=f"{old.label_ar} → {s_enum.label_ar}",
                        link_url=f"/tasks/{task.id}",
                        task=task)
            changed = True

    if changed:
        # MARSOUD-TASK-NOTIFY-CREATOR — for non-status inline edits
        # (title/description/priority/deadline) the status branch
        # above didn't fire, so watchers wouldn't hear. Send a
        # single TASK_UPDATED note in that case, excluding the
        # actor themselves (watchers_for handles the exclude —
        # so we don't double-guard on "actor is creator" here;
        # doing so would silently drop the assignee's ping when
        # the creator edits the task).
        # Skip only when a status flip already covered the fan-out.
        if status is None:
            for rid in watchers_for(task, exclude={uid}):
                _notify(rid, company_id=task.company_id,
                        kind=NotificationKind.TASK_UPDATED,
                        title=f"✏️ تحديث على مهمة: {task.title}",
                        body=None,
                        link_url=f"/tasks/{task.id}",
                        task=task)
        db.session.commit()
    return changed


# ─── Visibility ───────────────────────────────────────────────────────────
def visible_tasks_query(company_id, user_id, full_visibility,
                        pm_project_ids=None):
    """Tasks the given user is allowed to see.

    full_visibility: True for owner/admin — sees every task.
    pm_project_ids: iterable of project ids the user manages (PM view).
                    Their assigned tasks (legacy primary or multi-assignee
                    member) are also visible.
    Everyone else sees only tasks where they're either the legacy primary
    assignee OR a member of task_assignees.
    """
    q = Task.query.filter(Task.company_id == company_id)
    if full_visibility:
        return q

    user_task_ids = select(task_assignees.c.task_id).where(
        task_assignees.c.user_id == user_id
    )

    # MARSOUD-TASK-CREATOR-VIEW (Abdelhamid 2026-07-11) — the person
    # who CREATED the task can always open + follow it, even when
    # they're not one of the assignees. Rofida's ticket: "we need
    # whoever created the task to be able to open it normally even
    # if they're not assigned to it."
    visibility_clauses = [
        Task.assigned_to_id == user_id,
        Task.id.in_(user_task_ids),
        Task.created_by_id == user_id,
    ]
    if pm_project_ids is not None:
        pm_pids = list(pm_project_ids)
        if pm_pids:
            visibility_clauses.append(Task.project_id.in_(pm_pids))
    return q.filter(or_(*visibility_clauses))


def is_visible_to(task, user_id, full_visibility, pm_project_ids=None):
    if full_visibility:
        return True
    if task.assigned_to_id == user_id:
        return True
    if user_id in assignee_ids_for(task):
        return True
    # MARSOUD-TASK-CREATOR-VIEW — creator keeps read access.
    if task.created_by_id == user_id:
        return True
    if pm_project_ids and task.project_id in set(pm_project_ids):
        return True
    return False


# ─── Stats ────────────────────────────────────────────────────────────────
def _close_timestamps(company_id):
    """Map task_id → first datetime the task left the assignee's hands.
    Walks TaskActivityLog rows with action='STATUS_CHANGED' and returns
    the earliest transition into REVIEW or DONE per task.

    MARSOUD-TASK-REVIEW-NOT-INCOMPLETE (2026-08-06) — was DONE-only.
    Now: velocity + on-time + time-to-close all measure "when did the
    assignee finish their part", which is REVIEW or DONE (whichever
    came first). A slow reviewer no longer penalizes the assignee's
    velocity chart, and "on-time" is judged against the handoff, not
    the approval.

    Tasks whose close event predates the activity log feature fall
    back to None and are excluded from the median.
    """
    import json
    from app.models import TaskActivityLog
    rows = db.session.query(TaskActivityLog).filter(
        TaskActivityLog.company_id == company_id,
        TaskActivityLog.action == "STATUS_CHANGED",
    ).order_by(TaskActivityLog.created_at.asc()).all()
    _CLOSED_STATUSES = {"DONE", "REVIEW"}
    out = {}
    for r in rows:
        if r.task_id in out:
            continue   # only keep the first assignee-closing transition
        try:
            after = json.loads(r.after_json or "{}")
        except (TypeError, ValueError):
            continue
        if (after.get("status") or "").upper() in _CLOSED_STATUSES:
            out[r.task_id] = r.created_at
    return out


def _median(values):
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def team_stats(company_id, since=None):
    """MARSOUD — analytics-grade per-user stats.

    Each row reports counts (todo/in_progress/review/blocked/done/overdue),
    a completion rate, plus deeper performance metrics:

      avg_time_to_close   median days from task.created_at → first DONE
                          activity log entry (None when no task has been
                          closed)
      velocity_30d        count of tasks the user closed in the last 30
                          days (regardless of `since`, so the velocity
                          column is always comparable)
      overdue_ratio       overdue / total × 100 (0 when total = 0)
      on_time_rate        of done tasks WITH a deadline, % closed on or
                          before deadline (None when nothing qualifies)
      badges              auto-assigned highlights: 'star', 'behind', 'new'

    Args:
      since: optional date — restrict the count buckets (todo / in_progress
             / done / etc.) to tasks created on or after this date. The
             velocity_30d metric ignores `since` so the column always
             shows "last 30 days".

    Returns:
      {
        "rows":            sorted-by-total list of per-user dicts,
        "closed_per_week": list of 8 ints (last 8 weeks, oldest first),
        "status_dist":     {todo,in_progress,review,done,blocked} counts
                           across the WHOLE company (no `since`),
      }
    """
    today = date.today()
    now = datetime.utcnow()
    cutoff_30d = now - timedelta(days=30)
    members = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == company_id)
    ).fetchall()
    member_ids = [m.user_id for m in members]
    users = {u.id: u for u in
             db.session.query(User).filter(User.id.in_(member_ids)).all()}
    # Track when each user first joined this company so the 'new' badge
    # can fire on members < 30 days old.
    join_dates = {}
    for m in members:
        # user_companies.joined_at is the canonical column; some older rows
        # may have None and fall back to user.created_at.
        joined = getattr(m, "joined_at", None)
        if joined is None:
            u = users.get(m.user_id)
            joined = getattr(u, "created_at", None) if u else None
        if joined:
            join_dates[m.user_id] = joined

    # Pull every task once + its assignee list, then aggregate in Python.
    task_query = Task.query.filter(Task.company_id == company_id)
    if since:
        task_query = task_query.filter(Task.created_at >= since)
    tasks = task_query.all()

    closes = _close_timestamps(company_id)

    out = {uid: {
        "user": users.get(uid),
        "total": 0,
        "todo": 0, "in_progress": 0, "review": 0, "blocked": 0,
        "open": 0, "done": 0, "overdue": 0, "deadline_soon": 0,
        "_close_days": [],      # gathered then medianed below
        "velocity_30d": 0,
        "_on_time_eligible": 0,
        "_on_time_count": 0,
    } for uid in member_ids if uid in users}

    # MARSOUD-TASK-REVIEW-NOT-INCOMPLETE (2026-08-06) — `done` folds
    # REVIEW into DONE (assignee shipped it, from their POV they're
    # done), but the per-status split still exposes REVIEW as its own
    # count so the UI can distinguish "assignee closed" from "creator
    # approved". _on_time / _close_days measure from the FIRST close
    # transition per task (via _close_timestamps above, now
    # REVIEW-or-DONE), so a slow reviewer doesn't drag down the
    # assignee's on-time rate. The `review` counter also lives inside
    # the closed branch — a REVIEW task lands in BOTH `done` and
    # `review`, giving managers the aggregate + the drill-down chip.
    for t in tasks:
        ids = assignee_ids_for(t)
        closed_at = closes.get(t.id)
        for uid in ids:
            if uid not in out:
                continue
            row = out[uid]
            row["total"] += 1
            if t.is_closed_for_assignee:
                row["done"] += 1
                if t.status == TaskStatus.REVIEW:
                    row["review"] += 1
                if closed_at and t.created_at:
                    delta_days = (closed_at - t.created_at).total_seconds() / 86400
                    if delta_days >= 0:
                        row["_close_days"].append(delta_days)
                if t.deadline:
                    row["_on_time_eligible"] += 1
                    # Closed on or before the deadline?
                    closed_date = closed_at.date() if closed_at else today
                    if closed_date <= t.deadline:
                        row["_on_time_count"] += 1
            else:
                row["open"] += 1
                if t.status == TaskStatus.TODO:
                    row["todo"] += 1
                elif t.status == TaskStatus.IN_PROGRESS:
                    row["in_progress"] += 1
                elif t.status == TaskStatus.BLOCKED:
                    row["blocked"] += 1
            if t.is_overdue:
                row["overdue"] += 1
            elif t.deadline_soon:
                row["deadline_soon"] += 1

    # Velocity: always last-30-days, regardless of `since`. Walk the close
    # map directly (tasks already filtered by company_id at the source).
    velocity_tasks = [tid for tid, ts in closes.items() if ts >= cutoff_30d]
    if velocity_tasks:
        # For each velocity-eligible task, bump every assignee's velocity.
        from app.models import task_assignees
        rows = db.session.execute(
            task_assignees.select().where(
                task_assignees.c.task_id.in_(velocity_tasks)
            )
        ).fetchall()
        for r in rows:
            if r.user_id in out:
                out[r.user_id]["velocity_30d"] += 1

    # Finalise derived metrics + auto-badges.
    finalised = []
    for uid, row in out.items():
        row["avg_time_to_close"] = (
            round(_median(row["_close_days"]), 1)
            if row["_close_days"] else None
        )
        row["overdue_ratio"] = (
            round(row["overdue"] / row["total"] * 100, 1)
            if row["total"] else 0.0
        )
        row["on_time_rate"] = (
            round(row["_on_time_count"] / row["_on_time_eligible"] * 100, 1)
            if row["_on_time_eligible"] else None
        )
        row["completion_rate"] = (
            round(row["done"] / row["total"] * 100, 1)
            if row["total"] else 0.0
        )
        # Drop internal accumulators
        row.pop("_close_days", None)
        row.pop("_on_time_eligible", None)
        row.pop("_on_time_count", None)
        # Badges
        badges = []
        joined = join_dates.get(uid)
        if joined and (now - joined).days < 30:
            badges.append("new")
        if row["overdue"] > row["done"] and row["total"] > 0:
            badges.append("behind")
        row["badges"] = badges
        finalised.append(row)

    # Star badge → top by composite score (completion_rate × velocity_30d).
    # Only fires for users with at least 1 closed task in the velocity window.
    velocity_users = [r for r in finalised if r["velocity_30d"] > 0]
    if velocity_users:
        top = max(velocity_users,
                   key=lambda r: r["completion_rate"] * (r["velocity_30d"] + 1))
        if top["completion_rate"] >= 50:   # avoid handing a star for 1/2 done
            top["badges"].insert(0, "star")

    # Company-wide status distribution (no `since` — current state).
    all_tasks = Task.query.filter(Task.company_id == company_id).all()
    status_dist = {"todo": 0, "in_progress": 0, "review": 0,
                    "done": 0, "blocked": 0}
    for t in all_tasks:
        key = t.status.value.lower()
        if key in status_dist:
            status_dist[key] += 1

    # 8-week closed-per-week histogram (anchored on the current week).
    closed_per_week = [0] * 8
    week_anchor = now - timedelta(days=now.weekday())  # Monday of this week
    week_anchor = week_anchor.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = week_anchor - timedelta(weeks=7)
    for ts in closes.values():
        if ts < week_start:
            continue
        weeks_ago = int((now - ts).total_seconds() // (7 * 86400))
        idx = 7 - weeks_ago
        if 0 <= idx < 8:
            closed_per_week[idx] += 1

    rows = sorted(finalised, key=lambda r: -r["total"])
    return {
        "rows": rows,
        "closed_per_week": closed_per_week,
        "status_dist": status_dist,
    }


# ─── Notification utilities (bell dropdown) ───────────────────────────────
def recent_notifications(user_id, company_id, limit=10):
    return Notification.query.filter_by(
        user_id=user_id, company_id=company_id
    ).order_by(Notification.created_at.desc()).limit(limit).all()


def unread_count(user_id, company_id):
    return db.session.query(func.count(Notification.id)).filter(
        Notification.user_id == user_id,
        Notification.company_id == company_id,
        Notification.read_at.is_(None),
    ).scalar() or 0


def mark_all_read(user_id, company_id):
    Notification.query.filter_by(
        user_id=user_id, company_id=company_id
    ).filter(Notification.read_at.is_(None)).update(
        {"read_at": datetime.utcnow()}, synchronize_session=False
    )
    db.session.commit()


def mark_read(notification_id, user_id):
    n = db.session.get(Notification, notification_id)
    if not n or n.user_id != user_id:
        return None
    if not n.read_at:
        n.read_at = datetime.utcnow()
        db.session.commit()
    return n


# ─── MARSOUD-67: full task delete with cascade cleanup ────────────────
def delete_task_fully(task):
    """Atomically delete a Task and every dependent row + file on disk.

    The documents table stores attachments via (source_type='TASK',
    source_id=task.id) WITHOUT an FK, so deleting the task without
    explicit cleanup leaves orphan rows + leaks the files on disk + can
    let a new task reuse the id and inherit the orphan attachments
    (SQLite id reuse).

    Order matters — child rows before the parent:
      0. subtasks orphaned (parent_task_id → NULL)
      1. documents (table) + files on disk (best-effort)
      2. task_comments
      3. task_activity_logs
      4. task_assignees (m2m, no cascade configured)
      5. tasks (the row itself)
    """
    import os
    from flask import current_app
    from app.models import (
        Document, DocumentSourceType, Task,
        TaskComment, TaskActivityLog, task_assignees,
    )

    company_id = task.company_id
    task_id = task.id

    # 0. MARSOUD-PARENT-CHILD-TASK-HIERARCHY (2026-08-09) —
    # orphan subtasks BEFORE the parent row goes. The FK is
    # declared ondelete="SET NULL" (see migration
    # c9d1e4f7a2b8_task_parent_id.py) which handles Postgres
    # correctly. SQLite doesn't enforce that unless
    # PRAGMA foreign_keys=ON — same reason attachments +
    # comments + activity logs are wiped explicitly above the
    # parent delete. Ticket rule: "Deleting a parent does NOT
    # delete any subtask — subtasks retain all their data and
    # history and become standalone root tasks."
    Task.query.filter_by(parent_task_id=task_id).update(
        {"parent_task_id": None}, synchronize_session=False)

    # 1. attachments — delete files first, then the rows.
    docs = Document.query.filter_by(
        source_type=DocumentSourceType.TASK,
        source_id=task_id,
        company_id=company_id,
    ).all()
    for doc in docs:
        if doc.file_path:
            # file_path stored as `/static/...` — resolve to disk path.
            rel = doc.file_path.lstrip("/")
            disk = os.path.join(current_app.root_path, rel)
            if os.path.exists(disk):
                try:
                    os.remove(disk)
                except OSError:
                    current_app.logger.warning(
                        "delete_task_fully: failed to remove %s", disk,
                    )
        db.session.delete(doc)

    # 2. comments
    TaskComment.query.filter_by(task_id=task_id).delete()

    # 3. activity log
    TaskActivityLog.query.filter_by(task_id=task_id).delete()

    # 4. assignees (m2m secondary table — no cascade configured at the ORM)
    db.session.execute(
        task_assignees.delete().where(task_assignees.c.task_id == task_id)
    )

    # 5. the task itself
    db.session.delete(task)
    db.session.commit()
