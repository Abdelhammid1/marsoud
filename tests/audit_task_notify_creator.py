#!/usr/bin/env python3
"""MARSOUD-TASK-NOTIFY-CREATOR — audit for the ticket that asked for
in-app notifications to the task creator whenever an assignee updates
the task.

Setup: one company, three users — CREATOR, ASSIGNEE, BYSTANDER.
CREATOR opens a task assigned to ASSIGNEE.

Assertions:
  1. When ASSIGNEE moves the task's status via set_task_status,
     CREATOR gets a TASK_STATUS_CHANGED notification.
  2. When ASSIGNEE posts a comment via add_comment, CREATOR gets
     a TASK_COMMENT notification.
  3. When ASSIGNEE tweaks priority via apply_inline_edit (not the
     status branch), CREATOR gets a TASK_UPDATED notification.
  4. When CREATOR themselves moves the task's status, CREATOR does
     NOT get a notification (no self-pings).
  5. BYSTANDER (not assigned, not creator) gets nothing.
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__TASK_NOTIFY_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import (
        Company, User, user_companies, Task, TaskStatus, TaskPriority,
    )
    from werkzeug.security import generate_password_hash

    existing = Company.query.filter_by(name=COMPANY_NAME).first()
    if existing:
        _teardown(existing.id)
    c = Company(name=COMPANY_NAME, base_currency="SAR")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)

    def _mk(email):
        u = User(email=email,
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name=email.split("@")[0])
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=c.id, role="owner",
        ))
        return u

    creator = _mk("notif-creator@x.test")
    assignee = _mk("notif-assignee@x.test")
    bystander = _mk("notif-bystander@x.test")

    task = Task(
        company_id=c.id,
        title="اختبار الإشعارات",
        assigned_to_id=assignee.id,
        created_by_id=creator.id,
        priority=TaskPriority.MEDIUM,
        status=TaskStatus.TODO,
    )
    db.session.add(task); db.session.flush()
    # Add to task_assignees so assignee_ids_for picks them up.
    from app.models import task_assignees
    db.session.execute(task_assignees.insert().values(
        task_id=task.id, user_id=assignee.id,
        assigned_by_id=creator.id,
    ))
    db.session.commit()
    _STATE.update(company_id=c.id, task_id=task.id,
                    creator_id=creator.id, assignee_id=assignee.id,
                    bystander_id=bystander.id)


def _teardown(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM notifications WHERE company_id = :c"),
                     {"c": company_id})
        conn.execute(text("DELETE FROM task_assignees WHERE task_id IN "
                          "(SELECT id FROM tasks WHERE company_id = :c)"),
                     {"c": company_id})
        conn.execute(text("DELETE FROM task_comments WHERE company_id = :c"),
                     {"c": company_id})
        # Task activity log table is named `task_activity_logs` (plural)
        # in Alembic; older environments may not have it — swallow the
        # missing-table case to keep cleanup idempotent.
        try:
            conn.execute(
                text("DELETE FROM task_activity_logs WHERE company_id = :c"),
                {"c": company_id})
        except Exception:
            pass
        conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"),
                     {"c": company_id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                             {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text("DELETE FROM users WHERE email LIKE 'notif-%@x.test'"))


def _notifications_for(user_id, kind=None):
    """Fetch every notification the user has received in this company."""
    from app.models import Notification
    q = Notification.query.filter_by(
        user_id=user_id, company_id=_STATE["company_id"],
    )
    if kind:
        q = q.filter_by(kind=kind)
    return q.order_by(Notification.created_at.asc()).all()


def _clear_notifications():
    """Wipe any notification rows between checks so we assert only on
    what THIS check produced."""
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM notifications WHERE company_id = :c"),
                     {"c": _STATE["company_id"]})


@check("1. status flip by assignee → creator gets TASK_STATUS_CHANGED")
def _():
    from app.services.crm import set_task_status
    from app.models import Task, TaskStatus
    _clear_notifications()
    t = db.session.get(Task, _STATE["task_id"])
    set_task_status(t, TaskStatus.IN_PROGRESS,
                     by_user_id=_STATE["assignee_id"])
    creator_notes = _notifications_for(_STATE["creator_id"],
                                          kind="TASK_STATUS_CHANGED")
    assert len(creator_notes) == 1, \
        f"creator should get 1 status notification, got {len(creator_notes)}"
    assert "تغيرت حالة" in (creator_notes[0].title or "")
    return f"CREATOR received: {creator_notes[0].title!r}"


@check("2. comment by assignee → creator gets TASK_COMMENT")
def _():
    from app.services.tasks_extras import add_comment
    from app.models import Task
    _clear_notifications()
    t = db.session.get(Task, _STATE["task_id"])
    add_comment(t, "شغلت عليها كذا وكذا",
                 user_id=_STATE["assignee_id"])
    creator_notes = _notifications_for(_STATE["creator_id"],
                                          kind="TASK_COMMENT")
    assert len(creator_notes) == 1, \
        f"creator should get 1 comment notification, got {len(creator_notes)}"
    return f"CREATOR received: {creator_notes[0].title!r}"


@check("3. priority change by assignee → creator gets TASK_UPDATED")
def _():
    from app.services.tasks_extras import apply_inline_edit
    from app.models import Task
    _clear_notifications()
    t = db.session.get(Task, _STATE["task_id"])
    apply_inline_edit(t, priority="HIGH",
                       user_id=_STATE["assignee_id"])
    creator_notes = _notifications_for(_STATE["creator_id"],
                                          kind="TASK_UPDATED")
    assert len(creator_notes) == 1, \
        f"creator should get 1 update notification, got {len(creator_notes)}"
    return f"CREATOR received: {creator_notes[0].title!r}"


@check("4. status flip by CREATOR → CREATOR gets no self-ping")
def _():
    from app.services.crm import set_task_status
    from app.models import Task, TaskStatus
    _clear_notifications()
    t = db.session.get(Task, _STATE["task_id"])
    # Flip back to TODO by the creator themselves.
    set_task_status(t, TaskStatus.TODO,
                     by_user_id=_STATE["creator_id"])
    creator_notes = _notifications_for(_STATE["creator_id"])
    assert len(creator_notes) == 0, \
        f"creator should NOT self-notify, got {len(creator_notes)}"
    # Assignee should still receive it.
    assignee_notes = _notifications_for(_STATE["assignee_id"],
                                            kind="TASK_STATUS_CHANGED")
    assert len(assignee_notes) == 1, \
        f"assignee should receive status change, got {len(assignee_notes)}"
    return "no self-ping; assignee still notified"


@check("5. bystander (not assigned, not creator) receives nothing")
def _():
    from app.services.crm import set_task_status
    from app.services.tasks_extras import add_comment
    from app.models import Task, TaskStatus
    _clear_notifications()
    t = db.session.get(Task, _STATE["task_id"])
    set_task_status(t, TaskStatus.IN_PROGRESS,
                     by_user_id=_STATE["assignee_id"])
    add_comment(t, "تحديث تاني",
                 user_id=_STATE["assignee_id"])
    bystander_notes = _notifications_for(_STATE["bystander_id"])
    assert len(bystander_notes) == 0, \
        f"bystander shouldn't get notified, got {len(bystander_notes)}"
    return "bystander got 0 notifications"


# ─── Gap-closing checks ───────────────────────────────────────────────
@check("6. creator IS assignee — no duplicate notifications on self-edit")
def _():
    """Sanity check: if the creator is ALSO one of the assignees and
    they act on their own task, we shouldn't spam them. watchers_for()
    excludes the actor so this holds as a degenerate case."""
    from app.models import Task, TaskStatus, task_assignees
    from app.services.tasks_extras import apply_inline_edit
    _clear_notifications()
    # Add creator to the assignee set alongside the existing assignee.
    t = db.session.get(Task, _STATE["task_id"])
    db.session.execute(task_assignees.insert().values(
        task_id=t.id, user_id=_STATE["creator_id"],
        assigned_by_id=_STATE["creator_id"],
    ))
    db.session.commit()
    # Creator edits priority — should NOT self-ping.
    apply_inline_edit(t, priority="LOW",
                       user_id=_STATE["creator_id"])
    creator_notes = _notifications_for(_STATE["creator_id"])
    assert len(creator_notes) == 0, \
        f"creator-as-assignee still self-notified: {len(creator_notes)}"
    # Assignee should still get their update ping.
    assignee_notes = _notifications_for(_STATE["assignee_id"],
                                            kind="TASK_UPDATED")
    assert len(assignee_notes) == 1
    # Clean the extra assignee row for downstream checks.
    from sqlalchemy import text as _text
    with db.engine.begin() as conn:
        conn.execute(_text(
            "DELETE FROM task_assignees WHERE task_id = :t AND user_id = :u"
        ), {"t": t.id, "u": _STATE["creator_id"]})
    return "no self-ping even when creator is also an assignee"


@check("7. multi-field edit in one call fires exactly one TASK_UPDATED")
def _():
    """apply_inline_edit takes title/description/priority/deadline
    in one shot. The creator should get ONE consolidated
    TASK_UPDATED, not four (one per field)."""
    from app.models import Task
    from app.services.tasks_extras import apply_inline_edit
    _clear_notifications()
    t = db.session.get(Task, _STATE["task_id"])
    apply_inline_edit(
        t, title="عنوان جديد بعد التعديل",
        priority="MEDIUM", deadline="2026-08-31",
        user_id=_STATE["assignee_id"],
    )
    creator_notes = _notifications_for(_STATE["creator_id"],
                                          kind="TASK_UPDATED")
    assert len(creator_notes) == 1, \
        f"expected exactly 1 TASK_UPDATED, got {len(creator_notes)}"
    return "one consolidated notification per multi-field edit"


@check("8. status branch does NOT double-fire TASK_UPDATED")
def _():
    """When status is included among the changed fields, the status
    branch already sends TASK_STATUS_CHANGED. The tail block that
    emits TASK_UPDATED for other-field edits must NOT fire — otherwise
    the creator gets both."""
    from app.models import Task
    from app.services.tasks_extras import apply_inline_edit
    _clear_notifications()
    t = db.session.get(Task, _STATE["task_id"])
    apply_inline_edit(
        t, priority="HIGH", status="DONE",
        user_id=_STATE["assignee_id"],
    )
    status_notes = _notifications_for(
        _STATE["creator_id"], kind="TASK_STATUS_CHANGED")
    updated_notes = _notifications_for(
        _STATE["creator_id"], kind="TASK_UPDATED")
    assert len(status_notes) == 1, \
        f"expected 1 status note, got {len(status_notes)}"
    assert len(updated_notes) == 0, (
        f"status+priority combo emitted a redundant TASK_UPDATED "
        f"({len(updated_notes)})"
    )
    return "status flip + priority = 1 status note, 0 update notes"


@check("9. Notification row carries the right link_url")
def _():
    """The bell icon links back to the task. If link_url is wrong,
    the notification is useless."""
    from app.models import Task
    from app.services.tasks_extras import add_comment
    _clear_notifications()
    t = db.session.get(Task, _STATE["task_id"])
    add_comment(t, "تحقق من link_url",
                 user_id=_STATE["assignee_id"])
    creator_notes = _notifications_for(_STATE["creator_id"],
                                          kind="TASK_COMMENT")
    assert len(creator_notes) == 1
    expected = f"/tasks/{_STATE['task_id']}"
    assert creator_notes[0].link_url == expected, (
        f"link_url = {creator_notes[0].link_url!r}, expected {expected!r}"
    )
    return f"link_url points at {expected}"


@check("10. full /tasks/<id>/edit POST notifies watchers too")
def _():
    """apply_inline_edit is one code path; the "full edit" form
    (routes/tasks.py::edit) is a separate one that recomputes the
    task by hand. It emits its own TASK_UPDATED notification — this
    check exercises the whole POST round-trip so a future refactor
    can't silently drop the fan-out on this path."""
    from flask import current_app
    from app.models import Task
    from werkzeug.security import generate_password_hash
    _clear_notifications()
    # Give the assignee a real login so the route accepts them.
    from app.models import User
    assignee = db.session.get(User, _STATE["assignee_id"])
    assignee.password_hash = generate_password_hash(
        "x", method="pbkdf2:sha256")
    db.session.commit()

    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["assignee_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["company_id"]
    r = client.post(
        f"/tasks/{_STATE['task_id']}/edit",
        data={
            "title": "عنوان جديد من full edit",
            "description": "وصف مُحدَّث",
            "priority": "HIGH",
            "deadline": "",
            "assignee_ids": str(_STATE["assignee_id"]),
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 302), f"status={r.status_code}"
    creator_notes = _notifications_for(
        _STATE["creator_id"], kind="TASK_UPDATED")
    assert len(creator_notes) == 1, (
        f"creator should get 1 TASK_UPDATED from full-edit route, "
        f"got {len(creator_notes)}"
    )
    return f"full-edit POST fired 1 TASK_UPDATED to creator"


def _reset_g():
    """Clear g values Flask-Login caches on the app-context g. Without
    this the first check's identity bleeds into any later test_client
    call — same trick as audit_user_files."""
    from flask import g
    for key in ("_login_user", "active_company", "user_companies",
                 "impersonating"):
        try:
            g.pop(key, None)
        except Exception:
            pass


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}  ⇒ {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "company_id" in _STATE:
                    _teardown(_STATE["company_id"])
                    print("\n(cleaned up fixture company)")
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
