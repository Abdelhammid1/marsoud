#!/usr/bin/env python3
"""MARSOUD-TASK-PARENT-BY-PROJECT-01 (2026-09-17) — filter the
Parent Task dropdown on /tasks/new by the selected Project.

The task ticket's Acceptance Criteria mapped to checks:

  1. `parents_by_project_grouped` returns a dict keyed by
     project_id (as str) plus a "none" bucket for orphan tasks.
     ORDER inside every bucket is `created_at DESC`.
  2. Project A tasks land in bucket "A", Project B tasks in
     bucket "B", orphans in "none" — with no cross-leak.
  3. `available_parents_for` sort was changed from `Task.title`
     alpha to `Task.created_at DESC`.
  4. `GET /tasks/new` renders 200 + ships a `<script
     id="task-parents-data">` block with the JSON payload the
     JS in form.html reads. Just presence + shape — the JS
     itself needs a browser to exercise, so we assert the
     server contract rather than the DOM behaviour.
  5. `GET /tasks/new?project_id=X` pre-populates the initial
     `parent_choices` list with ONLY that project's tasks (not
     the full flat list) — matches what the JS's first paint
     would produce.
"""
import os
import sys
from pathlib import Path
from datetime import datetime, date, timedelta

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
    """Fresh company + owner + two projects (A, B) + tasks:
       · 2 tasks in project A
       · 2 tasks in project B
       · 1 orphan task (no project)
    Returns (cid, oid, pA.id, pB.id, task_ids dict)."""
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan, Project, Task
    from app.models.crm import TaskStatus, TaskPriority
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

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

    try:
        from app.services.legal import get_terms_version
        tv = get_terms_version() or "audit"
    except Exception:
        tv = "audit"
    owner = User(email=f"owner__{prefix.lower()}__@x.io",
                 full_name=f"Owner {prefix}", is_active=True,
                 email_verified_at=datetime.utcnow(),
                 terms_version=tv, terms_accepted_at=datetime.utcnow())
    owner.set_password("pw12345678")
    db.session.add(owner); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner"))
    db.session.commit()

    # Two projects — both need a Customer FK to satisfy NOT NULL.
    from app.models import Customer
    cust = Customer(company_id=c.id, name="Test customer")
    db.session.add(cust); db.session.flush()
    _end = date.today() + timedelta(days=90)
    pA = Project(company_id=c.id, name="Project A",
                  customer_id=cust.id, type="internal",
                  manager_id=owner.id,
                  start_date=date.today(), end_date=_end)
    pB = Project(company_id=c.id, name="Project B",
                  customer_id=cust.id, type="internal",
                  manager_id=owner.id,
                  start_date=date.today(), end_date=_end)
    db.session.add_all([pA, pB]); db.session.commit()

    # Tasks with distinct created_at so DESC ordering is testable.
    ids = {}
    now = datetime.utcnow()
    for i, (tag, project_id) in enumerate([
        ("A1", pA.id),   # older
        ("A2", pA.id),   # newer
        ("B1", pB.id),
        ("B2", pB.id),
        ("ORPH", None),
    ]):
        t = Task(
            company_id=c.id,
            title=f"Task {tag}",
            project_id=project_id,
            assigned_to_id=owner.id,
            created_by_id=owner.id,
            priority=TaskPriority.MEDIUM,
            status=TaskStatus.TODO,
            # 5 seconds apart — deterministic DESC order.
            created_at=now + timedelta(seconds=i * 5),
        )
        db.session.add(t); db.session.flush()
        ids[tag] = t.id
    db.session.commit()
    return c.id, owner.id, pA.id, pB.id, ids


@check("1. parents_by_project_grouped shape — dict with str project "
        "keys + a 'none' bucket for orphan tasks")
def _():
    from app import create_app
    from app.services.task_hierarchy import parents_by_project_grouped
    app = create_app()
    with app.app_context():
        cid, oid, pa, pb, ids = _boot("TPP1")
        m = parents_by_project_grouped(
            cid, oid, full_visibility=True)
        assert isinstance(m, dict)
        assert "none" in m, "no 'none' bucket for orphan tasks"
        assert str(pa) in m, "no bucket for project A"
        assert str(pb) in m, "no bucket for project B"
        # Every entry has id + title + project_id keys.
        sample = m[str(pa)][0]
        assert set(sample.keys()) >= {"id", "title", "project_id"}
        return f"buckets: {sorted(m.keys())}"


