#!/usr/bin/env python3
"""MARSOUD-PROJECT-ARCHIVE (2026-08-10) — audit for the
project-archive lifecycle.

Mirrors tests/audit_task_archive.py in shape. Every check
verified to fail against pre-migration HEAD.

Locks the ticket's Acceptance Criteria:
- Any project can be archived at any status (not blocked by
  the AC-09 CLIENT_FEEDBACK → CLOSED gate on
  change_project_status).
- Archived projects vanish from /projects/ default index.
- /projects/archive/ lists them; restore returns them to the
  active index.
- Archive is orthogonal to soft-delete: a soft-deleted
  project stays hidden, an archived project stays visible on
  the archive page but hidden from the main list.
- Archive does not touch status, progress_pct, deleted_at,
  deletion_reason.
- recompute_progress freezes on archived projects.
- Permission projects.archive scoped to owner/admin/PM only;
  team_member gets 403 on the archive page.
- PM can only archive projects they personally manage (route
  enforces _user_can_edit_project on top of the perm).

Checks:
  1. Schema: archived_at + archived_by_id + index exist.
  2. archive_project sets archived_at + archived_by_id;
     unarchive clears both. Idempotent.
  3. Both actions write a ProjectStatusEvent with the
     sentinel note.
  4. recompute_progress on an archived project is a no-op.
  5. recompute_progress on a live project still works
     (regression).
  6. /projects/ default index hides archived projects.
  7. /projects/?scope=archive shows them (opt-in).
  8. /projects/<id>/archive POST as PM of the project →
     row is archived + redirect to /projects/.
  9. Same POST as team_member → 403.
  10. /projects/<id>/unarchive POST restores the row.
  11. /projects/archive/ GET as owner → 200, lists it.
  12. /projects/archive/ GET as team_member → 403.
  13. Regression: archive doesn't touch status,
      progress_pct, deleted_at, deletion_reason.
  14. Regression: PM can archive projects they manage, but
      NOT projects they don't manage (403).
"""
import sys
from datetime import datetime, date
from pathlib import Path

# Windows console defaults to cp1252 — force UTF-8 so print()
# of the Arabic labels in assertion messages doesn't blow up.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

# Boss's env carries SESSION_COOKIE_DOMAIN=.marsoud.com;
# neutralise per-app so this audit runs on every machine.
_ORIG_CREATE_APP = create_app
def create_app(*a, **kw):
    app = _ORIG_CREATE_APP(*a, **kw)
    app.config["SESSION_COOKIE_DOMAIN"] = None
    return app


CHECKS = []
PREFIX = "__PRJARCH_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import (
        Company, Plan, User, UserStatus,
        Customer, Project, ProjectStatus, Task, TaskStatus,
        TaskPriority,
    )
    from app.models.user import user_companies
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.roles import set_membership_role
    from app.services.legal import get_terms_version
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code=f"{PREFIX}plan").first()
    if not plan:
        plan = Plan(code=f"{PREFIX}plan", name="PRJARCH",
                    name_ar="PRJARCH", allowed_subitems=None)
        plan.set_modules(["sales", "crm", "settings"])
        db.session.add(plan); db.session.flush()

    # Future expiry so enforce_subscription_read_only doesn't
    # redirect non-GETs to /home. (Trial-window bypass in
    # subitem_allowed is fine — this audit doesn't care about
    # the subitem filter, only the archive lifecycle.)
    from datetime import timedelta
    co = Company(name=f"{PREFIX}CO", base_currency="SAR",
                  plan_id=plan.id,
                  subscription_started_at=datetime.utcnow(),
                  subscription_expires_at=datetime.utcnow()
                    + timedelta(days=365))
    db.session.add(co); db.session.flush()
    db.session.commit()
    ensure_roles_ready_for_company(co.id)

    def _mk_user(email, name):
        u = User(email=email, full_name=name, is_active=True,
                  status=UserStatus.ACTIVE.value,
                  email_verified_at=datetime.utcnow(),
                  terms_version=get_terms_version(),
                  terms_accepted_at=datetime.utcnow(),
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"))
        db.session.add(u); db.session.flush()
        return u

    owner = _mk_user(f"{PREFIX}owner@x.test", "prjarch owner")
    pm = _mk_user(f"{PREFIX}pm@x.test", "prjarch pm")
    other_pm = _mk_user(f"{PREFIX}other_pm@x.test",
                          "prjarch other-pm")
    tm = _mk_user(f"{PREFIX}tm@x.test", "prjarch team")

    for u, role in ((owner, "owner"), (pm, "project_manager"),
                     (other_pm, "project_manager"),
                     (tm, "team_member")):
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=co.id, role=role))
    db.session.commit()
    # Sync role_id on the memberships so require_permission +
    # has_permission find the seeded roles via _db_has_permission.
    for u, role in ((owner, "owner"), (pm, "project_manager"),
                     (other_pm, "project_manager"),
                     (tm, "team_member")):
        set_membership_role(u.id, co.id, role)

    cust = Customer(company_id=co.id, name=f"{PREFIX}cust")
    db.session.add(cust); db.session.flush()

    today = date.today()
    # PM's project. Two tasks so recompute_progress has non-zero
    # denominator; one DONE so the initial %  is > 0.
    def _mk_project(name, manager, status=ProjectStatus.IN_PROGRESS):
        p = Project(company_id=co.id, name=name,
                     customer_id=cust.id, type="INTERNAL",
                     manager_id=manager.id,
                     start_date=today, end_date=today,
                     status=status)
        db.session.add(p); db.session.flush()
        return p

    proj_pm = _mk_project(f"{PREFIX}pm_project", pm)
    proj_other = _mk_project(f"{PREFIX}other_project", other_pm)

    t1 = Task(company_id=co.id, title="t1", project_id=proj_pm.id,
              assigned_to_id=pm.id, created_by_id=pm.id,
              status=TaskStatus.DONE, priority=TaskPriority.MEDIUM)
    t2 = Task(company_id=co.id, title="t2", project_id=proj_pm.id,
              assigned_to_id=pm.id, created_by_id=pm.id,
              status=TaskStatus.TODO, priority=TaskPriority.MEDIUM)
    db.session.add_all([t1, t2]); db.session.commit()
    proj_pm.recompute_progress()
    db.session.commit()

    _STATE.update(
        co_id=co.id, owner_id=owner.id, pm_id=pm.id,
        other_pm_id=other_pm.id, tm_id=tm.id,
        proj_pm_id=proj_pm.id, proj_other_id=proj_other.id,
        plan_id=plan.id,
    )


