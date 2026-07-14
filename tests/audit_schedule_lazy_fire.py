#!/usr/bin/env python3
"""MARSOUD-SCHEDULE-LAZY-FIRE (Abdelhamid 2026-07-14).

Kill the external-cron dependency for recurring tasks. When any
authenticated user opens any page, if the daily materializer hasn't
run in the last 15 minutes for this company, kick it off. Result:
recurring tasks self-heal as long as anyone uses the app that day.

Checks:
  1. materialize_due_schedules(company_id=X) scopes to that company
     only — other companies' schedules untouched.
  2. materialize_due_schedules(company_id=None) is unchanged
     (backward-compat with the cron path).
  3. First authenticated GET after a DAILY schedule was created
     yesterday spawns today's task WITHOUT any cron call.
  4. Second GET within the 15-min throttle window is a no-op
     (throttle holds — no duplicate DB work).
  5. Anonymous / unauthenticated requests never trigger the lazy fire.
  6. Endpoints on the skip-list (static, auth, cron) never trigger it.
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
        conn.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
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
            "DELETE FROM users WHERE email LIKE 'lzf-%@x.test'"))
        # Sweep orphan M2M rows so a rerun after ID reuse doesn't
        # collide.
        conn.execute(text(
            "DELETE FROM task_schedule_assignees WHERE schedule_id NOT IN "
            "(SELECT id FROM task_schedules)"))


def _setup():
    from app.models import Company, User, user_companies
    from werkzeug.security import generate_password_hash

    for name in ("__LZF_A__", "__LZF_B__"):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)

    a = Company(name="__LZF_A__", base_currency="SAR",
                 timezone="Asia/Riyadh")
    b = Company(name="__LZF_B__", base_currency="SAR",
                 timezone="Asia/Riyadh")
    db.session.add_all([a, b]); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(a.id); seed_default_coa(b.id)

    def _mk(email, cid, role):
        u = User(email=email,
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name=email.split("@")[0])
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=cid, role=role))
        return u

    owner_a = _mk("lzf-owner-a@x.test", a.id, "owner")
    assignee_a = _mk("lzf-assign-a@x.test", a.id, "sales_rep")
    owner_b = _mk("lzf-owner-b@x.test", b.id, "owner")
    assignee_b = _mk("lzf-assign-b@x.test", b.id, "sales_rep")
    db.session.commit()

    _STATE.update(
        a_id=a.id, b_id=b.id,
        owner_a_id=owner_a.id, assignee_a_id=assignee_a.id,
        owner_b_id=owner_b.id, assignee_b_id=assignee_b.id,
    )


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
              "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


# ─── Service scoping ──────────────────────────────────────────────
@check("1. materialize_due_schedules(company_id=A) doesn't touch company B")
def _():
    from app.services.task_schedules import (
        create_schedule, materialize_due_schedules,
    )
    from app.models import Task, TaskSchedule
    today = date.today()

    # Schedule in company A + schedule in company B, both DAILY
    # covering today (create_schedule fires immediately, so their
    # day-0 task already exists). We want to prove that a rerun
    # scoped to A doesn't re-trigger anything in B.
    s_a = create_schedule(
        company_id=_STATE["a_id"],
        created_by_id=_STATE["owner_a_id"],
        title="lzf-A-task", description=None, priority="MEDIUM",
        project_id=None, milestone_id=None, notes=None,
        assignee_ids=[_STATE["assignee_a_id"]],
        recurrence="DAILY",
        start_date=today, end_date=today + timedelta(days=2),
    )
    s_b = create_schedule(
        company_id=_STATE["b_id"],
        created_by_id=_STATE["owner_b_id"],
        title="lzf-B-task", description=None, priority="MEDIUM",
        project_id=None, milestone_id=None, notes=None,
        assignee_ids=[_STATE["assignee_b_id"]],
        recurrence="DAILY",
        start_date=today, end_date=today + timedelta(days=2),
    )

    # Roll BOTH schedules back so a rerun today can spawn again.
    s_a.last_generated_date = None
    s_b.last_generated_date = None
    db.session.commit()

    tasks_a_before = Task.query.filter_by(
        company_id=_STATE["a_id"], title="lzf-A-task").count()
    tasks_b_before = Task.query.filter_by(
        company_id=_STATE["b_id"], title="lzf-B-task").count()

    materialize_due_schedules(company_id=_STATE["a_id"])

    tasks_a_after = Task.query.filter_by(
        company_id=_STATE["a_id"], title="lzf-A-task").count()
    tasks_b_after = Task.query.filter_by(
        company_id=_STATE["b_id"], title="lzf-B-task").count()

    assert tasks_a_after == tasks_a_before + 1, \
        f"A didn't fire: {tasks_a_before} → {tasks_a_after}"
    assert tasks_b_after == tasks_b_before, \
        f"B fired unexpectedly: {tasks_b_before} → {tasks_b_after}"
    _STATE["s_a_id"] = s_a.id
    _STATE["s_b_id"] = s_b.id
    return f"A: +1, B: unchanged"


@check("2. materialize_due_schedules(company_id=None) fires everywhere (cron path)")
def _():
    from app.services.task_schedules import materialize_due_schedules
    from app.models import Task, TaskSchedule
    # Both schedules already fired today via check 1 (A) + immediate
    # fire (B). Reset both and call the unscoped materializer.
    s_a = db.session.get(TaskSchedule, _STATE["s_a_id"])
    s_b = db.session.get(TaskSchedule, _STATE["s_b_id"])
    s_a.last_generated_date = None
    s_b.last_generated_date = None
    db.session.commit()
    a_before = Task.query.filter_by(
        company_id=_STATE["a_id"], title="lzf-A-task").count()
    b_before = Task.query.filter_by(
        company_id=_STATE["b_id"], title="lzf-B-task").count()
    materialize_due_schedules()   # no company_id → both
    a_after = Task.query.filter_by(
        company_id=_STATE["a_id"], title="lzf-A-task").count()
    b_after = Task.query.filter_by(
        company_id=_STATE["b_id"], title="lzf-B-task").count()
    assert a_after == a_before + 1 and b_after == b_before + 1, \
        f"unscoped fire skipped a company: A {a_before}→{a_after}, B {b_before}→{b_after}"
    return "A: +1, B: +1"


# ─── Lazy-fire via HTTP ────────────────────────────────────────────
@check("3. Authenticated GET spawns due task WITHOUT calling the cron")
def _():
    """The lazy-fire hook fires materialize_due_schedules(company_id=X)
    on the first authenticated GET for a company within the throttle
    window. We simulate a fresh day by reactivating the schedule +
    resetting the throttle memo, then hitting a normal page."""
    from flask import current_app
    from app.models import Task, TaskSchedule

    # Reset schedule + wipe throttle memo so the hook can fire.
    s_a = db.session.get(TaskSchedule, _STATE["s_a_id"])
    s_a.last_generated_date = None
    s_a.active = True
    db.session.commit()
    # The throttle memo lives inside create_app's closure — grab it
    # via a fresh app instance's before_request stack.
    # Simpler: just wait > 15 minutes (test skips this by using a
    # fresh app instance which starts with an empty memo).
    fresh_app = create_app()
    with fresh_app.app_context():
        client = fresh_app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(_STATE["owner_a_id"])
            sess["_fresh"] = True
            sess["active_company_id"] = _STATE["a_id"]
        before = Task.query.filter_by(
            company_id=_STATE["a_id"], title="lzf-A-task").count()
        # Hit ANY page — the hook runs before_request.
        r = client.get("/home", follow_redirects=False)
        assert r.status_code in (200, 302), f"got {r.status_code}"
    # Task count in the outer session.
    db.session.expire_all()
    after = Task.query.filter_by(
        company_id=_STATE["a_id"], title="lzf-A-task").count()
    assert after == before + 1, \
        f"lazy fire didn't spawn a task (before={before} after={after})"
    return f"page hit → task spawned (before={before}, after={after})"


@check("4. Second GET within throttle window doesn't re-fire (no dup work)")
def _():
    from app.models import Task, TaskSchedule
    # Reset the schedule so IF the throttle failed, we'd see another
    # spawn. If throttle works, count stays flat.
    s_a = db.session.get(TaskSchedule, _STATE["s_a_id"])
    s_a.last_generated_date = None
    db.session.commit()

    fresh_app = create_app()
    with fresh_app.app_context():
        client = fresh_app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(_STATE["owner_a_id"])
            sess["_fresh"] = True
            sess["active_company_id"] = _STATE["a_id"]
        # First GET — will fire (throttle empty in fresh_app).
        client.get("/home")
        db.session.expire_all()
        after_first = Task.query.filter_by(
            company_id=_STATE["a_id"], title="lzf-A-task").count()
        # Reset again — mid-throttle-window rerun should NOT fire.
        s_a = db.session.get(TaskSchedule, _STATE["s_a_id"])
        s_a.last_generated_date = None
        db.session.commit()
        client.get("/home")
        db.session.expire_all()
        after_second = Task.query.filter_by(
            company_id=_STATE["a_id"], title="lzf-A-task").count()
    assert after_second == after_first, \
        f"throttle failed: {after_first} → {after_second}"
    return f"throttle held (both hits = {after_first})"


@check("5. Anonymous request never triggers lazy fire")
def _():
    from app.models import Task, TaskSchedule
    s_a = db.session.get(TaskSchedule, _STATE["s_a_id"])
    s_a.last_generated_date = None
    db.session.commit()

    fresh_app = create_app()
    with fresh_app.app_context():
        client = fresh_app.test_client()
        # No session — anonymous.
        before = Task.query.filter_by(
            company_id=_STATE["a_id"], title="lzf-A-task").count()
        client.get("/login")
        db.session.expire_all()
        after = Task.query.filter_by(
            company_id=_STATE["a_id"], title="lzf-A-task").count()
    assert after == before, \
        f"anonymous request triggered fire: {before} → {after}"
    return "anon GET is a no-op"


@check("6. /cron/* + static endpoints don't self-trigger the hook")
def _():
    """Regression guard: the hook must skip cron + static endpoints,
    otherwise the cron endpoint itself would try to run the same
    logic twice (once via the hook, once via its own code)."""
    src = (ROOT / "app/__init__.py").read_text(encoding="utf-8")
    # Confirm the skip-list is present in the hook.
    assert 'startswith(("static", "auth.", "cron."))' in src, \
        "skip-list missing from lazy-fire hook"
    assert 'request.method != "GET"' in src, \
        "GET-only guard missing from lazy-fire hook"
    return "skip-list + GET-only guard both present"


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
                for k in ("a_id", "b_id"):
                    if k in _STATE:
                        _teardown(_STATE[k])
                print("\n(cleaned up fixture companies)")
            except Exception as e:
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