@check("2. Project A bucket has ONLY project A tasks; same for B "
        "and 'none' — no cross-leak")
def _():
    from app import create_app
    from app.services.task_hierarchy import parents_by_project_grouped
    app = create_app()
    with app.app_context():
        cid, oid, pa, pb, ids = _boot("TPP2")
        m = parents_by_project_grouped(
            cid, oid, full_visibility=True)
        a_ids = {t["id"] for t in m[str(pa)]}
        b_ids = {t["id"] for t in m[str(pb)]}
        n_ids = {t["id"] for t in m["none"]}
        assert a_ids == {ids["A1"], ids["A2"]}, a_ids
        assert b_ids == {ids["B1"], ids["B2"]}, b_ids
        assert n_ids == {ids["ORPH"]}, n_ids
        return "3 buckets, no cross-project leak"


@check("3. Every bucket is ordered by created_at DESC — newest first")
def _():
    from app import create_app
    from app.services.task_hierarchy import parents_by_project_grouped
    app = create_app()
    with app.app_context():
        cid, oid, pa, pb, ids = _boot("TPP3")
        m = parents_by_project_grouped(
            cid, oid, full_visibility=True)
        # A2 is newer than A1 by construction (_boot creates them
        # 5 s apart, later index = later timestamp).
        a_seq = [t["id"] for t in m[str(pa)]]
        assert a_seq == [ids["A2"], ids["A1"]], a_seq
        b_seq = [t["id"] for t in m[str(pb)]]
        assert b_seq == [ids["B2"], ids["B1"]], b_seq
        return "created_at DESC verified for A + B buckets"


@check("4. available_parents_for now sorts by created_at DESC "
        "(was Task.title alpha)")
def _():
    """Guards against a future refactor that puts alpha back in.
    Not a behaviour test — a source-string test, mirrored on
    audit_invoice_pdf_polish.py's pattern for form.html."""
    p = ROOT / "app" / "services" / "task_hierarchy.py"
    txt = p.read_text(encoding="utf-8")
    assert "order_by(Task.created_at.desc())" in txt
    # And the OLD sort must be gone.
    assert "order_by(Task.title)" not in txt, (
        "the alpha sort was re-introduced — audit refuses")
    return "sort is created_at DESC + old alpha sort is gone"


@check("5. GET /tasks/new ships the task-parents-data <script> and "
        "the initial parent_choices matches the picked project")
def _():
    from app import create_app
    from app.services.api_tokens import generate_token
    app = create_app()
    with app.app_context():
        cid, oid, pa, pb, ids = _boot("TPP5")
        client = app.test_client()
        with client.session_transaction() as s:
            s["_user_id"] = str(oid)
            s["active_company_id"] = cid
        # 5a — no project picked. Initial paint is the "none" bucket.
        r = client.get("/tasks/new")
        assert r.status_code == 200, r.status_code
        body = r.data.decode("utf-8")
        assert 'id="task-parents-data"' in body, (
            "missing task-parents-data JSON script tag")
        # The orphan task's title is present in the initial parent
        # list (server-rendered). Project A/B tasks are NOT in the
        # initial <option>s — they only appear in the JSON payload.
        # We can't easily tell "which section" a title falls under
        # from a raw grep, so we rely on the JSON payload check.
        # 5b — GET /tasks/new?project_id=<A> primes the initial
        # parent list to the A bucket.
        r2 = client.get(f"/tasks/new?project_id={pa}")
        assert r2.status_code == 200
        body2 = r2.data.decode("utf-8")
        # Both project A tasks appear as <option> in the parent
        # select; the orphan does NOT appear in the initial paint.
        # Match on Task title text.
        assert "Task A1" in body2 and "Task A2" in body2, (
            "project A tasks missing from initial parent list")
        # Orphan title only appears inside the embedded JSON, not
        # in the initial visible <option>s. We can't distinguish
        # from a raw text scan alone, so instead we assert the
        # SHAPE by counting occurrences: A1 + A2 appear twice
        # (once in the initial <option>s, once in the JSON), the
        # orphan appears exactly once (JSON only).
        n_a1 = body2.count("Task A1")
        n_orph = body2.count("Task ORPH")
        assert n_a1 >= 2, n_a1
        assert n_orph == 1, (
            f"orphan task should be in JSON only, count={n_orph}")
        return ("task-parents-data present + initial paint = "
                "picked project's bucket")


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