def _teardown():
    from app.models import Company, User, Plan
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id=:c"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id=:c"), {"c": cid})
        db.session.commit()
    for p in Plan.query.filter(Plan.code.like(f"{PREFIX}%")).all():
        db.session.delete(p)
    db.session.commit()
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _reset_g():
    from flask import g
    for key in ("_login_user", "active_company", "user_companies",
                "impersonating"):
        try:
            g.pop(key, None)
        except Exception:
            pass


def _client_as(user_id):
    from flask import current_app
    _reset_g()
    db.session.expire_all()
    db.session.remove()
    c = current_app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["co_id"]
    return c


def _reload_project(pid):
    from app.models import Project
    db.session.expire_all()
    return db.session.get(Project, pid)


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. Schema: projects.archived_at + archived_by_id + index")
def _():
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    cols = {c["name"] for c in insp.get_columns("projects")}
    assert "archived_at" in cols, "archived_at missing"
    assert "archived_by_id" in cols, "archived_by_id missing"
    idxs = insp.get_indexes("projects")
    hit = any("archived_at" in (ix.get("column_names") or [])
              for ix in idxs)
    assert hit, "ix on archived_at missing"
    return "both cols + index present"


@check("2. archive_project sets flag; unarchive clears; idempotent")
def _():
    from app.services.project_archive import (
        archive_project, unarchive_project,
    )
    p = _reload_project(_STATE["proj_pm_id"])
    assert p.archived_at is None
    ok = archive_project(p, actor_id=_STATE["owner_id"])
    assert ok is True
    p = _reload_project(_STATE["proj_pm_id"])
    assert p.archived_at is not None
    assert p.archived_by_id == _STATE["owner_id"]
    # Idempotent — second call is a no-op returning False.
    ok2 = archive_project(p, actor_id=_STATE["owner_id"])
    assert ok2 is False
    # Restore.
    ok3 = unarchive_project(p, actor_id=_STATE["owner_id"])
    assert ok3 is True
    p = _reload_project(_STATE["proj_pm_id"])
    assert p.archived_at is None
    assert p.archived_by_id is None
    return "archive → set; unarchive → clear; idempotent both sides"


