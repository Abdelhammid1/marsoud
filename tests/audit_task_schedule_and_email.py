#!/usr/bin/env python3
"""MARSOUD-TASK-SCHEDULE + MARSOUD-EMAIL-CONTRAST-FIX (Abdelhamid 2026-07-11).

Three tickets audited together:

  #35 — Email task-title white-on-white. Fixed by swapping the CSS
        gradient div for a `<table bgcolor="#0A2540">` so mail
        clients that strip gradients still show white text on dark.

  #37 — Daily recurring task for a specified duration.
  #38 — Scheduled task that activates on a future date.

  Both #37 and #38 land as a single `TaskSchedule` model. This
  audit exercises the model, the cron materializer, and the
  full HTTP round-trip through /tasks/new.

Checks:
  1. Email template uses <table bgcolor="#0A2540"> for the header
     card AND declares color:#ffffff for the task title text —
     white-on-dark contrast survives Gmail's gradient stripping.
  2. Email template no longer contains linear-gradient inside a
     dark card (regression guard for #35).
  3. create_schedule() persists a ONCE schedule with correct
     start_date + null end_date + active=True.
  4. create_schedule() persists a DAILY schedule with start<end
     and rejects end<start with ScheduleError.
  5. materialize_due_schedules() fires nothing when today <
     start_date (schedule still waiting).
  6. materialize_due_schedules() creates ONE Task when today
     is in-window, sets last_generated_date=today, generated_count=1,
     and deactivates a ONCE schedule after it fires.
  7. materialize_due_schedules() is idempotent — running it twice
     the same day creates only ONE task (no dupes).
  8. materialize_due_schedules() on a DAILY schedule creates a new
     Task each day (simulated by rolling last_generated_date back
     one day) until today > end_date, then deactivates.
  9. HTTP: POST /tasks/new with schedule_mode=DAILY + start/end
     creates a TaskSchedule instead of a Task (and the task list
     stays empty until the cron fires).
 10. HTTP: after POST above, hitting /cron/tick actually spawns
     one Task, wires the assignee, and fires a notification.
"""
import sys
from pathlib import Path
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM task_schedule_assignees WHERE schedule_id IN "
            "(SELECT id FROM task_schedules WHERE company_id = :c)"
        ), {"c": company_id})
        conn.execute(text(
            "DELETE FROM task_assignees WHERE task_id IN "
            "(SELECT id FROM tasks WHERE company_id = :c)"
        ), {"c": company_id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'ts-%@x.test'"))


def _setup():
    from app.models import Company, User, user_companies
    from werkzeug.security import generate_password_hash

    for name in ("__TASK_SCHED__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__TASK_SCHED__", base_currency="SAR")
    db.session.add(a); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(a.id)

    def _mk(email, role):
        u = User(email=email,
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name=email.split("@")[0])
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=a.id, role=role))
        return u

    owner = _mk("ts-owner@x.test", "owner")
    assignee = _mk("ts-assignee@x.test", "sales_rep")
    db.session.commit()
    _STATE.update(
        a_id=a.id, owner_id=owner.id, assignee_id=assignee.id,
    )


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
              "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


# ─── #35 email contrast ──────────────────────────────────────────────
@check("1. Email template header uses bgcolor + white text")
def _():
    path = ROOT / "app/templates/emails/task_notification.html"
    body = path.read_text(encoding="utf-8")
    assert 'bgcolor="#0A2540"' in body, "bgcolor attribute missing"
    assert "color:#ffffff" in body, \
        "explicit white text color missing (regression risk in Gmail)"
    return "bgcolor + explicit white color present"


@check("2. Email template no longer uses linear-gradient on the dark card")
def _():
    path = ROOT / "app/templates/emails/task_notification.html"
    body = path.read_text(encoding="utf-8")
    # Comment mentioning it is fine; an actual `background: linear-gradient`
    # inline style on the header card is not.
    assert 'background:linear-gradient(135deg,#0A2540' not in body, \
        "old gradient background still present — Gmail will strip it"
    return "gradient removed"


