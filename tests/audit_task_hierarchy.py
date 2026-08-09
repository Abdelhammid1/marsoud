#!/usr/bin/env python3
"""MARSOUD-PARENT-CHILD-TASK-HIERARCHY (2026-08-09) — audit for
the new self-FK on Task and the surrounding UI + validation.

Fixture: two companies (home + other) so the cross-tenant
rejection has real fuel. Home carries a 3-level tree already
built in _setup so the descendant + breadcrumb walkers have
something non-trivial to walk. `PARENT_CHANGED` log entries
land in task_activity_logs so check 10 asserts on that table
directly.

Every check verified to fail against pre-migration HEAD.
"""
import sys
from datetime import date
from pathlib import Path

# Windows console defaults to cp1252 — force UTF-8 so print()
# of the Arabic assertion messages doesn't blow up in the
# middle of a check.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__TASKHIER_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import (
        Company, Plan, User, user_companies,
        Task, TaskStatus, TaskPriority, task_assignees,
    )
    from werkzeug.security import generate_password_hash
    from app.services.roles_seed import ensure_roles_ready_for_company

    plan = Plan.query.filter_by(code="__taskhier__").first()
    if not plan:
        plan = Plan(code="__taskhier__", name="TASKHIER",
                    name_ar="TASKHIER", allowed_subitems=None)
        plan.set_modules(["tasks", "projects", "settings"])
        db.session.add(plan); db.session.flush()

    home = Company(name=f"{PREFIX}HOME", base_currency="SAR",
                   plan_id=plan.id, timezone="Asia/Riyadh")
    db.session.add(home); db.session.flush()
    home.intended_plan_id = plan.id
    other = Company(name=f"{PREFIX}OTHER", base_currency="SAR",
                    plan_id=plan.id, timezone="Asia/Riyadh")
    db.session.add(other); db.session.flush()
    other.intended_plan_id = plan.id
    db.session.commit()
    ensure_roles_ready_for_company(home.id)
    ensure_roles_ready_for_company(other.id)

    u = User(email=f"{PREFIX}u@audit.local",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="taskhier user", is_active=True)
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=home.id, role="owner"))
    db.session.commit()

    # Also give the user access to the "other" company so a POST
    # from a fake test client can attempt to touch its task — the
    # cross-tenant guard must reject on parent_task_id, not on
    # user access. (The malicious POST is the actual attack shape:
    # a legitimate user hand-crafting a form.)
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=other.id, role="owner"))
    db.session.commit()

    # Build a 3-level tree in home: root -> child -> grandchild.
    # A shape any less than 3 would let a bug that only recurses
    # one level slip through the descendant walker check.
    def _mk_task(cid, title, parent_id=None):
        t = Task(company_id=cid, title=title,
                 assigned_to_id=u.id, created_by_id=u.id,
                 priority=TaskPriority.MEDIUM,
                 status=TaskStatus.TODO,
                 parent_task_id=parent_id)
        db.session.add(t); db.session.flush()
        db.session.execute(task_assignees.insert().values(
            task_id=t.id, user_id=u.id, assigned_by_id=u.id))
        return t

    root = _mk_task(home.id, f"{PREFIX}root")
    child = _mk_task(home.id, f"{PREFIX}child", parent_id=root.id)
    grand = _mk_task(home.id, f"{PREFIX}grand", parent_id=child.id)

    # A "loose" task the user can promote to a subtask in check 9/10.
    loose = _mk_task(home.id, f"{PREFIX}loose")

    # An other-company task the cross-tenant guard must refuse.
    foreign = _mk_task(other.id, f"{PREFIX}foreign")

    db.session.commit()

    _STATE.update(home_id=home.id, other_id=other.id, user_id=u.id,
                  root_id=root.id, child_id=child.id,
                  grand_id=grand.id, loose_id=loose.id,
                  foreign_id=foreign.id)


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
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
        # Null out parent_task_id FIRST so tasks-in-tasks don't block
        # the tasks table delete on a rebuild-batch DB.
        db.session.execute(text(
            "UPDATE tasks SET parent_task_id=NULL "
            "WHERE company_id=:c"), {"c": cid})
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
            "DELETE FROM companies WHERE id=:c"), {"c": cid})
        db.session.commit()
    db.session.execute(text("DELETE FROM plans WHERE code='__taskhier__'"))
    db.session.commit()
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text(
            "DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _reset_g():
    """Flask-Login caches _login_user on g. Every check that uses
    test_client() wipes it so the previous check's user doesn't
    leak across."""
    from flask import g
    for key in ("_login_user", "active_company", "user_companies",
                "impersonating"):
        try:
            g.pop(key, None)
        except Exception:
            pass


def _client_as_home_user():
    from flask import current_app
    _reset_g()
    c = current_app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["user_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["home_id"]
    return c


def _reload(task_id):
    from app.models import Task
    db.session.expire_all()
    return db.session.get(Task, task_id)


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. Schema: parent_task_id column + FK + index exist")
def _():
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    cols = {c["name"] for c in insp.get_columns("tasks")}
    assert "parent_task_id" in cols, (
        "tasks.parent_task_id column missing — migration didn't apply")
    fks = insp.get_foreign_keys("tasks")
    fk_hit = any(fk.get("referred_table") == "tasks"
                  and "parent_task_id" in (fk.get("constrained_columns") or [])
                  for fk in fks)
    assert fk_hit, "self-FK on tasks.parent_task_id missing"
    idxs = {ix["name"] for ix in insp.get_indexes("tasks")}
    # Alembic auto-creates index for FKs on SQLite too; either the
    # named index OR any index that covers parent_task_id is enough.
    idx_hit = ("ix_tasks_parent_task_id" in idxs
               or any("parent_task_id" in (ix.get("column_names") or [])
                      for ix in insp.get_indexes("tasks")))
    assert idx_hit, "index on tasks.parent_task_id missing"
    return "column + self-FK + index all present"


@check("2. Basic parent/child persist + backref works")
def _():
    root = _reload(_STATE["root_id"])
    child = _reload(_STATE["child_id"])
    assert child.parent_task_id == root.id, (
        f"child.parent_task_id={child.parent_task_id}, want {root.id}")
    assert child.parent is not None and child.parent.id == root.id, (
        "backref child.parent broken")
    subtask_ids = {s.id for s in root.subtasks}
    assert child.id in subtask_ids, (
        f"root.subtasks missing child: {subtask_ids}")
    return f"root #{root.id} .subtasks contains child #{child.id}"


@check("3. validate_parent(t, None) returns None (unset works)")
def _():
    from app.services.task_hierarchy import validate_parent
    root = _reload(_STATE["root_id"])
    assert validate_parent(root, None) is None
    assert validate_parent(root, "") is None
    assert validate_parent(root, "   ") is None
    return "None / '' / whitespace all resolve to unset"


@check("4. validate_parent refuses self")
def _():
    from app.services.task_hierarchy import (
        validate_parent, TaskHierarchyError,
    )
    child = _reload(_STATE["child_id"])
    try:
        validate_parent(child, child.id)
    except TaskHierarchyError as e:
        assert "نفسها" in str(e), f"wrong message: {e}"
        return f"self-loop rejected -> {e}"
    raise AssertionError("self-loop was not rejected")


@check("5. validate_parent refuses a descendant (cycle)")
def _():
    """root has child has grand. Setting root.parent = grand would
    put root under its own descendant. Must refuse."""
    from app.services.task_hierarchy import (
        validate_parent, TaskHierarchyError,
    )
    root = _reload(_STATE["root_id"])
    try:
        validate_parent(root, _STATE["grand_id"])
    except TaskHierarchyError as e:
        assert "دائرية" in str(e), f"wrong message: {e}"
        return f"cycle rejected -> {e}"
    raise AssertionError("descendant-as-parent was not rejected")


@check("6. validate_parent refuses cross-company parent")
def _():
    from app.services.task_hierarchy import (
        validate_parent, TaskHierarchyError,
    )
    root = _reload(_STATE["root_id"])
    try:
        validate_parent(root, _STATE["foreign_id"])
    except TaskHierarchyError as e:
        assert "شركة" in str(e), f"wrong message: {e}"
        return f"cross-tenant parent rejected -> {e}"
    raise AssertionError("cross-company parent was not rejected")


@check("7. descendant_ids + breadcrumb walk full 3-level tree")
def _():
    from app.services.task_hierarchy import (
        descendant_ids, breadcrumb, ancestors,
    )
    root = _reload(_STATE["root_id"])
    grand = _reload(_STATE["grand_id"])
    desc = descendant_ids(root)
    assert _STATE["child_id"] in desc, (
        f"child missing from root descendants: {desc}")
    assert _STATE["grand_id"] in desc, (
        f"grand missing from root descendants: {desc}")
    assert root.id not in desc, "self must not be in descendants"
    crumb = breadcrumb(grand)
    assert len(crumb) == 3, f"breadcrumb want 3, got {len(crumb)}: {crumb}"
    ids = [c["id"] for c in crumb]
    assert ids == [_STATE["root_id"], _STATE["child_id"],
                   _STATE["grand_id"]], f"crumb order wrong: {ids}"
    anc = ancestors(grand)
    assert len(anc) == 2 and anc[0].id == _STATE["root_id"] \
        and anc[1].id == _STATE["child_id"], (
            f"ancestors order wrong: {[a.id for a in anc]}")
    return f"3-level tree walks correctly ({ids})"


@check("8. GET /tasks/<grand_id> renders breadcrumb + parent chip")
def _():
    client = _client_as_home_user()
    r = client.get(f"/tasks/{_STATE['grand_id']}")
    assert r.status_code == 200, f"HTTP {r.status_code}"
    body = r.get_data(as_text=True)
    assert "مهمة أب:" in body, "parent chip label missing"
    # Both ancestor titles must appear as links (breadcrumb).
    assert f"{PREFIX}root" in body, "root title missing from body"
    assert f"{PREFIX}child" in body, "child title missing from body"
    # And the current task title as the terminal breadcrumb entry.
    assert f"{PREFIX}grand" in body, "self title missing"
    return "detail page shows chip + full 3-level breadcrumb"


@check("9. POST /tasks/new with parent_task_id creates subtask")
def _():
    from app.models import Task
    client = _client_as_home_user()
    r = client.post("/tasks/new", data={
        "title": f"{PREFIX}new_sub",
        "priority": "MEDIUM",
        "assignee_ids": str(_STATE["user_id"]),
        "parent_task_id": str(_STATE["root_id"]),
        "schedule_mode": "NONE",
    }, follow_redirects=False)
    assert r.status_code in (200, 302), f"HTTP {r.status_code}"
    db.session.expire_all()
    t = Task.query.filter_by(title=f"{PREFIX}new_sub").first()
    assert t is not None, "new subtask row missing"
    assert t.parent_task_id == _STATE["root_id"], (
        f"parent_task_id not persisted: {t.parent_task_id}")
    return f"POST created task #{t.id} under root #{t.parent_task_id}"


@check("10. POST /tasks/<id>/edit changes parent + writes PARENT_CHANGED log")
def _():
    from app.models import Task, TaskActivityLog
    # Promote the "loose" task into a subtask of root via edit.
    loose_id = _STATE["loose_id"]
    client = _client_as_home_user()
    r = client.post(f"/tasks/{loose_id}/edit", data={
        "title": f"{PREFIX}loose",
        "priority": "MEDIUM",
        "assignee_ids": str(_STATE["user_id"]),
        "parent_task_id": str(_STATE["root_id"]),
    }, follow_redirects=False)
    assert r.status_code in (200, 302), f"HTTP {r.status_code}"
    t = _reload(loose_id)
    assert t.parent_task_id == _STATE["root_id"], (
        f"parent_task_id not persisted on edit: {t.parent_task_id}")
    logs = TaskActivityLog.query.filter_by(
        task_id=loose_id, action="PARENT_CHANGED").all()
    assert logs, "no PARENT_CHANGED activity log entry written"
    return (f"edit set parent + logged PARENT_CHANGED "
            f"({len(logs)} row(s))")


@check("11. delete_task_fully(parent) orphans subtasks (parent_task_id -> NULL)")
def _():
    """Delete the "child" — grand should survive with
    parent_task_id NULL, per ticket rule 'Deleting a parent does
    not delete any subtask'."""
    from app.models import Task
    from app.services.tasks_extras import delete_task_fully
    child = _reload(_STATE["child_id"])
    grand_id = _STATE["grand_id"]
    delete_task_fully(child)
    db.session.expire_all()
    g = db.session.get(Task, grand_id)
    assert g is not None, "grandchild wrongly deleted with parent"
    assert g.parent_task_id is None, (
        f"grand still points at deleted parent: {g.parent_task_id}")
    return "child deleted, grand orphaned to root (parent_task_id=NULL)"


@check("12. Regression: /tasks/new without parent_task_id creates root task")
def _():
    from app.models import Task
    client = _client_as_home_user()
    r = client.post("/tasks/new", data={
        "title": f"{PREFIX}root_task_no_parent",
        "priority": "MEDIUM",
        "assignee_ids": str(_STATE["user_id"]),
        "schedule_mode": "NONE",
    }, follow_redirects=False)
    assert r.status_code in (200, 302), f"HTTP {r.status_code}"
    db.session.expire_all()
    t = Task.query.filter_by(
        title=f"{PREFIX}root_task_no_parent").first()
    assert t is not None, "regression task not created"
    assert t.parent_task_id is None, (
        f"flat task ended up with parent_task_id={t.parent_task_id}")
    return f"flat task #{t.id} created without a parent (regression clean)"


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
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
