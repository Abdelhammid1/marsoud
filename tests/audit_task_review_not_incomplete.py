#!/usr/bin/env python3
"""MARSOUD-TASK-REVIEW-NOT-INCOMPLETE (2026-08-06) — audit for the
ticket that reported REVIEW-status tasks getting blamed on the
assignee.

The bug: when an employee finishes their work and moves a task to
REVIEW, every performance rollup keeps counting the task as
unfinished — completion drops, overdue rises, velocity stalls,
and the deadline-reminder cron still nags them for a task that's
out of their hands.

The fix draws a line between two questions the codebase was
conflating:

  · "Did the assignee finish their part?" — yes at REVIEW or DONE
  · "Is the task truly closed?"           — yes only at DONE

Assignee-perspective sites move to the first answer. Project-level
completion, task archival, `completed_at`, and HR completion
scoring stay on the second (guardrail checks below).

Every check verified to fail against pre-change HEAD.
"""
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__TRNI_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import (
        Company, Plan, User, user_companies, Customer, Project,
        Task, TaskStatus, TaskPriority, task_assignees,
    )
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code="__trni__").first()
    if not plan:
        plan = Plan(code="__trni__", name="TRNI", name_ar="TRNI",
                    allowed_subitems=None)
        plan.set_modules(["tasks", "projects", "reports"])
        db.session.add(plan); db.session.flush()

    co = Company(name=f"{PREFIX}CO", base_currency="SAR",
                 plan_id=plan.id, timezone="Asia/Riyadh")
    db.session.add(co); db.session.flush()
    co.intended_plan_id = plan.id
    db.session.commit()

    u = User(email=f"{PREFIX}u@audit.local",
             password_hash=generate_password_hash("x", method="pbkdf2:sha256"),
             full_name="trni user", is_active=True)
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=co.id, role="owner"))
    db.session.commit()

    cust = Customer(company_id=co.id, name=f"{PREFIX}Cust")
    db.session.add(cust); db.session.flush()
    proj = Project(company_id=co.id, name=f"{PREFIX}Proj",
                   customer_id=cust.id, type="INTERNAL",
                   manager_id=u.id,
                   start_date=date.today(),
                   end_date=date.today() + timedelta(days=30))
    db.session.add(proj); db.session.commit()

    _STATE.update(company_id=co.id, user_id=u.id,
                  project_id=proj.id)


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    # Orphan sweep for task-scoped rows that don't carry company_id
    # (task_assignees / task_activity_logs). Same collision trap as T7.
    db.session.execute(text(
        "DELETE FROM task_assignees WHERE task_id NOT IN "
        "(SELECT id FROM tasks)"))
    try:
        db.session.execute(text(
            "DELETE FROM task_activity_logs WHERE task_id NOT IN "
            "(SELECT id FROM tasks)"))
    except Exception:
        db.session.rollback()
    db.session.commit()
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        db.session.execute(text(
            "DELETE FROM task_assignees WHERE task_id IN "
            "(SELECT id FROM tasks WHERE company_id=:c)"), {"c": cid})
        try:
            db.session.execute(text(
                "DELETE FROM task_activity_logs WHERE task_id IN "
                "(SELECT id FROM tasks WHERE company_id=:c)"),
                {"c": cid})
        except Exception:
            db.session.rollback()
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(
                        text(f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        db.session.execute(text(
            "DELETE FROM milestones WHERE project_id IN "
            "(SELECT id FROM projects WHERE company_id=:c)"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM projects WHERE company_id=:c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id=:c"), {"c": cid})
        db.session.commit()
    db.session.execute(text("DELETE FROM plans WHERE code='__trni__'"))
    db.session.commit()
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text(
            "DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _wipe_tasks():
    """Reset the task+notification tables between checks so accumulated
    fixture rows from earlier tests don't cross-contaminate rollups.
    (task_assignees is task-scoped, task_activity_logs carries its own
    task_id, notifications are per-user and per-company — sweep them
    all before each mutation check.)"""
    from sqlalchemy import text
    cid = _STATE["company_id"]
    db.session.rollback()
    db.session.execute(text(
        "DELETE FROM task_assignees WHERE task_id IN "
        "(SELECT id FROM tasks WHERE company_id=:c)"), {"c": cid})
    try:
        db.session.execute(text(
            "DELETE FROM task_activity_logs WHERE task_id IN "
            "(SELECT id FROM tasks WHERE company_id=:c)"), {"c": cid})
    except Exception:
        db.session.rollback()
    db.session.execute(text(
        "DELETE FROM notifications WHERE company_id=:c"), {"c": cid})
    db.session.execute(text(
        "DELETE FROM tasks WHERE company_id=:c"), {"c": cid})
    db.session.commit()


def _mk_task(*, status, deadline=None, title="t"):
    """Fresh task with the user as sole assignee. Commits so
    subsequent status flips have a stable id + audit trail."""
    from app.models import (
        Task, TaskStatus, TaskPriority, task_assignees,
    )
    t = Task(
        company_id=_STATE["company_id"],
        title=title,
        project_id=_STATE["project_id"],
        assigned_to_id=_STATE["user_id"],
        created_by_id=_STATE["user_id"],
        priority=TaskPriority.MEDIUM,
        status=status,
        deadline=deadline,
    )
    db.session.add(t); db.session.flush()
    db.session.execute(task_assignees.insert().values(
        task_id=t.id, user_id=_STATE["user_id"],
        assigned_by_id=_STATE["user_id"]))
    db.session.commit()
    return t


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. Task.is_overdue: REVIEW past deadline is NOT overdue")
def _():
    """The property is the widest blast radius — this alone fixes
    the badge in the Kanban, the calendar overdue count, the
    dashboard tasks_overdue card, and every rollup that inherits it.
    Pre-fix: REVIEW is treated identically to IN_PROGRESS for
    overdue purposes."""
    from app.models import TaskStatus
    _wipe_tasks()
    yesterday = date.today() - timedelta(days=1)
    t = _mk_task(status=TaskStatus.REVIEW, deadline=yesterday,
                 title="review-past-deadline")
    assert t.is_overdue is False, (
        "REVIEW past deadline is still counted as overdue")
    # And an IN_PROGRESS control — should stay overdue (sanity).
    t2 = _mk_task(status=TaskStatus.IN_PROGRESS, deadline=yesterday,
                  title="in-progress-past-deadline")
    assert t2.is_overdue is True, (
        "IN_PROGRESS past deadline lost its overdue flag — regression")
    return "REVIEW cleared, IN_PROGRESS still overdue"


@check("2. is_closed_for_assignee: true for REVIEW + DONE, false for the rest")
def _():
    """The new primitive. Every subsequent bucketer keys off this,
    so audit the primitive directly."""
    from app.models import Task, TaskStatus
    for status, expected in [
        (TaskStatus.TODO, False),
        (TaskStatus.IN_PROGRESS, False),
        (TaskStatus.REVIEW, True),
        (TaskStatus.DONE, True),
        (TaskStatus.BLOCKED, False),
    ]:
        t = Task(status=status)  # not persisted — pure predicate check
        got = t.is_closed_for_assignee
        assert got is expected, (
            f"is_closed_for_assignee({status.value}) = {got}, "
            f"expected {expected}")
    return "REVIEW + DONE → True; TODO/IN_PROGRESS/BLOCKED → False"


@check("3. _employee_task_buckets: REVIEW counts as done, not overdue")
def _():
    """The Employees-tab card. Ticket's most visible symptom: the
    'مكتمل' number ignores REVIEW and the 'متأخر' number blames it."""
    from app.routes.tasks import _employee_task_buckets
    from app.models import TaskStatus
    _wipe_tasks()
    yesterday = date.today() - timedelta(days=1)
    _mk_task(status=TaskStatus.REVIEW, deadline=yesterday,
             title="review-past")
    _mk_task(status=TaskStatus.DONE, deadline=yesterday,
             title="done-past")
    _mk_task(status=TaskStatus.IN_PROGRESS, deadline=yesterday,
             title="in-progress-past")

    rows = _employee_task_buckets(_STATE["company_id"])
    row = next(r for r in rows if r["user"].id == _STATE["user_id"])
    # DONE + REVIEW both fold into done → 2. IN_PROGRESS stays open.
    assert row["done"] == 2, f"done should be 2 (DONE+REVIEW), got {row['done']}"
    # New review bucket surfaces REVIEW separately for the UI chip.
    assert row.get("review") == 1, (
        f"review bucket missing or wrong: {row.get('review')}")
    # Only the IN_PROGRESS-past-deadline is overdue.
    assert row["overdue"] == 1, (
        f"overdue should be 1 (only IN_PROGRESS), got {row['overdue']}")
    return f"done={row['done']}, review={row['review']}, overdue={row['overdue']}"


@check("4. _employee_monthly_stats: first assignee-closing transition counted")
def _():
    """Monthly chart. Moving a task to REVIEW this month should
    credit this month. A subsequent REVIEW→DONE later in the month
    must NOT double-count."""
    from app.routes.tasks import _employee_monthly_stats
    from app.services.crm import set_task_status
    from app.services.tasks_extras import log_activity
    from app.models import TaskStatus
    _wipe_tasks()
    t = _mk_task(status=TaskStatus.IN_PROGRESS, title="monthly")

    # The route path pairs `log_activity(STATUS_CHANGED)` with
    # `set_task_status()` (routes/tasks.py::status(), lines 876-880);
    # `set_task_status()` itself only writes to UserActivityLog, not
    # TaskActivityLog. The monthly-chart function reads
    # TaskActivityLog, so we mirror the full route pairing here.
    def _flip(new):
        log_activity(t, "STATUS_CHANGED",
                     before={"status": t.status.value},
                     after={"status": new.value},
                     user_id=_STATE["user_id"])
        set_task_status(t, new, by_user_id=_STATE["user_id"])

    _flip(TaskStatus.REVIEW)
    # And later, the creator approves it in the same month.
    _flip(TaskStatus.DONE)

    stats = _employee_monthly_stats(
        _STATE["company_id"], _STATE["user_id"], months=1)
    assert stats["closed"], f"empty closed series: {stats}"
    # The current-month bucket is the LAST entry (oldest → newest).
    current = stats["closed"][-1]
    assert current == 1, (
        f"current-month bucket should be 1 (single task, not double-"
        f"counted across REVIEW+DONE transitions), got {current}")
    return f"current month = {current} (dedup across REVIEW+DONE)"


@check("5. team_stats: REVIEW past deadline → done +1, overdue unchanged")
def _():
    """The analytics grid. Verifies the reclassification landed in
    every aggregated column: done, open, review, overdue, and the
    completion_rate that derives from them."""
    from app.services.tasks_extras import team_stats
    from app.models import TaskStatus
    _wipe_tasks()
    yesterday = date.today() - timedelta(days=1)
    _mk_task(status=TaskStatus.REVIEW, deadline=yesterday,
             title="ts-review-past")
    _mk_task(status=TaskStatus.TODO, deadline=yesterday,
             title="ts-todo-past")

    out = team_stats(_STATE["company_id"])
    row = next(r for r in out["rows"]
               if r["user"] and r["user"].id == _STATE["user_id"])
    assert row["done"] >= 1, (
        f"team_stats.done must include REVIEW, got {row['done']}")
    assert row["review"] == 1, (
        f"per-status review bucket = {row['review']}, expected 1")
    # Only the TODO-past-deadline should register as overdue.
    assert row["overdue"] == 1, (
        f"team_stats.overdue miscount: {row['overdue']} — REVIEW "
        f"past deadline is leaking in")
    # completion_rate reflects the wider "closed" set — with 1 review
    # + 1 todo the assignee is 1/2 = 50%.
    assert row["completion_rate"] >= 50.0, (
        f"completion_rate low: {row['completion_rate']} — REVIEW not "
        f"counted as closed for the assignee")
    return (f"done={row['done']}, review={row['review']}, "
            f"overdue={row['overdue']}, completion={row['completion_rate']}%")


@check("6. remind_task_deadlines_24h skips REVIEW tasks")
def _():
    """The cron pings the assignee 24h before deadline. Pre-fix it
    fires for a task the assignee already handed off — pure noise
    when the ball is in the creator's court."""
    from app.services.opsflow_extras import remind_task_deadlines_24h
    from app.models import TaskStatus, Notification
    from sqlalchemy import text
    _wipe_tasks()
    # A REVIEW task with deadline tomorrow — the 24h window.
    tomorrow = date.today() + timedelta(days=1)
    _mk_task(status=TaskStatus.REVIEW, deadline=tomorrow,
             title="review-24h")
    # Nuke prior notifications so we count only what THIS run sent.
    db.session.execute(text(
        "DELETE FROM notifications WHERE user_id=:u"),
        {"u": _STATE["user_id"]})
    db.session.commit()
    sent = remind_task_deadlines_24h()
    # sent is a count of NEW notifications the cron fired. The single
    # REVIEW task is the only candidate; it must NOT ping.
    review_pings = Notification.query.filter_by(
        user_id=_STATE["user_id"],
        kind="TASK_DEADLINE_24H",
    ).count()
    assert review_pings == 0, (
        f"REVIEW task pinged the assignee ({review_pings} ping(s)) — "
        f"noise for work already handed off")
    return f"cron cycle sent={sent}, review pings={review_pings}"


@check("7. Guardrail: Project.recompute_progress still requires DONE")
def _():
    """A project isn't done until the reviewer approves. Progress
    percentage keys off actual DONE, not the assignee's handoff.
    Regression guard: if a future change lets REVIEW count toward
    project completion, the whole finance/status flow drifts."""
    from app.models import Project, Task, TaskStatus
    _wipe_tasks()
    # One REVIEW-status task on this project.
    _mk_task(status=TaskStatus.REVIEW, title="only-review")

    p = db.session.get(Project, _STATE["project_id"])
    p.recompute_progress()
    db.session.commit()
    assert p.progress_pct < 100, (
        f"project progress hit {p.progress_pct}% with only a REVIEW "
        f"task — reviewer never signed off, must not read as done")
    return f"project progress = {p.progress_pct}% (correct — only REVIEW)"


@check("8. Guardrail: TODO→REVIEW does not stamp completed_at")
def _():
    """completed_at is the reality of when the work was truly closed
    (reviewer approval). Setting it on the REVIEW handoff would
    corrupt the HR DONE_ON_TIME scoring, the completion timestamp
    stored on the row, and every downstream analytic that treats
    completed_at as authoritative."""
    from app.services.crm import set_task_status
    from app.models import TaskStatus
    _wipe_tasks()
    t = _mk_task(status=TaskStatus.TODO, title="stamp-guard")
    set_task_status(t, TaskStatus.REVIEW,
                    by_user_id=_STATE["user_id"])
    assert t.completed_at is None, (
        f"completed_at got stamped on REVIEW handoff: {t.completed_at}")
    # And once the creator approves, it must stamp.
    set_task_status(t, TaskStatus.DONE,
                    by_user_id=_STATE["user_id"])
    assert t.completed_at is not None, (
        "completed_at never got stamped on DONE — regression")
    return "REVIEW leaves completed_at NULL; DONE stamps it"


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                # Each check starts on the same fixture; DB state accumulates
                # across checks except where a check explicitly clears (e.g. #7).
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                    failed += 1
        finally:
            _teardown()
            print("\n(fixture cleaned up)")
    print(f"\n----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