@check("3. Both actions write ProjectStatusEvent with sentinel note")
def _():
    from app.models import ProjectStatusEvent
    from app.services.project_archive import (
        archive_project, unarchive_project,
    )
    p = _reload_project(_STATE["proj_pm_id"])
    archive_project(p, actor_id=_STATE["owner_id"])
    unarchive_project(p, actor_id=_STATE["owner_id"])
    events = ProjectStatusEvent.query.filter_by(
        project_id=_STATE["proj_pm_id"]
    ).order_by(ProjectStatusEvent.id.asc()).all()
    notes = [e.note for e in events]
    assert "__ARCHIVED__" in notes, f"ARCHIVED event missing: {notes}"
    assert "__UNARCHIVED__" in notes, (
        f"UNARCHIVED event missing: {notes}")
    return f"timeline has {len(events)} events incl. sentinels"


@check("4. recompute_progress is a no-op on archived projects")
def _():
    """Freeze the value once archived so the bar doesn't drift."""
    from app.services.project_archive import (
        archive_project, unarchive_project,
    )
    from app.models import Task, TaskStatus
    from decimal import Decimal
    p = _reload_project(_STATE["proj_pm_id"])
    # Set a stale value + archive; then finish another task and
    # recompute → progress_pct must NOT move.
    p.progress_pct = Decimal("42.00")
    db.session.commit()
    archive_project(p, actor_id=_STATE["owner_id"])
    # Add one more task + mark it DONE — under a live project this
    # would push the % up.
    t3 = Task(company_id=_STATE["co_id"], title="t3-frozen",
              project_id=_STATE["proj_pm_id"],
              assigned_to_id=_STATE["pm_id"],
              created_by_id=_STATE["pm_id"],
              status=TaskStatus.DONE)
    db.session.add(t3); db.session.commit()
    p = _reload_project(_STATE["proj_pm_id"])
    p.recompute_progress()
    db.session.commit()
    p = _reload_project(_STATE["proj_pm_id"])
    assert p.progress_pct == Decimal("42.00"), (
        f"archived project drifted: {p.progress_pct}")
    # Cleanup.
    unarchive_project(p, actor_id=_STATE["owner_id"])
    return f"frozen at 42.00% while archived"


@check("5. recompute_progress on a live project still works")
def _():
    """Regression — the archive guard mustn't leak onto live
    projects."""
    from decimal import Decimal
    p = _reload_project(_STATE["proj_pm_id"])
    p.progress_pct = Decimal("0.00")
    db.session.commit()
    p = _reload_project(_STATE["proj_pm_id"])
    p.recompute_progress()
    db.session.commit()
    p = _reload_project(_STATE["proj_pm_id"])
    # 2 out of 3 tasks are DONE (t1 + t3) after check 4 added t3.
    # 2/3 = 66.67
    assert p.progress_pct > Decimal("0.00"), (
        f"live project didn't recompute: {p.progress_pct}")
    return f"live recompute → {p.progress_pct}%"


@check("6. /projects/ default index hides archived projects")
def _():
    from app.services.project_archive import archive_project
    p = _reload_project(_STATE["proj_pm_id"])
    archive_project(p, actor_id=_STATE["owner_id"])
    c = _client_as(_STATE["owner_id"])
    r = c.get("/projects/")
    assert r.status_code == 200, f"HTTP {r.status_code}"
    body = r.get_data(as_text=True)
    assert f"{PREFIX}pm_project" not in body, (
        "archived project leaked into default index")
    assert f"{PREFIX}other_project" in body, (
        "non-archived project vanished — filter over-reached")
    return "archived hidden; live visible"


@check("7. /projects/?scope=archive opts back in")
def _():
    c = _client_as(_STATE["owner_id"])
    r = c.get("/projects/?scope=archive")
    assert r.status_code == 200, f"HTTP {r.status_code}"
    body = r.get_data(as_text=True)
    assert f"{PREFIX}pm_project" in body, (
        "scope=archive didn't surface the archived project")
    return "scope=archive shows archived rows"


@check("8. POST /projects/<id>/archive as PM → row archived + redirect")
def _():
    """First unarchive from check 6, then archive via HTTP as
    PM."""
    from app.services.project_archive import unarchive_project
    p = _reload_project(_STATE["proj_pm_id"])
    unarchive_project(p, actor_id=_STATE["owner_id"])
    c = _client_as(_STATE["pm_id"])
    r = c.post(f"/projects/{_STATE['proj_pm_id']}/archive",
                follow_redirects=False)
    assert r.status_code in (200, 302), f"HTTP {r.status_code}"
    p = _reload_project(_STATE["proj_pm_id"])
    assert p.archived_at is not None, "POST didn't archive"
    return "PM archived their own project via HTTP"


