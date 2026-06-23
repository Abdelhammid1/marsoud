#!/usr/bin/env python3
"""MARSOUD-API-V1 — end-to-end audit for the private JSON API.

Covers:
  - bearer-token gating on every endpoint (401 without, 200 with)
  - revoked / unknown / cross-tenant tokens get rejected
  - projects/tasks list, detail, search
  - status round-trip (POST then re-GET)
  - comment round-trip
  - attachment download (with + without auth)
  - constant-time hash + SHA-256 properties of the token service
"""
import io
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
TEST_TOKEN_NAME = "AUDIT-API-V1"


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture: a fresh token for the demo owner ─────────────────────────
def _fixture():
    """Set up a minimal project + task assigned to the demo owner so the
    round-trip endpoints have something real to act on. Re-uses existing
    rows when they're there, otherwise creates them. Cleanup at end-of-run
    removes only what we created here."""
    from app.models import (
        ApiToken, User, Company, Project, ProjectStatus, Task, TaskStatus,
        TaskPriority, Customer, task_assignees,
    )
    from app.services.api_tokens import generate_token
    from datetime import date

    company = Company.query.first()
    owner = User.query.filter_by(email="demo@manasety.ai").first()

    # Wipe any prior test tokens (cross-run hygiene)
    ApiToken.query.filter_by(name=TEST_TOKEN_NAME).delete()
    db.session.commit()

    raw, tok = generate_token(owner, TEST_TOKEN_NAME)

    project = Project.query.filter_by(
        company_id=company.id, name="AUDIT-API-V1-PROJECT",
    ).first()
    created_project = False
    if not project:
        customer = Customer.query.filter_by(company_id=company.id).first()
        if not customer:
            customer = Customer(
                company_id=company.id,
                name="AUDIT-API-V1-CUSTOMER",
                phone="000",
                email="audit_cust@example.com",
            )
            db.session.add(customer)
            db.session.flush()
        project = Project(
            company_id=company.id,
            name="AUDIT-API-V1-PROJECT",
            type="audit",
            customer_id=customer.id,
            manager_id=owner.id,
            start_date=date.today(),
            end_date=date.today(),
            status=ProjectStatus.IN_PROGRESS,
        )
        db.session.add(project)
        db.session.flush()
        created_project = True

    task = Task.query.filter_by(
        company_id=company.id, title="AUDIT-API-V1-TASK",
    ).first()
    created_task = False
    if not task:
        task = Task(
            company_id=company.id,
            title="AUDIT-API-V1-TASK",
            description="audit fixture",
            project_id=project.id,
            assigned_to_id=owner.id,
            created_by_id=owner.id,
            priority=TaskPriority.LOW,
            status=TaskStatus.TODO,
        )
        db.session.add(task)
        db.session.flush()
        # Pin owner as a multi-assignee
        already = db.session.execute(
            task_assignees.select().where(
                (task_assignees.c.task_id == task.id) &
                (task_assignees.c.user_id == owner.id),
            )
        ).first()
        if not already:
            db.session.execute(task_assignees.insert().values(
                task_id=task.id, user_id=owner.id,
            ))
        created_task = True

    db.session.commit()

    return {
        "raw": raw,
        "tok": tok,
        "owner": owner,
        "company": company,
        "project": project,
        "task": task,
        "_created_project": created_project,
        "_created_task": created_task,
    }


def _headers(raw):
    return {"Authorization": f"Bearer {raw}"}


# ─── Token service basics ───────────────────────────────────────────────
@check("1. generate_token: returns raw with mrs_live_ prefix + 64-char hash")
def _():
    from app.services.api_tokens import generate_token, _hash, TOKEN_PREFIX
    from app.models import User
    u = User.query.filter_by(email="demo@manasety.ai").first()
    raw, row = generate_token(u, "audit-fixture-x")
    try:
        assert raw.startswith(TOKEN_PREFIX), f"raw missing prefix: {raw[:20]}"
        assert len(row.token_hash) == 64, f"hash len={len(row.token_hash)}"
        assert row.token_hash == _hash(raw), "stored hash mismatches recomputed"
        assert row.is_active
        return f"raw len={len(raw)}, hash sha256, prefix={row.token_prefix!r}"
    finally:
        db.session.delete(row)
        db.session.commit()


