#!/usr/bin/env python3
"""MARSOUD-CREATOR-VIEW + MARSOUD-PM-DROPDOWN-FIX (Abdelhamid 2026-07-11).

Two tickets from Rofida via Abdelhamid, audited together because they
share the same permission-model surface.

Ticket A (Image #34) — "مشكلة في عرض المهام":
  When someone else is assigned to a task, the creator gets 403
  and can't follow it. Fix: whoever CREATED the task can open it
  and its comments/attachments, even when not one of the assignees.
  Same rule applied to leads.

Ticket B (Image #33) — "مشكلة في المشاريع":
  The "Project Manager" dropdown when creating a project only lists
  users whose ROLE NAME is project_manager / admin / owner. A user
  with a custom cloned role that grants `projects.manage` was
  silently skipped. Fix: enumerate company members and filter by
  `has_permission("projects.manage")`, not the role name.

Checks:
  1. is_visible_to() → True for a task's creator, even when not an
     assignee (regression: was False → 403 on the detail page).
  2. visible_tasks_query() → includes tasks whose creator is the
     current user (regression: only listed assignee tasks).
  3. HTTP round-trip: creator opens /tasks/<id>/ → 200 (was 403).
  4. HTTP round-trip: creator opens a task where they're neither
     an assignee NOR the creator → 403 (guard did NOT collapse).
  5. Lead: creator can open a lead they authored but assigned
     away → 200 (was 403).
  6. Lead list at /leads/ includes leads the current user created
     but doesn't own → visible on their board.
  7. _project_managers() includes a user with a custom role that
     grants projects.manage (was excluded because role name isn't
     "project_manager").
  8. _project_managers() excludes users without projects.manage
     (a random employee stays out of the dropdown).
"""
import sys
from pathlib import Path

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
            "DELETE FROM users WHERE email LIKE 'cv-%@x.test'"))


def _setup():
    from app.models import (
        Company, User, user_companies, Task, TaskStatus, TaskPriority,
        Lead, LeadStatus,
    )
    from werkzeug.security import generate_password_hash

    for name in ("__CREATOR_VIEW__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__CREATOR_VIEW__", base_currency="SAR")
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

    # Business-user roles that DO have tasks.view + leads.view but
    # do NOT have projects.manage — the perfect fixture role for
    # exercising both the creator-view unlock AND the PM dropdown
    # exclusion.
    creator = _mk("cv-creator@x.test", "sales_rep")
    assignee = _mk("cv-assignee@x.test", "sales_rep")
    stranger = _mk("cv-stranger@x.test", "sales_rep")
    # A user whose ROLE NAME is not project_manager but who we'll
    # grant projects.manage via a custom role permission — verifies
    # the PM dropdown honours DB permissions, not role names.
    custom_pm = _mk("cv-custom-pm@x.test", "team_member")
    # An owner (fixture control — owner should always show up in PM
    # dropdown via the projects.manage permission).
    owner = _mk("cv-owner@x.test", "owner")

    # Task the CREATOR made but assigned away to `assignee`.
    t = Task(
        company_id=a.id, title="Task owned by nobody, created by cv-creator",
        assigned_to_id=assignee.id, created_by_id=creator.id,
        priority=TaskPriority.MEDIUM, status=TaskStatus.TODO,
    )
    # Task the stranger has nothing to do with — used as the negative
    # guard in check 4.
    other = Task(
        company_id=a.id, title="Task the stranger cannot see",
        assigned_to_id=assignee.id, created_by_id=owner.id,
        priority=TaskPriority.MEDIUM, status=TaskStatus.TODO,
    )
    db.session.add_all([t, other]); db.session.flush()

    # Lead the CREATOR authored then reassigned to `assignee`.
    lead = Lead(
        company_id=a.id, client_name="Test Client",
        phone="0500000000", service_needed="whatever",
        status=LeadStatus.NEW_LEAD,
        assigned_to_id=assignee.id, created_by_id=creator.id,
    )
    db.session.add(lead); db.session.commit()

    # Grant custom_pm the `projects.manage` permission via a DB row
    # so has_permission() picks it up — the point of the ticket.
    _grant_projects_manage_to(custom_pm.id, a.id)

    _STATE.update(
        a_id=a.id,
        creator_id=creator.id, assignee_id=assignee.id,
        stranger_id=stranger.id, owner_id=owner.id,
        custom_pm_id=custom_pm.id,
        task_id=t.id, other_task_id=other.id, lead_id=lead.id,
    )


