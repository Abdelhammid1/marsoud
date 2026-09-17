#!/usr/bin/env python3
"""MARSOUD-MOBILE-TASK-CREATE-01 (2026-09-17) — POST /api/v1/tasks
+ GET /api/v1/company/users, backing the mobile "مهمة جديدة"
screen shipped in 1.0.7.

Checks:
  1. GET /company/users lists the tenant's active users;
     current user is first + `is_me:true`.
  2. POST /tasks with a valid body creates the task + wires the
     assignees + returns 201 + task_id.
  3. Refuses without title.
  4. Refuses without assignee_ids.
  5. Refuses a cross-tenant assignee (id belongs to another
     company).
  6. Refuses a cross-tenant project_id.
  7. Milestone requires project_id.
  8. Bad priority string → 400 with a clear message.
  9. Bad deadline (not iso YYYY-MM-DD) → 400.
"""
import os
import sys
from pathlib import Path
from datetime import datetime, date

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _boot(prefix):
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from app.services.api_tokens import generate_token

    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        "SELECT id FROM companies WHERE name LIKE :p"),
        {"p": f"__{prefix}__%"})]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"),
                    {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        "DELETE FROM users WHERE email LIKE :p"),
        {"p": f"%__{prefix.lower()}__%"})
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C",
                    allowed_subitems=None)
        db.session.add(plan)
    plan.set_modules(["accounting", "sales", "purchases", "hr", "reports"])
    db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency="EGP",
                subdomain=prefix.lower(), plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.commit()
    seed_default_coa(c.id); db.session.commit()

    from app.services.legal import get_terms_version
    tv = get_terms_version() or "audit"
    owner = User(email=f"owner__{prefix.lower()}__@x.io",
                 full_name=f"Owner {prefix}", is_active=True,
                 email_verified_at=datetime.utcnow(),
                 terms_version=tv, terms_accepted_at=datetime.utcnow())
    owner.set_password("pw12345678")
    db.session.add(owner); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner"))
    db.session.commit()

    raw, _tok = generate_token(owner, f"test:{prefix}")
    return c.id, owner.id, raw


def _add_second_user(cid, prefix):
    from app import db
    from app.models import User
    from app.models.user import user_companies
    from app.services.legal import get_terms_version
    tv = get_terms_version() or "audit"
    u = User(email=f"buddy__{prefix.lower()}__@x.io",
             full_name=f"Buddy {prefix}", is_active=True,
             email_verified_at=datetime.utcnow(),
             terms_version=tv,
             terms_accepted_at=datetime.utcnow())
    u.set_password("pw12345678")
    db.session.add(u); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=cid, role="team_member"))
    db.session.commit()
    return u.id


