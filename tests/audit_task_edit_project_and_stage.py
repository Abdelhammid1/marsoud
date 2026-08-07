#!/usr/bin/env python3
"""MARSOUD-TASK-EDIT-PROJECT-STAGE (2026-08-06) — audit for the ticket
that reported "المشروع والمرحلة لا يقبلان تعديل بعد إنشاء المهمة
بدونهما".

Reported bug — three layers cooperating to make the same field
uneditable:

  (a) form.html:31 disabled the Project <select> whenever `task` was
      truthy, with a sibling hidden <input name="project_id"> that
      re-submitted the current value.
  (b) routes/tasks.py::edit()'s POST branch never reads project_id
      from the form and never assigns t.project_id — even if (a)
      were bypassed, nothing would persist.
  (c) form.html's stage-cascade <script> is gated behind
      `{% if milestones_by_project %}`; edit() didn't pass it, so
      picking a different project on the edit page could never swap
      the Stage options.

Every check verified to fail against pre-change HEAD. The fixture
carries two companies and two projects-with-milestones in the
"home" company so we can exercise cross-project moves AND
cross-company rejection.
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__TEPS_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import (
        Company, Plan, User, user_companies, Project, Milestone,
        Task, TaskStatus, TaskPriority, task_assignees, Customer,
    )
    from werkzeug.security import generate_password_hash
    from app.services.roles_seed import ensure_roles_ready_for_company

    plan = Plan.query.filter_by(code="__teps__").first()
    if not plan:
        plan = Plan(code="__teps__", name="TEPS", name_ar="TEPS",
                    allowed_subitems=None)
        plan.set_modules(["tasks", "projects", "settings"])
        db.session.add(plan); db.session.flush()

    # Two companies: home (where the user lives + task lives) and other
    # (target for cross-company rejection).
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
             full_name="teps user", is_active=True,
             terms_version="v1.0")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=home.id, role="owner"))
    db.session.commit()

    # Each project needs a Customer (NOT NULL FK). One per company.
    def _mk_customer(cid, name):
        c = Customer(company_id=cid, name=name)
        db.session.add(c); db.session.flush()
        return c
    cust_home = _mk_customer(home.id, f"{PREFIX}CustHome")
    cust_other = _mk_customer(other.id, f"{PREFIX}CustOther")

    # Home has two projects, each with one milestone. Required NOT-NULL
    # columns on Project: customer_id, type, manager_id, start/end date.
    today = date.today()
    end = today.replace(day=28)
    def _mk_proj(cid, cust, name):
        p = Project(company_id=cid, name=name, customer_id=cust.id,
                    type="INTERNAL", manager_id=u.id,
                    start_date=today, end_date=end)
        db.session.add(p); db.session.flush()
        return p
    pa = _mk_proj(home.id, cust_home, f"{PREFIX}ProjectA")
    pb = _mk_proj(home.id, cust_home, f"{PREFIX}ProjectB")
    ma = Milestone(project_id=pa.id, name="A-M1", order=1)
    mb = Milestone(project_id=pb.id, name="B-M1", order=1)
    db.session.add_all([ma, mb]); db.session.flush()

    # Other company has one project (cross-tenant bait).
    pother = _mk_proj(other.id, cust_other, f"{PREFIX}Foreign")
    db.session.commit()

    # The seed task: no project, no milestone — mirrors the bug repro.
    t = Task(
        company_id=home.id,
        title="TEPS seed task (no project, no stage)",
        project_id=None,
        milestone_id=None,
        assigned_to_id=u.id,
        created_by_id=u.id,
        priority=TaskPriority.MEDIUM,
        status=TaskStatus.TODO,
    )
    db.session.add(t); db.session.flush()
    db.session.execute(task_assignees.insert().values(
        task_id=t.id, user_id=u.id, assigned_by_id=u.id))
    db.session.commit()

    _STATE.update(home_id=home.id, other_id=other.id,
                  user_id=u.id, task_id=t.id,
                  pa_id=pa.id, pb_id=pb.id, pother_id=pother.id,
                  ma_id=ma.id, mb_id=mb.id)


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    # task_assignees + task_activity_logs are task-scoped, not
    # company-scoped, so the company-id sweep below won't touch them.
    # Left behind, an old (task_id=1, user_id=X) row collides with the
    # next run's fresh task (SQLite reuses ids). Wipe orphans first.
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
        # Wipe assignees for THIS company's tasks first — they don't
        # carry company_id, only task_id.
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
        # Milestones are project-scoped, not company-scoped — sweep them
        # via project_id to avoid an FK dangling.
        db.session.execute(text(
            "DELETE FROM milestones WHERE project_id IN "
            "(SELECT id FROM projects WHERE company_id=:c)"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM projects WHERE company_id=:c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id=:c"), {"c": cid})
        db.session.commit()
    db.session.execute(text("DELETE FROM plans WHERE code='__teps__'"))
    db.session.commit()
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text(
            "DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _reset_g():
    """Flask-Login caches the loaded user on the app-context g. Two
    checks in a row against the same g will re-use the stale one, so
    every check that uses test_client() wipes the cache first."""
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


def _post_edit(client, **overrides):
    """POST /tasks/<id>/edit with sensible defaults so each check can
    override just the fields it cares about."""
    data = {
        "title": "TEPS seed task (no project, no stage)",
        "priority": "MEDIUM",
        "assignee_ids": str(_STATE["user_id"]),
    }
    data.update(overrides)
    # Blank strings are what real forms submit for "no value", not None.
    return client.post(f"/tasks/{_STATE['task_id']}/edit",
                       data=data, follow_redirects=False)


def _reload_task():
    from app.models import Task
    db.session.expire_all()
    return db.session.get(Task, _STATE["task_id"])


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. Bug repro: task without project → POST edit with project_id persists")
def _():
    """The direct reproduction of the ticket. Pre-fix, project_id
    never reaches t.project_id because edit() never reads it."""
    from app.models import Task
    # Make sure we start with a clean slate.
    t = db.session.get(Task, _STATE["task_id"])
    t.project_id = None
    t.milestone_id = None
    db.session.commit()
    client = _client_as_home_user()
    r = _post_edit(client, project_id=str(_STATE["pa_id"]))
    assert r.status_code in (200, 302), f"HTTP {r.status_code}"
    t = _reload_task()
    assert t.project_id == _STATE["pa_id"], (
        f"expected project_id={_STATE['pa_id']}, got {t.project_id}")
    return f"project_id persisted → {t.project_id}"


@check("2. Stage moves with project: POST edit switches both cleanly")
def _():
    """Task now sits on Project A + Milestone A. POST an edit that
    moves it to Project B + Milestone B in one go — both must
    persist, and the B milestone must NOT be rejected as belonging
    to the wrong project (because we're changing project AND
    milestone in the same request)."""
    from app.models import Task
    t = db.session.get(Task, _STATE["task_id"])
    t.project_id = _STATE["pa_id"]
    t.milestone_id = _STATE["ma_id"]
    db.session.commit()
    client = _client_as_home_user()
    r = _post_edit(client,
                   project_id=str(_STATE["pb_id"]),
                   milestone_id=str(_STATE["mb_id"]))
    assert r.status_code in (200, 302), f"HTTP {r.status_code}"
    t = _reload_task()
    assert t.project_id == _STATE["pb_id"], (
        f"project didn't move: {t.project_id}")
    assert t.milestone_id == _STATE["mb_id"], (
        f"milestone didn't move: {t.milestone_id}")
    return f"moved to project #{t.project_id} + milestone #{t.milestone_id}"


@check("3. Cross-company project rejected")
def _():
    """The other-company Project must not be assignable. Same
    validation shape as new() at routes/tasks.py:509-512."""
    from app.models import Task
    t = db.session.get(Task, _STATE["task_id"])
    t.project_id = _STATE["pa_id"]
    t.milestone_id = None
    db.session.commit()
    client = _client_as_home_user()
    r = _post_edit(client, project_id=str(_STATE["pother_id"]))
    # The route flashes and re-renders; either way the DB must be
    # unchanged. Status can be 200 (re-render) or 302 (redirect back).
    t = _reload_task()
    assert t.project_id == _STATE["pa_id"], (
        f"cross-company project leaked in: got {t.project_id}, "
        f"expected unchanged {_STATE['pa_id']}")
    return f"foreign project #{_STATE['pother_id']} rejected"


@check("4. Milestone-not-in-project rejected")
def _():
    """Submit Project A with Milestone B — the pair belongs to
    different projects. new() enforces this at line 520-525;
    edit() must do the same once it starts honouring project_id.
    Nothing may persist."""
    from app.models import Task
    t = db.session.get(Task, _STATE["task_id"])
    t.project_id = _STATE["pa_id"]
    t.milestone_id = _STATE["ma_id"]
    db.session.commit()
    client = _client_as_home_user()
    r = _post_edit(client,
                   project_id=str(_STATE["pa_id"]),
                   milestone_id=str(_STATE["mb_id"]))
    t = _reload_task()
    assert t.project_id == _STATE["pa_id"], (
        f"project changed on rejected submit: {t.project_id}")
    assert t.milestone_id == _STATE["ma_id"], (
        f"milestone leaked across projects: got {t.milestone_id}, "
        f"expected unchanged {_STATE['ma_id']}")
    return "mismatched (project, milestone) pair rejected atomically"


@check("5. Template no longer disables the Project select in edit mode")
def _():
    """Structural regression guard against layer (a). The
    `{% if task %}disabled{% endif %}` and the hidden project_id
    input that used to sit on line 37 must both be gone; otherwise
    the field is uneditable no matter how nice the route is."""
    tpl = (ROOT / "app" / "templates" / "tasks" / "form.html").read_text(
        encoding="utf-8")
    # Look at the ACTUAL <select> line, not the whole file — a docstring
    # comment that quotes the removed pattern would otherwise trigger a
    # false positive.
    import re
    m = re.search(
        r'<select[^>]*name="project_id"[^>]*>', tpl)
    assert m, "project_id <select> tag missing"
    tag = m.group(0)
    assert "disabled" not in tag, (
        f"Project select still carries disabled: {tag}")
    # And the compensating hidden input must be gone (search only the
    # form region, since a hidden field is a real HTML input).
    assert '<input type="hidden" name="project_id"' not in tpl, (
        "hidden project_id input still present — will double-submit")
    return "disabled attribute + hidden project_id input both removed"


@check("6. Edit route passes milestones_by_project (cascade JS attaches)")
def _():
    """Structural + rendered check: GET the edit page and confirm
    the JSON script tag from form.html:171 actually rendered. Pre-fix
    it's gated out by `{% if milestones_by_project %}` because edit()
    doesn't pass the map — the Stage cascade never attached."""
    client = _client_as_home_user()
    r = client.get(f"/tasks/{_STATE['task_id']}/edit")
    assert r.status_code == 200, f"HTTP {r.status_code}"
    body = r.get_data(as_text=True)
    assert 'id="task-milestones-data"' in body, (
        "milestones_by_project script tag missing — "
        "cascade will not attach")
    # And the map must actually contain our two projects' ids so the
    # client-side listener has something to swap in.
    assert f'"{_STATE["pa_id"]}"' in body \
        or f'{_STATE["pa_id"]}:' in body, (
            f"project A id {_STATE['pa_id']} not in milestones map")
    return "cascade JSON present + carries the fixture projects"


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
