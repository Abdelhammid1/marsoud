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


def _notify(user_id, *, company_id, kind, title, body=None, link_url=None):
    """Best-effort notification insert."""
    try:
        n = Notification(
            user_id=user_id, company_id=company_id,
            kind=kind.value if hasattr(kind, "value") else kind,
            title=title, body=body, link_url=link_url,
        )
        db.session.add(n)
    except Exception:
        current_app.logger.exception("notification insert failed")


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
                link_url=f"/tasks/{task.id}")

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


def activity_description(entry):
    """Render an activity entry as a short Arabic phrase."""
    a = entry.action
    user = entry.user.full_name if entry.user else "النظام"
    try:
        before = json.loads(entry.before_json) if entry.before_json else {}
        after = json.loads(entry.after_json) if entry.after_json else {}
    except (TypeError, ValueError):
        before, after = {}, {}

    if a == "CREATED":
        return f"{user} أنشأ هذه المهمة"
    if a == "STATUS_CHANGED":
        try:
            o = TaskStatus[before.get("status", "TODO")].label_ar
            n = TaskStatus[after.get("status", "TODO")].label_ar
        except KeyError:
            o, n = before.get("status"), after.get("status")
        return f"{user} غيّر الحالة من «{o}» إلى «{n}»"
    if a == "PRIORITY_CHANGED":
        try:
            o = TaskPriority[before.get("priority", "MEDIUM")].label_ar
            n = TaskPriority[after.get("priority", "MEDIUM")].label_ar
        except KeyError:
            o, n = before.get("priority"), after.get("priority")
        return f"{user} غيّر الأولوية من «{o}» إلى «{n}»"
    if a == "DEADLINE_CHANGED":
        return f"{user} حدّث الموعد النهائي إلى {after.get('deadline') or '—'}"
    if a == "TITLE_CHANGED":
        return f"{user} حدّث العنوان"
    if a == "DESCRIPTION_CHANGED":
        return f"{user} حدّث الوصف"
    if a == "ASSIGNEES_CHANGED":
        added_ids = set(after.get("ids", [])) - set(before.get("ids", []))
        removed_ids = set(before.get("ids", [])) - set(after.get("ids", []))
        bits = []
        if added_ids:
            bits.append("أضاف " + "، ".join(_safe_user_full_name(i) for i in added_ids))
        if removed_ids:
            bits.append("أزال " + "، ".join(_safe_user_full_name(i) for i in removed_ids))
        return f"{user} " + (" و".join(bits) if bits else "حدّث المكلَّفين")
    if a == "COMMENT_ADDED":
        return f"{user} علّق على المهمة"
    return f"{user}: {a}"


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

    # Notify everyone watching the task except the commenter.
    recipients = assignee_ids_for(task) - {uid}
    for rid in recipients:
        _notify(rid, company_id=task.company_id,
                kind=NotificationKind.TASK_COMMENT,
                title=f"💬 تعليق جديد على: {task.title}",
                body=text[:200],
                link_url=f"/tasks/{task.id}")

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
                task.completed_at = datetime.now()
            else:
                task.completed_at = None
            # Notify assignees (except actor) about status changes.
            for rid in assignee_ids_for(task) - {uid}:
                _notify(rid, company_id=task.company_id,
                        kind=NotificationKind.TASK_STATUS_CHANGED,
                        title=f"📊 تحديث حالة: {task.title}",
                        body=f"{old.label_ar} → {s_enum.label_ar}",
                        link_url=f"/tasks/{task.id}")
            changed = True

    if changed:
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

    visibility_clauses = [
        Task.assigned_to_id == user_id,
        Task.id.in_(user_task_ids),
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
    if pm_project_ids and task.project_id in set(pm_project_ids):
        return True
    return False


# ─── Stats ────────────────────────────────────────────────────────────────
def team_stats(company_id):
    """Per-user stats: total/open/done/overdue counts.

    Counts a user once per task: a multi-assignee task counts toward each
    assignee's total. Only company members are returned.
    """
    today = date.today()
    members = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == company_id)
    ).fetchall()
    member_ids = [m.user_id for m in members]
    users = {u.id: u for u in
             db.session.query(User).filter(User.id.in_(member_ids)).all()}

    # Pull every task once + its assignee list, then aggregate in Python.
    tasks = Task.query.filter(Task.company_id == company_id).all()
    # MARSOUD — separate buckets per status so the team-stats page can show
    # "للقيام" (TODO) and "قيد التنفيذ" (IN_PROGRESS) as distinct columns
    # instead of lumping them under a generic "open". `open` is kept as a
    # convenience aggregate (everything not DONE) for backwards compat.
    out = {uid: {"user": users.get(uid), "total": 0,
                 "todo": 0, "in_progress": 0, "review": 0, "blocked": 0,
                 "open": 0, "done": 0, "overdue": 0, "deadline_soon": 0}
           for uid in member_ids if uid in users}

    for t in tasks:
        ids = assignee_ids_for(t)
        for uid in ids:
            if uid not in out:
                continue
            row = out[uid]
            row["total"] += 1
            if t.status == TaskStatus.DONE:
                row["done"] += 1
            else:
                row["open"] += 1
                if t.status == TaskStatus.TODO:
                    row["todo"] += 1
                elif t.status == TaskStatus.IN_PROGRESS:
                    row["in_progress"] += 1
                elif t.status == TaskStatus.REVIEW:
                    row["review"] += 1
                elif t.status == TaskStatus.BLOCKED:
                    row["blocked"] += 1
            if t.is_overdue:
                row["overdue"] += 1
            elif t.deadline_soon:
                row["deadline_soon"] += 1

    # Sort by total desc so heaviest contributors land at the top.
    return sorted(out.values(), key=lambda r: -r["total"])


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
        {"read_at": datetime.now()}, synchronize_session=False
    )
    db.session.commit()


def mark_read(notification_id, user_id):
    n = db.session.get(Notification, notification_id)
    if not n or n.user_id != user_id:
        return None
    if not n.read_at:
        n.read_at = datetime.now()
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
      1. documents (table) + files on disk (best-effort)
      2. task_comments
      3. task_activity_logs
      4. task_assignees (m2m, no cascade configured)
      5. tasks (the row itself)
    """
    import os
    from flask import current_app
    from app.models import (
        Document, DocumentSourceType,
        TaskComment, TaskActivityLog, task_assignees,
    )

    company_id = task.company_id
    task_id = task.id

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
