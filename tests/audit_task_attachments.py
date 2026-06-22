#!/usr/bin/env python3
"""MARSOUD — Task creation attachments audit (ticket B / image #46).

Acceptance: a user creating a task can attach multiple files (PDF /
images / Word / Excel / ZIP) in the SAME submission, and they end up
in the Documents table bound to that task.
"""
import io
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
DEMO_EMAIL = "demo@manasety.ai"
DEMO_PASS = "demo1234"


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _login(client, email, password):
    r = client.post("/login", data={"email": email, "password": password},
                    follow_redirects=False)
    assert r.status_code in (302, 303), \
        f"login for {email} failed: status={r.status_code}"


@check("1. Form HTML declares multipart encoding + a multi-file input")
def _():
    src = (ROOT / "app/templates/tasks/form.html").read_text()
    assert 'enctype="multipart/form-data"' in src, \
        "form not multipart"
    assert 'name="attachments"' in src, "attachments input missing"
    assert "multiple" in src, "input not marked multiple"
    return "multipart form + <input multiple name=attachments>"


@check("2. Route consumes request.files.getlist('attachments')")
def _():
    src = (ROOT / "app/routes/tasks.py").read_text()
    assert 'request.files.getlist("attachments")' in src, \
        "new() doesn't read the multi-file list"
    assert "save_document" in src, "save_document call missing"
    assert "DocumentSourceType.TASK" in src, \
        "task source_type not specified for the saved docs"
    return "new() loops the uploaded files through save_document(TASK)"


@check("3. Form for EDIT mode hides the attachment input")
def _():
    src = (ROOT / "app/templates/tasks/form.html").read_text()
    # The attachment block is wrapped in {% if not task %}
    assert "{% if not task %}" in src, \
        "edit mode should hide the attachments field"
    return "attachments shown only in create mode"


@check("4. Live: POST /tasks/new with 2 files → both Documents created")
def _():
    """The full multipart round-trip via Flask's test_client."""
    from app.models import Task, Document, Company

    app = create_app()
    with app.app_context():
        company = Company.query.first()
        with app.test_client() as client:
            _login(client, DEMO_EMAIL, DEMO_PASS)

            # Build two in-memory files
            pdf = (io.BytesIO(b"%PDF-1.0\nfake content\n"), "audit_test.pdf")
            png = (io.BytesIO(b"\x89PNG\r\n\x1a\nfake"), "audit_test.png")

            data = {
                "title": "AUDIT-TASK-ATTACH",
                "priority": "LOW",
                "assignee_ids": "1",   # demo user id 1
                "deadline": "2026-12-31",
                "attachments": [pdf, png],
            }
            r = client.post("/tasks/new", data=data,
                            content_type="multipart/form-data",
                            follow_redirects=False)
            assert r.status_code in (302, 303), \
                f"create failed: status={r.status_code}"

        t = Task.query.filter_by(company_id=company.id,
                                  title="AUDIT-TASK-ATTACH").order_by(
                                      Task.id.desc()).first()
        assert t, "task wasn't created"
        docs = Document.query.filter_by(
            source_type="TASK", source_id=t.id,
        ).all()
        names = sorted(d.name for d in docs)
        # Cleanup before asserting so a fail still leaves the DB clean
        for d in docs:
            db.session.delete(d)
        db.session.delete(t)
        db.session.commit()
        assert len(docs) == 2, f"expected 2 attachments, got {len(docs)}"
        assert names == ["audit_test.pdf", "audit_test.png"], \
            f"unexpected attachment names: {names}"
    return f"2 files attached: {names}"


@check("5. Live: invalid extension fails per file, task still creates")
def _():
    """An unsupported extension (.exe) must not abort the task creation
    — the file should be flashed as failed, the task survives."""
    from app.models import Task, Document, Company

    app = create_app()
    with app.app_context():
        company = Company.query.first()
        with app.test_client() as client:
            _login(client, DEMO_EMAIL, DEMO_PASS)
            data = {
                "title": "AUDIT-TASK-BAD-EXT",
                "priority": "LOW",
                "assignee_ids": "1",
                "deadline": "2026-12-31",
                "attachments": [
                    (io.BytesIO(b"M Z fake exe"), "bad.exe"),
                ],
            }
            r = client.post("/tasks/new", data=data,
                            content_type="multipart/form-data",
                            follow_redirects=False)
            assert r.status_code in (302, 303), \
                f"task should still create; got {r.status_code}"

        t = Task.query.filter_by(company_id=company.id,
                                  title="AUDIT-TASK-BAD-EXT").order_by(
                                      Task.id.desc()).first()
        assert t, "task wasn't created"
        docs = Document.query.filter_by(
            source_type="TASK", source_id=t.id,
        ).all()
        # Cleanup
        for d in docs:
            db.session.delete(d)
        db.session.delete(t)
        db.session.commit()
        assert len(docs) == 0, \
            f"bad.exe shouldn't have been saved, got {len(docs)} docs"
    return "task created OK; bad.exe rejected without rolling back the task"


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
