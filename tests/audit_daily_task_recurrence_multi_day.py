#!/usr/bin/env python3
"""MARSOUD-TASK-SCHEDULE-IMMEDIATE — multi-day recurrence verification.

Rofida's original ticket said "I made a daily task starting today until
day 31 and no task appeared today." The 2026-07-13 fix materialises the
first task immediately at save time. This audit walks the whole timeline
day-by-day to prove the daily cadence also fires on every subsequent day
via the cron path, notifications land each time, and the schedule
retires cleanly the moment `today > end_date`.

Checks:
  1. Immediate fire on save (day 0) — exactly 1 task exists.
  2. Cron tick simulated for day+1 → exactly 1 new task.
  3. Cron tick simulated for day+2 → exactly 1 new task.
  4. Cron tick simulated for day+3 → exactly 1 new task.
  5. Cron tick simulated for day+4 → exactly 1 new task.
  6. Cron tick simulated for day+5 (end_date) → exactly 1 new task.
  7. Cron tick simulated for day+6 (past end_date) → NO new task
     and the schedule flips active=False.
  8. Notifications: assignee receives one TASK_ASSIGNED per day
     (not just for day 0).
  9. Idempotence: calling materialize_due_schedules TWICE for the
     same simulated day creates only ONE task (dedupe holds).
 10. End-to-end via HTTP: POST /cron/tick actually runs the
     materializer without a 500, using the real cron code path.
"""
import sys
from pathlib import Path
from datetime import date, datetime, timedelta

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
            "DELETE FROM users WHERE email LIKE 'dtr-%@x.test'"))
        # Self-heal orphans from prior interrupted runs.
        conn.execute(text(
            "DELETE FROM task_schedule_assignees WHERE schedule_id NOT IN "
            "(SELECT id FROM task_schedules)"))


def _setup():
    from app.models import Company, User, user_companies
    from werkzeug.security import generate_password_hash

    for name in ("__DAILY_RECUR__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__DAILY_RECUR__", base_currency="SAR")
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

    owner = _mk("dtr-owner@x.test", "owner")
    assignee = _mk("dtr-assignee@x.test", "sales_rep")
    db.session.commit()

    # Create the schedule that will drive the whole timeline:
    #   start=today, end=today+5 (6-day window inclusive of both ends).
    from app.services.task_schedules import create_schedule
    today = date.today()
    schedule = create_schedule(
        company_id=a.id, created_by_id=owner.id,
        title="dtr-daily-standup",
        description="repro of Rofida's original ticket",
        priority="MEDIUM",
        project_id=None, milestone_id=None, notes=None,
        assignee_ids=[assignee.id],
        recurrence="DAILY",
        start_date=today, end_date=today + timedelta(days=5),
    )
    _STATE.update(
        a_id=a.id, owner_id=owner.id, assignee_id=assignee.id,
        schedule_id=schedule.id, today=today,
    )


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
              "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


def _task_count():
    from app.models import Task
    return Task.query.filter_by(
        company_id=_STATE["a_id"], title="dtr-daily-standup").count()


def _notification_count():
    from app.models import Notification
    return Notification.query.filter_by(
        user_id=_STATE["assignee_id"], kind="TASK_ASSIGNED",
    ).count()


# ─── Timeline ─────────────────────────────────────────────────────
@check("1. Day 0 (today): immediate fire on save spawned exactly 1 task")
def _():
    n = _task_count()
    assert n == 1, f"expected 1 task after create, got {n}"
    from app.models import TaskSchedule
    s = db.session.get(TaskSchedule, _STATE["schedule_id"])
    assert s.last_generated_date == _STATE["today"], \
        f"schedule bookkeeping wrong: last_generated_date={s.last_generated_date}"
    assert s.generated_count == 1
    assert s.active is True, "schedule shouldn't retire on day 0"
    _STATE["expected_notif"] = 1
    return f"1 task at day 0; schedule generated_count=1"


@check("2. Day +1: cron tick spawns exactly one more task")
def _():
    from app.services.task_schedules import materialize_due_schedules
    before = _task_count()
    summary = materialize_due_schedules(
        today=_STATE["today"] + timedelta(days=1))
    after = _task_count()
    assert after == before + 1, \
        f"day+1 delta = {after - before} (expected 1); summary={summary}"
    _STATE["expected_notif"] += 1
    return f"day+1: 1 new task (total {after})"


@check("3. Day +2: cron tick spawns exactly one more task")
def _():
    from app.services.task_schedules import materialize_due_schedules
    before = _task_count()
    materialize_due_schedules(
        today=_STATE["today"] + timedelta(days=2))
    after = _task_count()
    assert after == before + 1, \
        f"day+2 delta = {after - before} (expected 1)"
    _STATE["expected_notif"] += 1
    return f"day+2: 1 new task (total {after})"