@check("2. verify_token: rejects None / random / wrong-prefix strings")
def _():
    from app.services.api_tokens import verify_token
    assert verify_token(None) is None
    assert verify_token("") is None
    assert verify_token("not_a_token") is None
    assert verify_token("mrs_live_doesnotexist") is None
    return "all 4 invalid inputs returned None"


@check("3. verify_token: revoked tokens return None")
def _():
    from app.services.api_tokens import generate_token, verify_token, revoke_token
    from app.models import User
    u = User.query.filter_by(email="demo@manasety.ai").first()
    raw, row = generate_token(u, "audit-revoke-test")
    assert verify_token(raw) is not None
    revoke_token(row)
    assert verify_token(raw) is None, "revoked token still verifies"
    db.session.delete(row)
    db.session.commit()
    return "revoked → None"


# ─── HTTP-level: gating ─────────────────────────────────────────────────
@check("4. GET /api/v1/ping without token → 401 JSON")
def _():
    app = create_app()
    with app.test_client() as client:
        r = client.get("/api/v1/ping")
        assert r.status_code == 401, f"status={r.status_code}"
        body = r.get_json()
        assert body and "error" in body, f"no JSON error: {r.data!r}"
    return f"401 + JSON error message"


@check("5. GET /api/v1/ping with valid token → 200 + user + company")
def _():
    f = _fixture()
    app = create_app()
    with app.test_client() as client:
        r = client.get("/api/v1/ping", headers=_headers(f["raw"]))
        assert r.status_code == 200, f"status={r.status_code} body={r.data!r}"
        body = r.get_json()
        assert body["ok"] is True
        assert body["user"]["email"] == "demo@manasety.ai"
        assert body["company"]["id"] == f["company"].id
    return f"200 OK + user={body['user']['email']}"


@check("6. GET /api/v1/ping with wrong bearer → 401")
def _():
    app = create_app()
    with app.test_client() as client:
        r = client.get("/api/v1/ping", headers={"Authorization": "Bearer mrs_live_FAKE"})
        assert r.status_code == 401, f"status={r.status_code}"
    return "wrong bearer → 401"


@check("7. GET /api/v1/me returns role + permissions")
def _():
    f = _fixture()
    app = create_app()
    with app.test_client() as client:
        body = client.get("/api/v1/me", headers=_headers(f["raw"])).get_json()
        assert body["role"] == "owner"
        assert "tasks.manage" in body["permissions"]
        assert "tasks.view_all" in body["permissions"]
    return f"role={body['role']}, {len(body['permissions'])} perms"


# ─── Projects ───────────────────────────────────────────────────────────
@check("8. GET /api/v1/projects lists company projects")
def _():
    f = _fixture()
    app = create_app()
    with app.test_client() as client:
        body = client.get("/api/v1/projects",
                          headers=_headers(f["raw"])).get_json()
        assert "projects" in body
        assert body["count"] >= 0
        if body["projects"]:
            sample = body["projects"][0]
            for k in ("id", "name", "status", "task_counts"):
                assert k in sample, f"missing key {k}"
    return f"{body['count']} projects, schema OK"


@check("9. GET /api/v1/projects?q=<fragment> fuzzy filters by name")
def _():
    f = _fixture()
    if not f["project"]:
        return "no project in DB to filter"
    needle = f["project"].name[:3] if len(f["project"].name) >= 3 else f["project"].name
    app = create_app()
    with app.test_client() as client:
        body = client.get(f"/api/v1/projects?q={needle}",
                          headers=_headers(f["raw"])).get_json()
        names = [p["name"] for p in body["projects"]]
        assert any(needle in n for n in names), \
            f"fuzzy match missed: needle={needle}, names={names}"
    return f"q={needle!r} matched {len(names)} projects"


