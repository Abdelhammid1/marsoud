#!/usr/bin/env python3
"""MARSOUD-RECURRING-TASKS (Abdelhamid 2026-07-22).

Take an existing Task, convert it to a recurring series, generate
future occurrences from cron.

Checks:
  1. promote_to_recurring stamps the task + creates a series row.
  2. Refuses to double-promote a task that's already in a series.
  3. DAILY frequency generates one task per day until end condition.
  4. WEEKLY frequency generates once every 7 days.
  5. CUSTOM interval respects interval_count.
  6. END_AFTER_N stops after the requested count.
  7. END_ON_DATE stops on the boundary.
  8. Skip dates are honoured — no task generated on those dates.
  9. delete_series APPLY_THIS deletes just one task.
 10. delete_series APPLY_THIS_AND_FUTURE deletes future + deactivates.
 11. delete_series APPLY_ALL nukes everything.
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

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


def _teardown():
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        target_cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__REC_%__'"))]
        for cid in target_cids:
            conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"),
                          {"c": cid})
            # recurring_task_exceptions is scoped through series_id
            # (no company_id) — same class of orphan risk as
            # invoice_items pre-Ticket-orphan-cascade fix. Purge
            # via the series lookup FIRST.
            conn.execute(text(
                "DELETE FROM recurring_task_exceptions WHERE series_id IN "
                "(SELECT id FROM recurring_task_series WHERE company_id = :c)"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                                  {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"), {"c": cid})
        # Zombie sweep for orphan exceptions from prior buggy runs.
        conn.execute(text(
            "DELETE FROM recurring_task_exceptions WHERE series_id NOT IN "
            "(SELECT id FROM recurring_task_series)"))
        conn.execute(text("DELETE FROM users WHERE email LIKE 'rec-%@x.test'"))


def _setup_task(title):
    from app.models import (
        Company, User, Task, TaskStatus, TaskPriority, Plan,
    )
    from app.models.user import user_companies
    from app.services.subscription import activate_default_subscription
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    from datetime import datetime as _dt

    # Fresh company each time — cheap since we teardown at end.
    slug = title.lower().replace("_", "-")
    cname = f"__REC_{title}__"
    c = Company(name=cname, base_currency="EGP",
                subdomain=f"rec-{slug}")
    activate_default_subscription(c)
    ent = Plan.query.filter_by(code="enterprise").first()
    if ent:
        c.plan_id = ent.id
        c.intended_plan_id = ent.id
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email=f"rec-{slug}@x.test",
             password_hash=generate_password_hash("x", method="pbkdf2:sha256"),
             full_name="rec", is_active=True,
             email_verified_at=_dt.utcnow())
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))

    t = Task(
        company_id=c.id, title=f"مهمة اختبار {title}",
        assigned_to_id=u.id, created_by_id=u.id,
        status=TaskStatus.TODO, priority=TaskPriority.MEDIUM,
        deadline=date.today(),
    )
    db.session.add(t); db.session.commit()
    return c, u, t


@check("1. promote_to_recurring stamps the task + creates a series")
def _():
    from app.services.recurring_tasks import promote_to_recurring
    from app.models import FREQ_DAILY, RecurringTaskSeries
    _teardown()
    _c, _u, t = _setup_task("PROMO")
    s = promote_to_recurring(t, frequency=FREQ_DAILY)
    db.session.commit()
    assert t.recurring_series_id == s.id
    assert t.occurrence_index == 1
    assert s.template_task_id == t.id
    assert s.generated_count == 1
    _STATE["promo_series_id"] = s.id
    return "series created + task stamped"


@check("2. Refuses to double-promote a task")
def _():
    from app.services.recurring_tasks import (
        promote_to_recurring, RecurringTaskError,
    )
    from app.models import Task, FREQ_DAILY
    t = Task.query.filter_by(company_id=None).first()   # placeholder
    from app.models import RecurringTaskSeries
    t_id = RecurringTaskSeries.query.filter_by(
        id=_STATE["promo_series_id"]).one().template_task_id
    t = db.session.get(Task, t_id)
    try:
        promote_to_recurring(t, frequency=FREQ_DAILY)
        raised = False
    except RecurringTaskError:
        raised = True
    assert raised, "expected RecurringTaskError"
    return "double-promotion refused"


@check("3. DAILY frequency generates one task per day")
def _():
    from app.services.recurring_tasks import (
        promote_to_recurring, generate_due_occurrences,
    )
    from app.models import Task, FREQ_DAILY
    c, u, t = _setup_task("DAILY")
    # Set deadline 3 days ago so a cron tick with today=today
    # generates day-1, day-2, today = 3 new tasks.
    t.deadline = date.today() - timedelta(days=3)
    db.session.commit()
    s = promote_to_recurring(t, frequency=FREQ_DAILY)
    db.session.commit()
    made = generate_due_occurrences(today=date.today())
    made_this_series = [m for m in made if m.recurring_series_id == s.id]
    # Expected: 3 new tasks (day-2, day-1, today).
    assert len(made_this_series) == 3, \
        f"got {len(made_this_series)} new tasks"
    # Ordering: last_generated_date should be today.
    assert s.last_generated_date == date.today()
    return f"generated {len(made_this_series)} tasks"


@check("4. WEEKLY frequency generates once every 7 days")
def _():
    from app.services.recurring_tasks import (
        promote_to_recurring, generate_due_occurrences,
    )
    from app.models import FREQ_WEEKLY
    c, u, t = _setup_task("WEEKLY")
    t.deadline = date.today() - timedelta(days=14)
    db.session.commit()
    s = promote_to_recurring(t, frequency=FREQ_WEEKLY)
    db.session.commit()
    made = generate_due_occurrences(today=date.today())
    made_this = [m for m in made if m.recurring_series_id == s.id]
    # Anchor was today-14. Next weekly: today-7. Then today.
    # So 2 new tasks.
    assert len(made_this) == 2, f"got {len(made_this)}"
    return "1 per 7 days"


@check("5. CUSTOM interval respects interval_count")
def _():
    from app.services.recurring_tasks import (
        promote_to_recurring, generate_due_occurrences,
    )
    from app.models import FREQ_CUSTOM
    c, u, t = _setup_task("CUSTOM")
    t.deadline = date.today() - timedelta(days=10)
    db.session.commit()
    s = promote_to_recurring(t, frequency=FREQ_CUSTOM,
                              interval_count=3)
    db.session.commit()
    made = generate_due_occurrences(today=date.today())
    made_this = [m for m in made if m.recurring_series_id == s.id]
    # anchor: today-10. next: today-7, then today-4, then today-1.
    # Today is NOT the next boundary (today-1 + 3 = today+2).
    # So 3 tasks.
    assert len(made_this) == 3, f"got {len(made_this)}"
    return f"custom interval=3 → {len(made_this)} tasks"


@check("6. END_AFTER_N stops after count")
def _():
    from app.services.recurring_tasks import (
        promote_to_recurring, generate_due_occurrences,
    )
    from app.models import FREQ_DAILY, END_AFTER_N
    c, u, t = _setup_task("AFTERN")
    t.deadline = date.today() - timedelta(days=10)
    db.session.commit()
    s = promote_to_recurring(t, frequency=FREQ_DAILY,
                              end_condition=END_AFTER_N, end_count=3)
    db.session.commit()
    made = generate_due_occurrences(today=date.today())
    made_this = [m for m in made if m.recurring_series_id == s.id]
    # Should stop at 3 total occurrences (including the template).
    # generated_count already = 1 after promote. So 2 more get made.
    assert s.generated_count == 3, \
        f"generated_count={s.generated_count}"
    assert not s.active, "series should deactivate at N"
    return f"stopped at {s.generated_count}"


@check("7. END_ON_DATE stops on boundary")
def _():
    from app.services.recurring_tasks import (
        promote_to_recurring, generate_due_occurrences,
    )
    from app.models import FREQ_DAILY, END_ON_DATE
    c, u, t = _setup_task("ONDATE")
    t.deadline = date.today() - timedelta(days=10)
    db.session.commit()
    s = promote_to_recurring(t, frequency=FREQ_DAILY,
                              end_condition=END_ON_DATE,
                              end_date=date.today() - timedelta(days=5))
    db.session.commit()
    made = generate_due_occurrences(today=date.today())
    made_this = [m for m in made if m.recurring_series_id == s.id]
    # anchor today-10 (occurrence 1). Next: today-9, today-8, today-7,
    # today-6, today-5. End on today-5, so today-4 doesn't fire.
    # Total occurrences: 6.
    assert s.generated_count <= 6, \
        f"generated_count={s.generated_count}"
    assert not s.active
    return f"stopped at end_date, count={s.generated_count}"


@check("8. Skip dates honoured")
def _():
    from app.services.recurring_tasks import (
        promote_to_recurring, generate_due_occurrences,
    )
    from app.models import FREQ_DAILY
    c, u, t = _setup_task("SKIP")
    t.deadline = date.today() - timedelta(days=5)
    db.session.commit()
    # Skip today-3 and today-1.
    exceptions = [date.today() - timedelta(days=3),
                   date.today() - timedelta(days=1)]
    s = promote_to_recurring(t, frequency=FREQ_DAILY,
                              exception_dates=exceptions)
    db.session.commit()
    made = generate_due_occurrences(today=date.today())
    made_this = [m for m in made if m.recurring_series_id == s.id]
    # anchor: today-5 (occurrence 1). Would-be next: t-4, t-3, t-2, t-1, t.
    # Skip t-3 and t-1 → generated: t-4, t-2, t = 3.
    assert len(made_this) == 3, \
        f"got {len(made_this)}, dates: {[m.deadline for m in made_this]}"
    dates_made = {m.deadline for m in made_this}
    assert date.today() - timedelta(days=3) not in dates_made
    assert date.today() - timedelta(days=1) not in dates_made
    return "skip dates skipped"


@check("9. delete_series APPLY_THIS removes one task")
def _():
    from app.services.recurring_tasks import (
        promote_to_recurring, generate_due_occurrences,
        delete_series, APPLY_THIS,
    )
    from app.models import Task, FREQ_DAILY
    c, u, t = _setup_task("DELTHIS")
    t.deadline = date.today() - timedelta(days=2)
    db.session.commit()
    s = promote_to_recurring(t, frequency=FREQ_DAILY)
    db.session.commit()
    generate_due_occurrences(today=date.today())
    # Now we have 3 tasks in the series.
    all_tasks = Task.query.filter_by(recurring_series_id=s.id).all()
    assert len(all_tasks) == 3
    # Delete just the middle one.
    middle = [t for t in all_tasks if t.occurrence_index == 2][0]
    delete_series(s, mode=APPLY_THIS, from_task=middle)
    remaining = Task.query.filter_by(recurring_series_id=s.id).all()
    assert len(remaining) == 2, f"got {len(remaining)}"
    return "1 task removed, 2 remain, series intact"


@check("10. delete_series APPLY_THIS_AND_FUTURE cuts off")
def _():
    from app.services.recurring_tasks import (
        promote_to_recurring, generate_due_occurrences,
        delete_series, APPLY_THIS_AND_FUTURE,
    )
    from app.models import Task, FREQ_DAILY
    c, u, t = _setup_task("DELFUT")
    t.deadline = date.today() - timedelta(days=4)
    db.session.commit()
    s = promote_to_recurring(t, frequency=FREQ_DAILY)
    db.session.commit()
    generate_due_occurrences(today=date.today())
    tasks = Task.query.filter_by(
        recurring_series_id=s.id).order_by(Task.occurrence_index).all()
    # Cut at occurrence_index=3.
    third = tasks[2]
    delete_series(s, mode=APPLY_THIS_AND_FUTURE, from_task=third)
    remaining = Task.query.filter_by(recurring_series_id=s.id).all()
    assert len(remaining) == 2, \
        f"expected 2 remain (1,2), got {len(remaining)}"
    assert not s.active
    return f"cut at #3 → {len(remaining)} left"


@check("11. delete_series APPLY_ALL nukes everything")
def _():
    from app.services.recurring_tasks import (
        promote_to_recurring, generate_due_occurrences,
        delete_series, APPLY_ALL,
    )
    from app.models import (
        Task, RecurringTaskSeries, FREQ_DAILY,
    )
    c, u, t = _setup_task("DELALL")
    t.deadline = date.today() - timedelta(days=2)
    db.session.commit()
    s = promote_to_recurring(t, frequency=FREQ_DAILY)
    db.session.commit()
    generate_due_occurrences(today=date.today())
    sid = s.id
    delete_series(s, mode=APPLY_ALL)
    remaining = Task.query.filter_by(recurring_series_id=sid).all()
    assert len(remaining) == 0
    assert db.session.get(RecurringTaskSeries, sid) is None
    return "series + all tasks gone"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _teardown()
            for label, fn in CHECKS:
                try:
                    res = fn()
                    print(f"PASS  {label}  ⇒ {res}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback; traceback.print_exc()
        finally:
            _teardown()
            print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