@check("4. Day +3: cron tick spawns exactly one more task")
def _():
    from app.services.task_schedules import materialize_due_schedules
    before = _task_count()
    materialize_due_schedules(
        today=_STATE["today"] + timedelta(days=3))
    after = _task_count()
    assert after == before + 1
    _STATE["expected_notif"] += 1
    return f"day+3: 1 new task (total {after})"


@check("5. Day +4: cron tick spawns exactly one more task")
def _():
    from app.services.task_schedules import materialize_due_schedules
    before = _task_count()
    materialize_due_schedules(
        today=_STATE["today"] + timedelta(days=4))
    after = _task_count()
    assert after == before + 1
    _STATE["expected_notif"] += 1
    return f"day+4: 1 new task (total {after})"


@check("6. Day +5 (end_date, inclusive): still spawns one more task")
def _():
    from app.services.task_schedules import materialize_due_schedules
    before = _task_count()
    materialize_due_schedules(
        today=_STATE["today"] + timedelta(days=5))
    after = _task_count()
    assert after == before + 1, \
        f"day+5 (end_date) should fire; delta={after - before}"
    _STATE["expected_notif"] += 1
    return f"day+5: 1 new task (total {after})"


@check("7. Day +6 (past end_date): NO new task + schedule deactivates")
def _():
    from app.services.task_schedules import materialize_due_schedules
    from app.models import TaskSchedule
    before = _task_count()
    materialize_due_schedules(
        today=_STATE["today"] + timedelta(days=6))
    after = _task_count()
    assert after == before, \
        f"day+6 (past end) leaked a task: delta={after - before}"
    s = db.session.get(TaskSchedule, _STATE["schedule_id"])
    assert s.active is False, "schedule should retire past end_date"
    return f"day+6: 0 new tasks; schedule active=False"


# ─── Notifications ─────────────────────────────────────────────────
@check("8. Assignee received ONE notification per fire (6 total)")
def _():
    n = _notification_count()
    assert n == _STATE["expected_notif"], \
        f"expected {_STATE['expected_notif']} notifications, got {n}"
    return f"{n} TASK_ASSIGNED notifications received"


# ─── Idempotence ───────────────────────────────────────────────────
@check("9. Double cron tick for the same day is a no-op (dedupe)")
def _():
    from app.services.task_schedules import (
        create_schedule, materialize_due_schedules,
    )
    from app.models import Task, TaskSchedule
    # Fresh 1-day schedule owned by the same fixture.
    s = create_schedule(
        company_id=_STATE["a_id"], created_by_id=_STATE["owner_id"],
        title="dtr-dedupe-check",
        description=None, priority="LOW",
        project_id=None, milestone_id=None, notes=None,
        assignee_ids=[_STATE["assignee_id"]],
        recurrence="DAILY",
        start_date=_STATE["today"],
        end_date=_STATE["today"] + timedelta(days=2),
    )
    # Immediate fire already happened; count baseline.
    before = Task.query.filter_by(
        company_id=_STATE["a_id"], title="dtr-dedupe-check").count()
    assert before == 1, f"immediate fire baseline wrong: {before}"
    # Double-tick same day.
    materialize_due_schedules(today=_STATE["today"])
    materialize_due_schedules(today=_STATE["today"])
    after = Task.query.filter_by(
        company_id=_STATE["a_id"], title="dtr-dedupe-check").count()
    assert after == before, \
        f"double-tick duplicated (delta={after - before})"
    return "same-day double-tick is a no-op"


# ─── HTTP ──────────────────────────────────────────────────────────
@check("10. POST /cron/tick runs the real cron pipeline without error")
def _():
    """Hit the actual cron route to make sure the wiring holds
    (not just the service-layer function). We can't easily
    simulate 'tomorrow' via HTTP because the route reads
    date.today() internally — but a 200 response with a non-error
    task_schedules key proves the pipeline is intact."""
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    r = client.post("/cron/tick", follow_redirects=False)
    assert r.status_code == 200, f"cron tick got {r.status_code}"
    payload = r.get_json() or {}
    ts = payload.get("task_schedules", {})
    # A clean run returns a dict shape {fired, deactivated, errors}.
    # Old error payloads carry an "error" string key.
    assert isinstance(ts, dict), f"task_schedules not a dict: {ts!r}"
    assert "error" not in ts, f"cron reported error: {ts}"
    return f"cron OK — task_schedules={ts}"


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