@check("9. POST /projects/<id>/archive as team_member refused")
def _():
    """require_permission redirects (302) with a flash rather
    than aborting 403 — audit_portal_403.py E6 locks this
    convention. The load-bearing assertion is that the row
    stayed non-archived, not the specific status code."""
    from app.services.project_archive import unarchive_project
    p = _reload_project(_STATE["proj_pm_id"])
    unarchive_project(p, actor_id=_STATE["owner_id"])
    c = _client_as(_STATE["tm_id"])
    r = c.post(f"/projects/{_STATE['proj_pm_id']}/archive",
                follow_redirects=False)
    p = _reload_project(_STATE["proj_pm_id"])
    assert p.archived_at is None, (
        f"team_member archived a project (HTTP {r.status_code})")
    assert r.status_code in (302, 303, 403, 404), (
        f"unexpected status: {r.status_code}")
    return f"team_member refused ({r.status_code}); row untouched"


@check("10. POST /projects/<id>/unarchive as PM restores")
def _():
    from app.services.project_archive import archive_project
    p = _reload_project(_STATE["proj_pm_id"])
    archive_project(p, actor_id=_STATE["owner_id"])
    c = _client_as(_STATE["pm_id"])
    r = c.post(f"/projects/{_STATE['proj_pm_id']}/unarchive",
                follow_redirects=False)
    assert r.status_code in (200, 302), f"HTTP {r.status_code}"
    p = _reload_project(_STATE["proj_pm_id"])
    assert p.archived_at is None, "POST didn't unarchive"
    return "PM unarchived via HTTP"


@check("11. GET /projects/archive/ as owner → 200, lists it")
def _():
    from app.services.project_archive import archive_project
    p = _reload_project(_STATE["proj_pm_id"])
    archive_project(p, actor_id=_STATE["owner_id"])
    c = _client_as(_STATE["owner_id"])
    r = c.get("/projects/archive/")
    assert r.status_code == 200, f"HTTP {r.status_code}"
    body = r.get_data(as_text=True)
    assert f"{PREFIX}pm_project" in body, (
        "archive page didn't list the archived project")
    return "archive page renders + lists row"


@check("12. GET /projects/archive/ as team_member refused")
def _():
    """Same @require_permission redirect convention as check 9.
    Either 302 (permission-flash redirect) or 403 (some other
    guard) is a valid refusal — what matters is that the page
    body isn't served."""
    c = _client_as(_STATE["tm_id"])
    r = c.get("/projects/archive/", follow_redirects=False)
    assert r.status_code in (302, 303, 403, 404), (
        f"unexpected status: {r.status_code}")
    if r.status_code == 200:
        body = r.get_data(as_text=True)
        assert "أرشيف المشاريع" not in body, (
            "archive page body reached team_member")
    return f"team_member refused ({r.status_code})"


@check("13. Archive is orthogonal to status/progress/soft-delete")
def _():
    """Regression — the archive column mustn't fiddle with the
    project's other fields."""
    from decimal import Decimal
    from app.services.project_archive import (
        archive_project, unarchive_project,
    )
    p = _reload_project(_STATE["proj_pm_id"])
    # Ensure it's currently archived from check 11.
    if p.archived_at is None:
        archive_project(p, actor_id=_STATE["owner_id"])
        p = _reload_project(_STATE["proj_pm_id"])
    status_before = p.status
    progress_before = p.progress_pct
    deleted_before = p.deleted_at
    reason_before = p.deletion_reason
    # Round-trip.
    unarchive_project(p, actor_id=_STATE["owner_id"])
    archive_project(p, actor_id=_STATE["owner_id"])
    p = _reload_project(_STATE["proj_pm_id"])
    assert p.status == status_before, "status drifted"
    assert p.progress_pct == progress_before, "progress drifted"
    assert p.deleted_at == deleted_before, "deleted_at drifted"
    assert p.deletion_reason == reason_before, (
        "deletion_reason drifted")
    return "status/progress/soft-delete all untouched"


@check("14. PM can't archive projects they don't manage")
def _():
    from app.services.project_archive import unarchive_project
    # Ensure other_project starts non-archived.
    p_other = _reload_project(_STATE["proj_other_id"])
    if p_other.archived_at is not None:
        unarchive_project(p_other, actor_id=_STATE["owner_id"])
    c = _client_as(_STATE["pm_id"])
    r = c.post(f"/projects/{_STATE['proj_other_id']}/archive",
                follow_redirects=False)
    p_other = _reload_project(_STATE["proj_other_id"])
    assert p_other.archived_at is None, (
        f"PM archived someone else's project (HTTP {r.status_code})")
    # 403 (edit-gate refused) or 404 (PM can't even see it).
    assert r.status_code in (403, 404), (
        f"expected 403/404, got {r.status_code}")
    return f"cross-manager archive refused ({r.status_code})"


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