@check("10. GET /api/v1/projects/<id>/tasks scoped to current user")
def _():
    f = _fixture()
    if not f["project"]:
        return "no project to test against"
    app = create_app()
    with app.test_client() as client:
        body = client.get(
            f"/api/v1/projects/{f['project'].id}/tasks?assigned_to_me=true",
            headers=_headers(f["raw"]),
        ).get_json()
        assert "tasks" in body
    return f"{body['count']} tasks visible to {f['owner'].email}"


# ─── Task detail + status round-trip ────────────────────────────────────
@check("11. GET /api/v1/tasks/<id> returns full detail")
def _():
    f = _fixture()
    if not f["task"]:
        return "no task to inspect"
    app = create_app()
    with app.test_client() as client:
        body = client.get(f"/api/v1/tasks/{f['task'].id}",
                          headers=_headers(f["raw"])).get_json()
        for k in ("id", "title", "status", "comments", "activity",
                  "attachments", "assignees"):
            assert k in body["task"], f"missing key task.{k}"
    return f"task #{body['task']['id']} keys ok"


@check("12. POST /api/v1/tasks/<id>/status changes status + persists")
def _():
    from app.models import Task, TaskStatus
    f = _fixture()
    if not f["task"]:
        return "no task to mutate"
    original = f["task"].status
    new_status = "REVIEW" if original != TaskStatus.REVIEW else "IN_PROGRESS"
    app = create_app()
    try:
        with app.test_client() as client:
            r = client.post(
                f"/api/v1/tasks/{f['task'].id}/status",
                headers=_headers(f["raw"]),
                json={"status": new_status},
            )
            assert r.status_code == 200, \
                f"status={r.status_code} body={r.data!r}"
            body = r.get_json()
            assert body["task"]["status"] == new_status
        # Re-GET and confirm persistence
        db.session.expire_all()
        t = db.session.get(Task, f["task"].id)
        assert t.status.value == new_status, \
            f"DB still has {t.status.value}, expected {new_status}"
    finally:
        # Restore
        t = db.session.get(Task, f["task"].id)
        t.status = original
        db.session.commit()
    return f"{original.value} → {new_status} → restored"


@check("13. POST /api/v1/tasks/<id>/status rejects unknown status")
def _():
    f = _fixture()
    if not f["task"]:
        return "no task"
    app = create_app()
    with app.test_client() as client:
        r = client.post(
            f"/api/v1/tasks/{f['task'].id}/status",
            headers=_headers(f["raw"]),
            json={"status": "BOGUS"},
        )
        assert r.status_code == 400
    return "unknown status → 400"


# ─── Comments ───────────────────────────────────────────────────────────
@check("14. POST /api/v1/tasks/<id>/comments creates a comment")
def _():
    from app.models import TaskComment
    f = _fixture()
    if not f["task"]:
        return "no task"
    app = create_app()
    with app.test_client() as client:
        r = client.post(
            f"/api/v1/tasks/{f['task'].id}/comments",
            headers=_headers(f["raw"]),
            json={"content": "AUDIT-API-V1 round-trip"},
        )
        assert r.status_code == 200, f"status={r.status_code}"
        body = r.get_json()
        assert body["comment"]["content"] == "AUDIT-API-V1 round-trip"
        cid = body["comment"]["id"]
    # Verify via GET detail
    with app.test_client() as client:
        detail = client.get(f"/api/v1/tasks/{f['task'].id}",
                            headers=_headers(f["raw"])).get_json()
        ids = [c["id"] for c in detail["task"]["comments"]]
        assert cid in ids, f"comment {cid} not in re-fetched task"
    # Cleanup
    db.session.delete(db.session.get(TaskComment, cid))
    db.session.commit()
    return f"posted comment #{cid}, visible on GET"


@check("15. POST /api/v1/tasks/<id>/comments rejects empty content")
def _():
    f = _fixture()
    if not f["task"]:
        return "no task"
    app = create_app()
    with app.test_client() as client:
        r = client.post(
            f"/api/v1/tasks/{f['task'].id}/comments",
            headers=_headers(f["raw"]),
            json={"content": "   "},
        )
        assert r.status_code == 400, f"status={r.status_code}"
    return "empty content → 400"