def _grant_projects_manage_to(user_id, company_id):
    """Install a role that has projects.manage granted at the DB
    level and swap this user onto it. Mirrors what the "Edit User
    Permissions" screen does when Abdelhamid ticks the box for
    someone on a custom role."""
    from app.models import (
        Role, Permission, role_permissions, user_companies,
    )
    from sqlalchemy import select
    # Ensure the permission row exists.
    perm = Permission.query.filter_by(code="projects.manage").first()
    if not perm:
        perm = Permission(code="projects.manage",
                          description="Manage projects")
        db.session.add(perm); db.session.flush()
    # Create a role for this test scenario.
    role = Role(
        company_id=company_id, code="custom_pm_clone",
        name_ar="نسخة مدير مشروع",
        type="CUSTOM",
    )
    db.session.add(role); db.session.flush()
    # Grant projects.manage (idempotent — some role auto-seeders may
    # already have populated the row).
    already = db.session.execute(
        role_permissions.select().where(
            role_permissions.c.role_id == role.id,
            role_permissions.c.permission_id == perm.id,
        )
    ).fetchone()
    if not already:
        db.session.execute(role_permissions.insert().values(
            role_id=role.id, permission_id=perm.id,
        ))
    # Attach this role to the user in this company.
    db.session.execute(
        user_companies.update()
        .where(user_companies.c.user_id == user_id)
        .where(user_companies.c.company_id == company_id)
        .values(role_id=role.id)
    )
    db.session.commit()


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
              "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


# ─── Task creator visibility ─────────────────────────────────────────
@check("1. is_visible_to() → True for the creator, not the assignee")
def _():
    from app.services.tasks_extras import is_visible_to
    from app.models import Task
    t = db.session.get(Task, _STATE["task_id"])
    assert is_visible_to(t, _STATE["creator_id"],
                         full_visibility=False), \
        "creator was blocked from their own task"
    # Sanity: stranger is still 403.
    assert not is_visible_to(t, _STATE["stranger_id"],
                             full_visibility=False), \
        "stranger got access — visibility rule leaked"
    return "creator ✓  stranger ✗"


@check("2. visible_tasks_query() includes tasks whose creator = user")
def _():
    from app.services.tasks_extras import visible_tasks_query
    q = visible_tasks_query(
        _STATE["a_id"], _STATE["creator_id"],
        full_visibility=False, pm_project_ids=None,
    )
    ids = {t.id for t in q.all()}
    assert _STATE["task_id"] in ids, \
        "creator's task not returned by visible_tasks_query"
    return f"task {_STATE['task_id']} in creator's list"


@check("3. HTTP: creator opens /tasks/<id>/ → 200")
def _():
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["creator_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    r = client.get(f"/tasks/{_STATE['task_id']}", follow_redirects=True)
    assert r.status_code == 200, \
        f"creator got {r.status_code} on their own task"
    return "200 OK"


@check("4. HTTP: stranger opens same task → 403 (guard didn't collapse)")
def _():
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["stranger_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    r = client.get(f"/tasks/{_STATE['task_id']}", follow_redirects=True)
    assert r.status_code == 403, \
        f"stranger got {r.status_code} — access control broken"
    return "403 as expected"


# ─── Lead creator visibility ─────────────────────────────────────────
@check("5. HTTP: creator opens /leads/<id>/ they no longer own → 200")
def _():
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["creator_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    r = client.get(f"/leads/{_STATE['lead_id']}", follow_redirects=True)
    assert r.status_code == 200, \
        f"lead creator got {r.status_code} — should be 200"
    return "200 OK"


@check("6. /leads/ list includes leads the current user created")
def _():
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["creator_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    r = client.get("/leads/", follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    # Client name from fixture must appear in the response body.
    assert b"Test Client" in r.data, \
        "lead the creator authored was hidden from their /leads/ list"
    return "lead visible in creator's list"


# ─── PM dropdown fix ─────────────────────────────────────────────────
@check("7. _project_managers() includes custom-role user with projects.manage")
def _():
    from flask import g
    from app.routes.projects import _project_managers
    from app.models import Company
    _reset_g()
    g.active_company = db.session.get(Company, _STATE["a_id"])
    pms = _project_managers()
    ids = {u.id for u in pms if u}
    assert _STATE["custom_pm_id"] in ids, \
        "custom-role user with projects.manage was missed"
    assert _STATE["owner_id"] in ids, \
        "owner disappeared from PM dropdown"
    return f"custom_pm + owner both present ({len(ids)} PMs)"


@check("8. _project_managers() excludes plain employees without projects.manage")
def _():
    from flask import g
    from app.routes.projects import _project_managers
    from app.models import Company
    _reset_g()
    g.active_company = db.session.get(Company, _STATE["a_id"])
    pms = _project_managers()
    ids = {u.id for u in pms if u}
    assert _STATE["creator_id"] not in ids, \
        "plain-employee creator leaked into PM dropdown"
    assert _STATE["assignee_id"] not in ids, \
        "plain-employee assignee leaked into PM dropdown"
    assert _STATE["stranger_id"] not in ids, \
        "plain-employee stranger leaked into PM dropdown"
    return "employees stay out"


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
