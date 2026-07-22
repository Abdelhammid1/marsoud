#!/usr/bin/env python3
"""MARSOUD-PM-TASKS-VIS + MARSOUD-NOTIF-FILTER (Abdelhamid 2026-07-22).

Two small fixes bundled as Ticket L:

  1. **PM sees all project tasks.** Previously, if a user was set as the
     manager (Project.manager_id) of a project but their global role
     in the company wasn't "project_manager", they only saw tasks
     assigned to them — missing tasks that other team members were
     working on in the project they manage. The gate at
     app/routes/tasks.py:67 was `if _role() == "project_manager"`,
     which excluded team_member users who had been promoted to
     manage a specific project.

  2. **Notifications page filter.** The /notifications/ page showed a
     flat list; no way to narrow to unread. Now accepts
     ?filter=unread + shows two buttons at the top.

Checks:
  1. A team_member who is manager of project P sees ALL P's tasks
     on /tasks/?project_id=<P.id>, not just tasks assigned to them.
  2. A team_member who is NOT PM of any project keeps existing
     behavior (only their own + created).
  3. An owner still sees everything (regression check).
  4. GET /notifications/ (no query) → all notifications.
  5. GET /notifications/?filter=unread → only unread.
  6. GET /notifications/?filter=all → all (explicit).
  7. GET /notifications/?filter=bogus → treated as "all" (safe URL).
"""
import os
import sys
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


def _wipe(name):
    from app.models import Company
    from sqlalchemy import text, inspect
    c = Company.query.filter_by(name=name).first()
    if not c:
        return
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"),
                     {"c": c.id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": c.id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": c.id})


def _setup():
    from app.models import (
        Company, User, Project, Task, TaskStatus, TaskPriority,
        Notification, Customer,
    )
    from app.models.user import user_companies
    from app.services.roles_seed import (
        seed_permissions_catalog, seed_system_roles_for_company,
    )
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    from sqlalchemy import text
    from datetime import datetime

    seed_permissions_catalog()
    _wipe("__PM_TASKS__")
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'pmvis-%@x.test'"))

    c = Company(name="__PM_TASKS__", base_currency="EGP")
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    seed_system_roles_for_company(c.id)
    # MARSOUD-CHOOSE-PLAN — pretend the owner already picked a plan.
    from app.models import Plan
    p = Plan.query.filter_by(code="enterprise").first() \
        or Plan.query.filter_by(is_active=True).first()
    if p:
        c.plan_id = p.id
        c.intended_plan_id = p.id
    from datetime import datetime as _dt, timedelta as _td
    c.subscription_expires_at = _dt.utcnow() + _td(days=30)
    db.session.flush()

    def _mk(email, role):
        u = User(email=email,
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name=email.split("@")[0])
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=c.id, role=role))
        return u
    owner   = _mk("pmvis-owner@x.test", "owner")
    pm_user = _mk("pmvis-pm@x.test",    "team_member")   # NOT project_manager role
    reg     = _mk("pmvis-reg@x.test",   "team_member")
    stranger = _mk("pmvis-str@x.test",  "team_member")   # unrelated

    # Project managed by pm_user (via manager_id). pm_user's ROLE in
    # the company is still "team_member" — this is the whole point.
    cust = Customer(company_id=c.id, name="عميل تجريبي")
    db.session.add(cust); db.session.flush()
    from datetime import date as _date, timedelta as _td
    _today = _date.today()
    proj = Project(company_id=c.id, name="مشروع PM",
                   number="PRJ-0001",
                   customer_id=cust.id, type="TEST",
                   manager_id=pm_user.id, status="PLANNING",
                   start_date=_today,
                   end_date=_today + _td(days=30))
    db.session.add(proj); db.session.flush()

    # 3 tasks in project: one assigned to reg, one to stranger, one
    # to pm_user themselves. Only 1/3 is normally visible to pm_user
    # (the one where they're the assignee) — the PM fix should surface
    # all 3.
    for name, aid in (("T1", reg.id), ("T2", stranger.id),
                      ("T3", pm_user.id)):
        db.session.add(Task(
            company_id=c.id, project_id=proj.id,
            title=name, status=TaskStatus.TODO,
            priority=TaskPriority.MEDIUM,
            assigned_to_id=aid,
            created_by_id=owner.id,
            created_at=datetime.utcnow(),
        ))
    db.session.flush()

    # Notifications for pm_user: 2 unread + 1 read.
    from datetime import datetime as _dt
    for title, is_read in (("N1", False), ("N2", False),
                            ("N3", True)):
        db.session.add(Notification(
            company_id=c.id, user_id=pm_user.id,
            kind="TASK_ASSIGNED", title=title,
            read_at=_dt.utcnow() if is_read else None,
        ))

    db.session.commit()

    _STATE.update(
        cid=c.id, project_id=proj.id,
        owner_id=owner.id, pm_id=pm_user.id,
        reg_id=reg.id, stranger_id=stranger.id,
    )


