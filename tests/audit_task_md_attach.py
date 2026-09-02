#!/usr/bin/env python3
"""MARSOUD-TASK-MD-ATTACH-01 (2026-09-02) — accept .md attachments.

User asked: "عاوز اقدر ارفع فايلات md في المهام". Widening the shared
`ALLOWED_EXTS` in `services/opsflow_extras.py` covers every source
that goes through `save_document()` — tasks, leads, projects, custody.
This audit locks the whitelist so a future refactor doesn't drop
markdown by accident.

Checks:
  1. `md, txt, csv, json` all in the ALLOWED_EXTS set.
  2. Legacy formats still present (regression guard on the widening).
  3. `.exe / .sh` still refused — widening didn't turn into an
     "anything goes" whitelist.
  4. Task form template's `accept=` hint advertises `.md` so the OS
     file picker shows markdown files by default.
"""
import os
import sys
import io
from pathlib import Path
from datetime import datetime

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


@check("1. md/txt/csv/json all in ALLOWED_EXTS")
def _():
    from app.services.opsflow_extras import ALLOWED_EXTS
    for want in ("md", "txt", "csv", "json"):
        assert want in ALLOWED_EXTS, \
            f"{want!r} missing from ALLOWED_EXTS: {sorted(ALLOWED_EXTS)}"
    return "4 text formats present"


@check("2. legacy formats still present (regression guard)")
def _():
    from app.services.opsflow_extras import ALLOWED_EXTS
    for want in ("pdf", "png", "jpg", "jpeg", "docx", "xlsx", "zip",
                 "heic"):
        assert want in ALLOWED_EXTS, f"legacy {want!r} was dropped!"
    return f"{len(ALLOWED_EXTS)} total formats retained"


@check("3. binary formats (.exe/.sh) still refused")
def _():
    from app.services.opsflow_extras import ALLOWED_EXTS
    for banned in ("exe", "sh", "bat", "ps1", "dll", "so", "app",
                    "js", "html", "php", "py"):
        assert banned not in ALLOWED_EXTS, (
            f"widening accidentally allowed {banned!r} — "
            "this is a security regression"
        )
    return "10 exec/script extensions correctly refused"


@check("4. save_document accepts a .md upload end-to-end")
def _():
    from app import create_app, db
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.opsflow_extras import save_document, DocumentError
    from werkzeug.datastructures import FileStorage
    from sqlalchemy import text
    app = create_app()
    with app.app_context():
        # Clean prior audit rows
        db.session.execute(text(
            "DELETE FROM companies WHERE name LIKE '__MD__%'"))
        db.session.execute(text(
            "DELETE FROM users WHERE email LIKE '%__md__%'"))
        db.session.execute(text(
            "DELETE FROM plans WHERE code = '__MD__'"))
        db.session.commit()
        plan = Plan(code="__MD__", name="C", name_ar="C")
        plan.set_modules(["crm"])
        db.session.add(plan); db.session.flush()
        c = Company(name="__MD__co", base_currency="EGP",
                     subdomain="md", plan_id=plan.id,
                     subscription_started_at=datetime.utcnow(),
                     subscription_expires_at=datetime(2999, 1, 1))
        db.session.add(c); db.session.commit()

        try:
            from app.services.legal import get_terms_version
            tv = get_terms_version() or "audit"
        except Exception:
            tv = "audit"
        u = User(email="owner__md__@x.io", full_name="Owner",
                 is_active=True,
                 email_verified_at=datetime.utcnow(),
                 terms_version=tv,
                 terms_accepted_at=datetime.utcnow())
        u.set_password("pw12345678")
        db.session.add(u); db.session.commit()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=c.id, role="owner"))
        db.session.commit()

        fs = FileStorage(
            stream=io.BytesIO(b"# Markdown\nHello from md upload."),
            filename="spec.md",
            content_type="text/markdown",
        )
        try:
            doc = save_document(
                company_id=c.id,
                source_type="TASK", source_id=1,
                file_storage=fs,
                visibility="INTERNAL",
                uploaded_by_id=u.id,
            )
        except DocumentError as e:
            raise AssertionError(f".md upload refused: {e}") from None
        assert doc.file_path.endswith(".md"), f"stored path: {doc.file_path}"
        assert doc.name == "spec.md"

        # Clean up the file we just wrote
        try:
            fp = Path(str(app.root_path)) / doc.file_path.lstrip("/")
            if fp.exists():
                fp.unlink()
        except Exception:
            pass
        return f"stored as {doc.file_path.rsplit('/', 1)[-1]}"


@check("5. .exe upload correctly refused with the widened whitelist")
def _():
    from app import create_app
    from app.services.opsflow_extras import save_document, DocumentError
    from werkzeug.datastructures import FileStorage
    import io as _io
    app = create_app()
    with app.app_context():
        fs = FileStorage(
            stream=_io.BytesIO(b"MZfake"),
            filename="malware.exe",
            content_type="application/octet-stream",
        )
        try:
            save_document(
                company_id=1, source_type="TASK", source_id=1,
                file_storage=fs, visibility="INTERNAL",
                uploaded_by_id=None,
            )
        except DocumentError as e:
            assert "غير مدعومة" in str(e), (
                f"expected extension-refusal message, got: {e}")
            return ".exe refused as expected"
        raise AssertionError(".exe was accepted!")


@check("6. task form advertises .md in its accept attribute")
def _():
    p = Path(ROOT) / "app" / "templates" / "tasks" / "form.html"
    html = p.read_text(encoding="utf-8")
    assert ".md" in html, ".md missing from tasks/form.html accept="
    # The detail-page single-file uploader too
    p2 = Path(ROOT) / "app" / "templates" / "tasks" / "detail.html"
    html2 = p2.read_text(encoding="utf-8")
    assert ".md" in html2, ".md missing from tasks/detail.html accept="
    return "both form + detail advertise .md"


def main():
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