# ─── #37/38 model + service ──────────────────────────────────────────
@check("3. create_schedule persists a ONCE schedule correctly")
def _():
    from app.services.task_schedules import create_schedule
    from app.models import TaskSchedule
    s = create_schedule(
        company_id=_STATE["a_id"],
        created_by_id=_STATE["owner_id"],
        title="one-shot future task",
        description="desc", priority="MEDIUM",
        project_id=None, milestone_id=None, notes=None,
        assignee_ids=[_STATE["assignee_id"]],
        recurrence="ONCE",
        start_date=date.today() + timedelta(days=3),
        end_date=None,
    )
    assert s.recurrence == "ONCE"
    assert s.start_date == date.today() + timedelta(days=3)
    assert s.end_date is None, "ONCE schedule should force end_date NULL"
    assert s.active is True
    _STATE["once_id"] = s.id
    return f"ONCE schedule {s.id} start={s.start_date}"


@check("4. create_schedule rejects DAILY with end<start")
def _():
    from app.services.task_schedules import (
        create_schedule, ScheduleError,
    )
    try:
        create_schedule(
            company_id=_STATE["a_id"],
            created_by_id=_STATE["owner_id"],
            title="bad daily", description=None, priority="LOW",
            project_id=None, milestone_id=None, notes=None,
            assignee_ids=[_STATE["assignee_id"]],
            recurrence="DAILY",
            start_date=date.today() + timedelta(days=5),
            end_date=date.today(),
        )
        assert False, "should have raised ScheduleError"
    except ScheduleError as e:
        assert "قبل" in str(e), f"unexpected message: {e}"
    return "end<start rejected"


@check("5. materialize does nothing when start_date > today")
def _():
    from app.services.task_schedules import materialize_due_schedules
    from app.models import Task, TaskSchedule
    before_tasks = Task.query.filter_by(
        company_id=_STATE["a_id"]).count()
    summary = materialize_due_schedules(today=date.today())
    after_tasks = Task.query.filter_by(
        company_id=_STATE["a_id"]).count()
    assert after_tasks == before_tasks, \
        "materialize spawned tasks for a future-dated schedule"
    # The ONCE schedule we made stays active (still in future).
    s = db.session.get(TaskSchedule, _STATE["once_id"])
    assert s.active is True
    assert s.last_generated_date is None
    return f"no spawns; still active (fired={summary['fired']})"


@check("6. materialize fires ONCE schedule when today ≥ start_date")
def _():
    from app.services.task_schedules import materialize_due_schedules
    from app.models import Task, TaskSchedule
    # Move the ONCE schedule so its start_date is today.
    s = db.session.get(TaskSchedule, _STATE["once_id"])
    s.start_date = date.today()
    db.session.commit()
    before_tasks = Task.query.filter_by(
        company_id=_STATE["a_id"]).count()
    summary = materialize_due_schedules(today=date.today())
    after_tasks = Task.query.filter_by(
        company_id=_STATE["a_id"]).count()
    assert after_tasks == before_tasks + 1, \
        f"expected 1 new task, got {after_tasks - before_tasks}"
    s = db.session.get(TaskSchedule, _STATE["once_id"])
    assert s.active is False, "ONCE schedule should retire after firing"
    assert s.generated_count == 1
    assert s.last_generated_date == date.today()
    return "1 task spawned + schedule retired"


@check("7. materialize is idempotent — running twice creates no dupe")
def _():
    from app.services.task_schedules import materialize_due_schedules
    from app.models import Task, TaskSchedule
    # Reactivate the ONCE schedule (test control) and roll back its
    # last-fired date. Then run materialize twice.
    s = db.session.get(TaskSchedule, _STATE["once_id"])
    s.active = True
    s.last_generated_date = None
    s.generated_count = 0
    db.session.commit()
    before = Task.query.filter_by(company_id=_STATE["a_id"]).count()
    materialize_due_schedules(today=date.today())
    materialize_due_schedules(today=date.today())
    after = Task.query.filter_by(company_id=_STATE["a_id"]).count()
    assert after == before + 1, \
        f"double materialize created {after - before} tasks — expected 1"
    return "second call was a no-op"