def _login(user_id, cid):
    from flask import current_app, g
    for k in ("_login_user", "active_company"):
        try: g.pop(k, None)
        except Exception: pass
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["active_company_id"] = cid
    return client


@check("1. PM (team_member role, but manager of project P) sees ALL "
       "P's tasks on /tasks/?project_id=P")
def _():
    c = _login(_STATE["pm_id"], _STATE["cid"])
    r = c.get(f"/tasks/?project_id={_STATE['project_id']}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # All 3 task titles should render.
    for name in ("T1", "T2", "T3"):
        assert name in html, f"{name} missing from PM view"
    return "T1 + T2 + T3 all visible"


@check("2. Non-PM team_member sees only their own + created tasks "
       "(no regression)")
def _():
    c = _login(_STATE["reg_id"], _STATE["cid"])
    r = c.get(f"/tasks/?project_id={_STATE['project_id']}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # reg is the assignee of T1 only.
    assert "T1" in html, "T1 (assigned to reg) should be visible"
    assert "T2" not in html and "T3" not in html, \
        f"non-PM should NOT see T2/T3; got:\n{html[html.find('T'):html.find('T')+200]}"
    return "T1 only, T2/T3 hidden"


@check("3. Owner still sees everything (regression)")
def _():
    c = _login(_STATE["owner_id"], _STATE["cid"])
    r = c.get(f"/tasks/?project_id={_STATE['project_id']}&scope=all")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for name in ("T1", "T2", "T3"):
        assert name in html, f"{name} missing from owner view"
    return "T1 + T2 + T3 visible to owner"


@check("4. /notifications/ (no filter) → all notifications")
def _():
    c = _login(_STATE["pm_id"], _STATE["cid"])
    r = c.get("/notifications/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for name in ("N1", "N2", "N3"):
        assert name in html, f"{name} missing from full list"
    return "N1 + N2 + N3 all visible"


@check("5. /notifications/?filter=unread → only unread")
def _():
    c = _login(_STATE["pm_id"], _STATE["cid"])
    r = c.get("/notifications/?filter=unread")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "N1" in html and "N2" in html, "unread should include N1+N2"
    assert "N3" not in html, "N3 was read — should be hidden"
    return "N1 + N2 shown, N3 hidden"


@check("6. /notifications/?filter=all → all (explicit)")
def _():
    c = _login(_STATE["pm_id"], _STATE["cid"])
    r = c.get("/notifications/?filter=all")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for name in ("N1", "N2", "N3"):
        assert name in html
    return "explicit all works"


@check("7. /notifications/?filter=bogus → falls back to all (safe URL)")
def _():
    c = _login(_STATE["pm_id"], _STATE["cid"])
    r = c.get("/notifications/?filter=zzz-not-a-real-filter")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "N1" in html and "N3" in html, \
        "unknown filter should show everything, not crash"
    return "bogus filter safe"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
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
            _wipe("__PM_TASKS__")
            print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
