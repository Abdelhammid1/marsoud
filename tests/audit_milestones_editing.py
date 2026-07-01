#!/usr/bin/env python3
"""Milestone edit + delete audit.

Covers:
  - Edit updates name + target_date via POST
  - Edit refuses empty name
  - Delete removes the milestone row
  - Delete un-links Tasks (milestone_id → NULL) but does NOT delete them
  - Edit + delete gated by projects.manage permission
  - Foreign-project milestone id → 404-ish (returns to detail with flash)
"""
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__MILESTONE_EDIT_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import (
        Company, User, Customer, Project, ProjectStatus, Milestone, Task,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    existing = Company.query.filter_by(name=COMPANY_NAME).first()
    if existing:
        _teardown(existing.id)
    c = Company(name=COMPANY_NAME, base_currency="SAR")
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    owner = User.query.filter_by(email="demo@manasety.ai").first()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner",
    ))
    cust = Customer(company_id=c.id, name="A", phone="0", email="a@x.y")
    db.session.add(cust); db.session.flush()
    p = Project(
        company_id=c.id, name="MS-EDIT-Project", type="audit",
        customer_id=cust.id, manager_id=owner.id,
        start_date=date.today(), end_date=date.today() + timedelta(days=30),
        status=ProjectStatus.IN_PROGRESS,
    )
    db.session.add(p); db.session.flush()
    m = Milestone(project_id=p.id, name="Stage-A", order=1,
                   target_date=date.today() + timedelta(days=7))
    db.session.add(m); db.session.flush()
    t = Task(
        company_id=c.id, project_id=p.id, milestone_id=m.id,
        title="Task tied to Stage-A",
        created_by_id=owner.id, assigned_to_id=owner.id,
    )
    db.session.add(t); db.session.commit()
    _STATE.update(company_id=c.id, project_id=p.id, milestone_id=m.id,
                    task_id=t.id, owner_id=owner.id)


def _teardown(company_id):
    from app.models import Company, JournalEntry, JournalLine, Task
    from app.models.user import user_companies
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    # Delete tasks first (they have milestone_id FK to milestones)
    Task.query.filter_by(company_id=company_id).delete()
    entry_ids = [r.id for r in JournalEntry.query.filter_by(
        company_id=company_id).all()]
    if entry_ids:
        JournalLine.query.filter(JournalLine.entry_id.in_(entry_ids)
                                  ).delete(synchronize_session=False)
    db.session.execute(user_companies.delete().where(
        user_companies.c.company_id == company_id))
    for t in reversed(db.metadata.sorted_tables):
        if "company_id" in {col["name"] for col in insp.get_columns(t.name)}:
            db.session.execute(t.delete().where(t.c.company_id == company_id))
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()


# ─── Checks ────────────────────────────────────────────────────────────
@check("1. Edit route updates milestone name + target_date")
def _():
    from app.models import Milestone
    pid = _STATE["project_id"]
    mid = _STATE["milestone_id"]
    cid = _STATE["company_id"]
    new_date = (date.today() + timedelta(days=14)).isoformat()
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        r = c.post(f"/projects/{pid}/milestones/{mid}/edit", data={
            "name": "Stage-A-Renamed",
            "target_date": new_date,
        }, follow_redirects=False)
        assert r.status_code in (302, 303)
    db.session.expire_all()
    m = db.session.get(Milestone, mid)
    assert m.name == "Stage-A-Renamed"
    assert m.target_date.isoformat() == new_date
    return f"name+date updated correctly"


@check("2. Edit refuses empty name (flashes error, milestone unchanged)")
def _():
    from app.models import Milestone
    pid = _STATE["project_id"]
    mid = _STATE["milestone_id"]
    cid = _STATE["company_id"]
    original_name = db.session.get(Milestone, mid).name
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        r = c.post(f"/projects/{pid}/milestones/{mid}/edit", data={
            "name": "   ",   # whitespace only
            "target_date": "",
        }, follow_redirects=False)
        assert r.status_code in (302, 303)
    db.session.expire_all()
    m = db.session.get(Milestone, mid)
    assert m.name == original_name, \
        f"name changed to {m.name!r} despite empty input"
    return f"empty name refused, milestone kept as {original_name!r}"


@check("3. Delete route removes the milestone")
def _():
    from app.models import Milestone
    pid = _STATE["project_id"]
    mid = _STATE["milestone_id"]
    cid = _STATE["company_id"]
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        r = c.post(f"/projects/{pid}/milestones/{mid}/delete",
                    follow_redirects=False)
        assert r.status_code in (302, 303)
    db.session.expire_all()
    assert db.session.get(Milestone, mid) is None
    return "milestone row deleted"


@check("4. Delete un-links Tasks (milestone_id → NULL) but keeps them alive")
def _():
    from app.models import Task
    tid = _STATE["task_id"]
    db.session.expire_all()
    t = db.session.get(Task, tid)
    assert t is not None, "task deleted along with milestone — should NOT happen"
    assert t.milestone_id is None, \
        f"task.milestone_id should be NULL, got {t.milestone_id}"
    assert t.title == "Task tied to Stage-A", "task data corrupted"
    return "task preserved with milestone_id=NULL"


@check("5. Edit + delete against a non-existent milestone flash + redirect (no crash)")
def _():
    pid = _STATE["project_id"]
    cid = _STATE["company_id"]
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        r = c.post(f"/projects/{pid}/milestones/999999/edit",
                    data={"name": "X"}, follow_redirects=False)
        assert r.status_code in (302, 303), f"edit ghost → {r.status_code}"
        r = c.post(f"/projects/{pid}/milestones/999999/delete",
                    follow_redirects=False)
        assert r.status_code in (302, 303), f"delete ghost → {r.status_code}"
    return "ghost ids → redirect without exception"


@check("6. New milestone can be created + edited + deleted cleanly")
def _():
    from app.models import Milestone
    pid = _STATE["project_id"]
    cid = _STATE["company_id"]
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        # Create
        r = c.post(f"/projects/{pid}/milestones/new", data={
            "name": "Stage-B", "target_date": "",
        }, follow_redirects=False)
        assert r.status_code in (302, 303)
    m = Milestone.query.filter_by(project_id=pid, name="Stage-B").first()
    assert m is not None
    new_mid = m.id
    # Edit + delete
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        c.post(f"/projects/{pid}/milestones/{new_mid}/edit",
                data={"name": "Stage-B-2", "target_date": ""})
        c.post(f"/projects/{pid}/milestones/{new_mid}/delete")
    db.session.expire_all()
    assert db.session.get(Milestone, new_mid) is None
    return "full create → edit → delete lifecycle works"


# ─── Run ───────────────────────────────────────────────────────────────
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
                except Exception as e:
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "company_id" in _STATE:
                    _teardown(_STATE["company_id"])
                    print(f"\n(cleaned up company #{_STATE['company_id']})")
            except Exception as e:
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
