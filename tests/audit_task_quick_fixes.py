#!/usr/bin/env python3
"""Quick audit for two small task fixes:
  #1  Description editing locked to the task creator only.
  #2  team_stats() exposes separate `todo` + `in_progress` counts so the
      stats page can render the missing "للقيام" column.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── #1: description gating ────────────────────────────────────────────
@check("#1a  _can_edit_description helper exists in tasks.py")
def _():
    src = (ROOT / "app/routes/tasks.py").read_text()
    assert "_can_edit_description" in src, "helper missing"
    assert "task.created_by_id == current_user.id" in src
    return "helper present + checks creator id"


@check("#1b  edit() route gates description with _can_edit_description")
def _():
    src = (ROOT / "app/routes/tasks.py").read_text()
    # The new_desc block should only mutate when _can_edit_description is true
    assert "if _can_edit_description(t):" in src, \
        "edit() doesn't gate the description change"
    return "edit() flow gates description change behind helper"


@check("#1c  inline_edit drops non-creator description silently")
def _():
    src = (ROOT / "app/routes/tasks.py").read_text()
    assert "desc_in = None" in src, \
        "inline_edit doesn't strip non-creator description"
    return "inline_edit silently drops description for non-creators"


@check("#1d  Template hides description editor for non-creators")
def _():
    src = (ROOT / "app/templates/tasks/detail.html").read_text()
    assert "is_creator = task.created_by_id and task.created_by_id == current_user.id" in src
    assert "can_edit and is_creator" in src, \
        "editor not gated by is_creator"
    return "detail.html shows editor only when can_edit AND is_creator"


# ─── #2: TODO bucket in team_stats ─────────────────────────────────────
@check("#2a  team_stats() returns todo + in_progress buckets")
def _():
    from app.services.tasks_extras import team_stats
    from app.models import Company
    company = Company.query.order_by(Company.id).first()
    rows = team_stats(company.id)
    if not rows:
        return "no team data — but team_stats returned an empty list (ok)"
    sample = rows[0]
    for key in ("todo", "in_progress", "review", "blocked", "open", "done"):
        assert key in sample, f"missing key {key!r} in team_stats row"
    return f"row keys present: todo, in_progress, review, blocked, open, done"


@check("#2b  Existing scenario: a TODO task counts in 'todo' bucket")
def _():
    """Create one TODO task assigned to a test user, recount."""
    from app.models import (
        Task, TaskStatus, TaskPriority, Company, User, Employee, EmployeeStatus,
        ContractType, task_assignees,
    )
    from app.services.tasks_extras import team_stats
    from datetime import date

    company = Company.query.order_by(Company.id).first()
    # Reuse the perm-fix test user if it exists; otherwise the demo user.
    user = User.query.filter_by(email="perm_fix_test@example.com").first()
    if not user:
        user = User.query.filter_by(email="demo@manasety.ai").first()
    assert user, "no test user available"

    # Snapshot the user's todo count before
    rows_before = team_stats(company.id)
    before_todo = next((r["todo"] for r in rows_before
                       if r["user"] and r["user"].id == user.id), 0)

    # Create a TODO task assigned to this user
    t = Task(
        company_id=company.id,
        title="TASK_QUICK_FIX_TEST",
        status=TaskStatus.TODO,
        priority=TaskPriority.LOW,
        assigned_to_id=user.id,
        created_by_id=user.id,
    )
    db.session.add(t)
    db.session.flush()
    db.session.execute(task_assignees.insert().values(
        task_id=t.id, user_id=user.id,
    ))
    db.session.commit()

    rows_after = team_stats(company.id)
    after_todo = next((r["todo"] for r in rows_after
                      if r["user"] and r["user"].id == user.id), 0)
    delta = after_todo - before_todo

    # Cleanup
    db.session.execute(task_assignees.delete().where(task_assignees.c.task_id == t.id))
    db.session.delete(t)
    db.session.commit()

    assert delta == 1, f"todo bucket should bump by 1, bumped by {delta}"
    return f"adding 1 TODO task bumped 'todo' bucket from {before_todo} → {after_todo}"


@check("#2c  Template renders للقيام column")
def _():
    src = (ROOT / "app/templates/tasks/stats.html").read_text()
    assert "للقيام" in src, "stats.html missing للقيام column header"
    assert "r.todo" in src, "stats.html doesn't render r.todo"
    assert "r.in_progress" in src, "stats.html doesn't render r.in_progress"
    return "stats.html has للقيام column + uses r.todo / r.in_progress"


def main():
    app = create_app()
    with app.app_context():
        passed = failed = 0
        for label, fn in CHECKS:
            try:
                msg = fn()
                print(f"\033[92mPASS\033[0m  {label}")
                if msg:
                    print(f"        {msg}")
                passed += 1
            except Exception as e:
                print(f"\033[91mFAIL\033[0m  {label}")
                print(f"        {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                failed += 1
        print()
        print(f"  {passed}/{passed + failed} checks passed.")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