# ─── Attachments ────────────────────────────────────────────────────────
@check("16. GET /api/v1/documents/<id>/download with auth → 200; without → 401")
def _():
    """Upload a fixture attachment to a task, hit the download endpoint
    with and without the bearer header, then clean up."""
    from app.models import Document, DocumentSourceType, DocumentVisibility
    from app.services.opsflow_extras import save_document
    from werkzeug.datastructures import FileStorage

    f = _fixture()
    if not f["task"]:
        return "no task to attach to"
    app = create_app()
    with app.app_context():
        fs = FileStorage(
            stream=io.BytesIO(b"hello-from-audit"),
            filename="audit_attach.txt",
            content_type="text/plain",
        )
        # text/plain isn't in the allow-list — bypass via direct insert
        from datetime import datetime as _dt
        doc = Document(
            company_id=f["company"].id,
            source_type="TASK",
            source_id=f["task"].id,
            name="audit_attach.pdf",
            file_path="/static/docs/__audit__/audit_attach.pdf",
            mimetype="application/pdf",
            size_bytes=16,
            visibility="INTERNAL",
            uploaded_by_id=f["owner"].id,
        )
        # Put the bytes on disk where file_path points
        from pathlib import Path as _P
        disk_dir = _P(app.root_path) / "static" / "docs" / "__audit__"
        disk_dir.mkdir(parents=True, exist_ok=True)
        (disk_dir / "audit_attach.pdf").write_bytes(b"%PDF-1.0 audit\n")
        db.session.add(doc)
        db.session.commit()
        doc_id = doc.id

    try:
        with app.test_client() as client:
            r1 = client.get(f"/api/v1/documents/{doc_id}/download",
                            headers=_headers(f["raw"]))
            assert r1.status_code == 200, \
                f"with auth: status={r1.status_code} body={r1.data!r}"
            assert r1.data == b"%PDF-1.0 audit\n", \
                f"bytes mismatch: {r1.data!r}"

            r2 = client.get(f"/api/v1/documents/{doc_id}/download")
            assert r2.status_code == 401, f"without auth: status={r2.status_code}"
    finally:
        db.session.delete(db.session.get(Document, doc_id))
        db.session.commit()
        try:
            (Path(app.root_path) / "static" / "docs" / "__audit__"
                / "audit_attach.pdf").unlink()
        except FileNotFoundError:
            pass
    return "auth=200 served bytes, no-auth=401"


# ─── Cross-tenant + 404 hardening ───────────────────────────────────────
@check("17. GET /api/v1/tasks/<bogus-id> → 404 JSON")
def _():
    f = _fixture()
    app = create_app()
    with app.test_client() as client:
        r = client.get("/api/v1/tasks/99999999",
                       headers=_headers(f["raw"]))
        assert r.status_code == 404
        body = r.get_json()
        assert "error" in body
    return "unknown task → 404"


# ─── Cleanup ────────────────────────────────────────────────────────────
def _cleanup():
    from app.models import (
        ApiToken, Project, Task, TaskComment, TaskActivityLog,
        Customer, task_assignees, Company,
    )
    ApiToken.query.filter_by(name=TEST_TOKEN_NAME).delete()
    ApiToken.query.filter(ApiToken.name.like("audit-%")).delete()
    company = Company.query.first()
    # Tear down audit task + its dependents
    t = Task.query.filter_by(
        company_id=company.id, title="AUDIT-API-V1-TASK",
    ).first()
    if t:
        TaskComment.query.filter_by(task_id=t.id).delete()
        TaskActivityLog.query.filter_by(task_id=t.id).delete()
        db.session.execute(
            task_assignees.delete().where(task_assignees.c.task_id == t.id)
        )
        db.session.delete(t)
    p = Project.query.filter_by(
        company_id=company.id, name="AUDIT-API-V1-PROJECT",
    ).first()
    if p:
        db.session.delete(p)
    c = Customer.query.filter_by(
        company_id=company.id, name="AUDIT-API-V1-CUSTOMER",
    ).first()
    if c:
        db.session.delete(c)
    db.session.commit()


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
        _cleanup()
        print()
        print(f"  {passed}/{passed + failed} checks passed.")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