@check("1. GET /api/v1/company/users lists active users, me first")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        cid, oid, tok = _boot("TC1")
        _add_second_user(cid, "TC1")
        client = app.test_client()
        r = client.get(
            f"/api/v1/company/users?company_id={cid}",
            headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, r.status_code
        data = r.get_json()
        users = data["users"]
        assert len(users) == 2
        assert users[0]["is_me"] is True
        assert users[0]["id"] == oid
        return f"2 users listed, me first (id={oid})"


@check("2. POST /api/v1/tasks with valid body → 201 + task_id, "
        "assignee wired")
def _():
    from app import create_app, db
    from app.models import Task
    app = create_app()
    with app.app_context():
        cid, oid, tok = _boot("TC2")
        mid = _add_second_user(cid, "TC2")
        client = app.test_client()
        r = client.post(
            f"/api/v1/tasks?company_id={cid}",
            headers={"Authorization": f"Bearer {tok}"},
            json={
                "title": "Test task",
                "assignee_ids": [oid, mid],
                "description": "some content",
                "priority": "HIGH",
                "deadline": (date.today()).isoformat(),
            })
        assert r.status_code == 201, (r.status_code, r.get_data()[:400])
        data = r.get_json()
        assert data["ok"] is True
        tid = data["task_id"]
        t = db.session.get(Task, tid)
        assert t is not None
        assert t.title == "Test task"
        # Primary assignee is the first id.
        assert t.assigned_to_id == oid
        return f"task #{tid} created + {t.title!r}"


@check("3. POST /tasks without title → 400 title_required")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        cid, oid, tok = _boot("TC3")
        client = app.test_client()
        r = client.post(
            f"/api/v1/tasks?company_id={cid}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"assignee_ids": [oid]})
        assert r.status_code == 400
        body = r.get_json()
        assert "title" in (body.get("error") or "").lower()
        return "empty title refused"


@check("4. POST /tasks without assignee_ids → 400")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        cid, oid, tok = _boot("TC4")
        client = app.test_client()
        r = client.post(
            f"/api/v1/tasks?company_id={cid}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"title": "X"})
        assert r.status_code == 400
        body = r.get_json()
        assert "assignee" in (body.get("error") or "").lower()
        return "empty assignees refused"


@check("5. POST /tasks with a cross-tenant assignee id → 400")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        cid, oid, tok = _boot("TC5")
        # Second tenant + user; foreign to the caller.
        cid2, _oid2, _tok2 = _boot("TC5B")
        foreign_uid = _add_second_user(cid2, "TC5B")
        client = app.test_client()
        r = client.post(
            f"/api/v1/tasks?company_id={cid}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"title": "X", "assignee_ids": [foreign_uid]})
        assert r.status_code == 400
        body = r.get_json()
        assert "assignee" in (body.get("error") or "").lower()
        return "cross-tenant assignee refused"


@check("6. POST /tasks with a cross-tenant project_id → 404")
def _():
    from app import create_app, db
    from app.models import Project, Customer
    app = create_app()
    with app.app_context():
        cid, oid, tok = _boot("TC6")
        # Second tenant + a project inside it.
        cid2, oid2, _tok2 = _boot("TC6B")
        c2 = Customer(company_id=cid2, name="X")
        db.session.add(c2); db.session.flush()
        p = Project(
            company_id=cid2, name="Foreign",
            customer_id=c2.id, type="internal",
            manager_id=oid2, start_date=date.today(),
            end_date=date.today())
        db.session.add(p); db.session.commit()
        client = app.test_client()
        r = client.post(
            f"/api/v1/tasks?company_id={cid}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"title": "X",
                  "assignee_ids": [oid],
                  "project_id": p.id})
        assert r.status_code == 404
        return "cross-tenant project refused"


@check("7. POST /tasks with milestone_id but no project_id → 400")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        cid, oid, tok = _boot("TC7")
        client = app.test_client()
        r = client.post(
            f"/api/v1/tasks?company_id={cid}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"title": "X", "assignee_ids": [oid],
                  "milestone_id": 1})
        assert r.status_code == 400
        return "milestone-without-project refused"


@check("8. POST /tasks with unknown priority → 400")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        cid, oid, tok = _boot("TC8")
        client = app.test_client()
        r = client.post(
            f"/api/v1/tasks?company_id={cid}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"title": "X", "assignee_ids": [oid],
                  "priority": "EXTREME"})
        assert r.status_code == 400
        body = r.get_json()
        assert "priority" in (body.get("error") or "").lower()
        return "bad priority refused"


@check("9. POST /tasks with bad deadline → 400")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        cid, oid, tok = _boot("TC9")
        client = app.test_client()
        r = client.post(
            f"/api/v1/tasks?company_id={cid}",
            headers={"Authorization": f"Bearer {tok}"},
            json={"title": "X", "assignee_ids": [oid],
                  "deadline": "not-a-date"})
        assert r.status_code == 400
        return "bad deadline refused"


def main():
    from app import create_app
    _ = create_app()
    passed = failed = 0
    for label, fn in CHECKS:
        try:
            res = fn()
            print(f"PASS  {label}  ⇒ {res}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
            failed += 1
            import traceback; traceback.print_exc()
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
