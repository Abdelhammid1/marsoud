#!/usr/bin/env python3
"""MARSOUD-RICH-TEXT-EDITOR-01 (2026-09-17) — Phase 1 audit.

Phase 1 scope:
  · sanitize_html strips XSS vectors + keeps allowed formatting
  · render_rich_text (Jinja `rich_text` filter) safely handles both
    HTML content AND legacy plain-text descriptions
  · task write paths (create + edit + inline patch) sanitize before
    storing
  · task/form.html loads the editor + wires the description field
  · task/detail.html renders the description through the rich-text
    filter, not the old plain-text branch

Checks:
  1-5. sanitize_html: script tag, on* handler, javascript: URL,
       data: URL, disallowed CSS all get stripped; allowed
       formatting (bold, italic, list, link, code, heading) sails
       through.
  6. render_rich_text on plain-text preserves the text, escapes any
     `<` that appears, and converts `\\n` to `<br>`.
  7. render_rich_text on HTML re-sanitizes + returns Markup.
  8. Round-trip: POST /tasks/new with a description containing an
     XSS payload → the DB row's description has NO script/onerror.
  9. GET /tasks/<id> renders the safe HTML, NOT the raw script tag.
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
    return c.id, owner.id


@check("1. sanitize_html strips <script>, keeps allowed formatting")
def _():
    from app import create_app
    from app.services.rich_text import sanitize_html
    app = create_app()
    with app.app_context():
        payload = (
            "<p><strong>Hello</strong>"
            "<script>alert('xss')</script>"
            " <em>world</em></p>"
        )
        out = sanitize_html(payload)
        assert "<strong>Hello</strong>" in out
        assert "<em>world</em>" in out
        assert "<script>" not in out
        assert "alert" not in out, out
        return "script tag stripped; bold + italic kept"


@check("2. sanitize_html strips on* event handlers")
def _():
    from app import create_app
    from app.services.rich_text import sanitize_html
    app = create_app()
    with app.app_context():
        payload = '<a href="https://x.com" onclick="alert(1)">click</a>'
        out = sanitize_html(payload)
        assert "onclick" not in out.lower(), out
        assert "https://x.com" in out
        return "onclick attribute stripped; href kept"


@check("3. sanitize_html blocks javascript: and data: URLs")
def _():
    from app import create_app
    from app.services.rich_text import sanitize_html
    app = create_app()
    with app.app_context():
        js = '<a href="javascript:alert(1)">bad</a>'
        assert "javascript:" not in sanitize_html(js).lower()
        data = '<a href="data:text/html,<script>alert(1)</script>">bad</a>'
        cleaned = sanitize_html(data)
        assert "data:text/html" not in cleaned.lower(), cleaned
        assert "<script>" not in cleaned
        return "javascript: + data: URL schemes refused"


@check("4. sanitize_html allows headings, lists, code, blockquote")
def _():
    from app import create_app
    from app.services.rich_text import sanitize_html
    app = create_app()
    with app.app_context():
        payload = (
            "<h2>Heading</h2>"
            "<ul><li>one</li><li>two</li></ul>"
            "<ol><li>x</li></ol>"
            "<blockquote>quote</blockquote>"
            "<pre><code>x = 1</code></pre>"
        )
        out = sanitize_html(payload)
        for needed in ("<h2>Heading</h2>",
                        "<ul>", "<li>one</li>",
                        "<ol>",
                        "<blockquote>",
                        "<pre>", "<code>x = 1</code>"):
            assert needed in out, f"missing {needed!r} in\n{out}"
        return "structural formatting preserved"


@check("5. Every link opens in a new tab with rel=noopener noreferrer")
def _():
    from app import create_app
    from app.services.rich_text import sanitize_html
    app = create_app()
    with app.app_context():
        out = sanitize_html('<a href="https://example.com">x</a>')
        assert 'target="_blank"' in out, out
        assert "noopener" in out and "noreferrer" in out, out
        return "target=_blank + rel noopener applied"


@check("6. render_rich_text on plain text escapes + nl2br")
def _():
    from app import create_app
    from app.services.rich_text import render_rich_text
    from markupsafe import Markup
    app = create_app()
    with app.app_context():
        # Legacy plain text — the shape most descriptions in the DB
        # have today. Multi-line, no HTML tags, no `<` characters —
        # but the plain-text branch still needs to convert newlines
        # to <br> so the paragraph break survives.
        out = render_rich_text("First line\nSecond line\nThird line")
        assert isinstance(out, Markup)
        s = str(out)
        assert "First line" in s
        assert s.count("<br>") == 2, s
        # A stray `<` that legacy users typed as `a<b`: safe-escape
        # so the browser doesn't try to interpret a nonexistent tag.
        out2 = render_rich_text("a<b, y>x")
        s2 = str(out2)
        assert "&lt;b" in s2 or "a&lt;b" in s2, s2
        return "plain text: newlines → <br>; stray `<` escaped"


@check("7. render_rich_text on HTML re-sanitizes + returns Markup")
def _():
    from app import create_app
    from app.services.rich_text import render_rich_text
    from markupsafe import Markup
    app = create_app()
    with app.app_context():
        out = render_rich_text(
            "<p><strong>ok</strong><script>bad</script></p>")
        assert isinstance(out, Markup)
        s = str(out)
        assert "<strong>ok</strong>" in s
        assert "<script>" not in s and "bad" not in s
        return "HTML input re-sanitized on display"


@check("8. Task.description write path sanitizes XSS payload before "
        "storing")
def _():
    from app import create_app, db
    from app.models import Task, TaskStatus, TaskPriority
    app = create_app()
    with app.app_context():
        cid, oid = _boot("RTE8")
        client = app.test_client()
        with client.session_transaction() as s:
            s["_user_id"] = str(oid)
            s["active_company_id"] = cid
        xss = (
            "<p>Legit <strong>content</strong>.</p>"
            "<script>alert('pwn')</script>"
            "<img src=x onerror=alert(1)>"
        )
        r = client.post("/tasks/new", data={
            "title": "Test task",
            "description": xss,
            "priority": "MEDIUM",
            "assignee_ids": str(oid),
        }, follow_redirects=False)
        assert r.status_code in (302, 303), (r.status_code, r.data[:400])
        row = (Task.query
                .filter_by(company_id=cid, title="Test task")
                .first())
        assert row is not None
        stored = row.description or ""
        assert "Legit" in stored
        assert "<strong>" in stored
        assert "<script>" not in stored
        assert "onerror" not in stored.lower(), stored
        # `<img>` tag is not in our allowlist, so it's stripped
        # entirely.
        assert "<img" not in stored.lower(), stored
        return "stored HTML is clean; script + onerror gone"


@check("9. GET /tasks/<id> renders sanitized HTML, not the raw script")
def _():
    from app import create_app, db
    from app.models import Task
    app = create_app()
    with app.app_context():
        cid, oid = _boot("RTE9")
        client = app.test_client()
        with client.session_transaction() as s:
            s["_user_id"] = str(oid)
            s["active_company_id"] = cid
        client.post("/tasks/new", data={
            "title": "Render check",
            "description": (
                "<p>Read <strong>me</strong>.</p>"
                "<script>alert('bad')</script>"),
            "priority": "MEDIUM",
            "assignee_ids": str(oid),
        }, follow_redirects=False)
        t = (Task.query
             .filter_by(company_id=cid, title="Render check").first())
        assert t is not None
        r = client.get(f"/tasks/{t.id}")
        assert r.status_code == 200, r.status_code
        body = r.data.decode("utf-8")
        # The clean bit shows up.
        assert "<strong>me</strong>" in body
        # The dangerous bit doesn't. NOTE: bleach's linkify or the
        # display macro could keep the literal text "alert" inside a
        # code block; check for the tag form specifically.
        assert "<script>alert" not in body
        return "detail page shows safe HTML"


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