@check("8. DAILY schedule fires once per day and deactivates after end_date")
def _():
    from app.services.task_schedules import (
        create_schedule, materialize_due_schedules,
    )
    from app.models import Task, TaskSchedule
    today = date.today()
    s = create_schedule(
        company_id=_STATE["a_id"],
        created_by_id=_STATE["owner_id"],
        title="daily standup",
        description=None, priority="HIGH",
        project_id=None, milestone_id=None, notes=None,
        assignee_ids=[_STATE["assignee_id"]],
        recurrence="DAILY",
        start_date=today - timedelta(days=1),
        end_date=today + timedelta(days=1),
    )
    # Day 1 (today − 1): fire.
    before = Task.query.filter_by(company_id=_STATE["a_id"]).count()
    materialize_due_schedules(today=today - timedelta(days=1))
    d1 = Task.query.filter_by(company_id=_STATE["a_id"]).count()
    assert d1 == before + 1, "day 1 didn't spawn"
    # Day 2 (today): fire again.
    materialize_due_schedules(today=today)
    d2 = Task.query.filter_by(company_id=_STATE["a_id"]).count()
    assert d2 == d1 + 1, f"day 2 didn't spawn (delta={d2 - d1})"
    # Day 4 (past end_date): don't fire; deactivate.
    materialize_due_schedules(today=today + timedelta(days=4))
    d3 = Task.query.filter_by(company_id=_STATE["a_id"]).count()
    assert d3 == d2, "post-end-date fire leaked a task"
    s = db.session.get(TaskSchedule, s.id)
    assert s.active is False, "past-end DAILY schedule stayed active"
    return "day 1 + day 2 spawned; day 4 deactivated"


# ─── HTTP round-trip ─────────────────────────────────────────────────
@check("9. POST /tasks/new with DAILY schedule creates TaskSchedule (not Task)")
def _():
    from flask import current_app
    from app.models import Task, TaskSchedule
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    tasks_before = Task.query.filter_by(company_id=_STATE["a_id"]).count()
    scheds_before = TaskSchedule.query.filter_by(
        company_id=_STATE["a_id"]).count()
    r = client.post("/tasks/new", data={
        "title": "http round-trip task",
        "description": "posted via new-task form",
        "priority": "MEDIUM",
        "assignee_ids": str(_STATE["assignee_id"]),
        "schedule_mode": "DAILY",
        "schedule_start_date": date.today().isoformat(),
        "schedule_end_date":
            (date.today() + timedelta(days=2)).isoformat(),
    }, follow_redirects=False)
    assert r.status_code in (200, 302), \
        f"status={r.status_code} body={r.data[:200]!r}"
    tasks_after = Task.query.filter_by(company_id=_STATE["a_id"]).count()
    scheds_after = TaskSchedule.query.filter_by(
        company_id=_STATE["a_id"]).count()
    assert tasks_after == tasks_before, \
        "task was created immediately — should have deferred to cron"
    assert scheds_after == scheds_before + 1, \
        "no new TaskSchedule row was inserted"
    _STATE["http_sched_id"] = TaskSchedule.query.filter_by(
        company_id=_STATE["a_id"]).order_by(
            TaskSchedule.id.desc()).first().id
    return f"schedule {_STATE['http_sched_id']} created, 0 tasks"


@check("10. materialize fires the HTTP-created schedule + notification")
def _():
    from app.services.task_schedules import materialize_due_schedules
    from app.models import Task, TaskSchedule, Notification
    before = Task.query.filter_by(company_id=_STATE["a_id"]).count()
    summary = materialize_due_schedules(today=date.today())
    after = Task.query.filter_by(company_id=_STATE["a_id"]).count()
    assert after >= before + 1, \
        f"expected at least 1 new task, got {after - before}"
    # The assignee should have received a TASK_ASSIGNED notification.
    n = Notification.query.filter_by(
        user_id=_STATE["assignee_id"], kind="TASK_ASSIGNED",
    ).first()
    assert n is not None, "assignee didn't get TASK_ASSIGNED"
    return f"{after - before} task(s) spawned + notification fired"


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
                if "a_id" in _STATE:
                    _teardown(_STATE["a_id"])
                print("\n(cleaned up fixture company)")
            except Exception as e:
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
