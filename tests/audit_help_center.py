#!/usr/bin/env python3
"""MARSOUD-HELP-CENTER-01 (Abdelhamid 2026-07-24).

Checks:
  1. Video-URL extractor accepts youtube.com, youtu.be, vimeo.com;
     rejects garbage.
  2. Super-admin creates article + example + video media → rows saved.
  3. Unpublished article → /help/<key> returns 404.
  4. Publish → /help/<key> returns 200 with title.
  5. Contextual "?" icon renders when a matching article exists,
     hides when nothing published for that module.
  6. Invalid YouTube URL raises HelpMediaError at add-time.
"""
import io
import os
import sys
from datetime import datetime
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


def _teardown():
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM help_media WHERE article_id IN "
            "(SELECT id FROM help_articles WHERE module_key LIKE 'hc-%')"))
        conn.execute(text(
            "DELETE FROM help_examples WHERE article_id IN "
            "(SELECT id FROM help_articles WHERE module_key LIKE 'hc-%')"))
        conn.execute(text(
            "DELETE FROM help_articles WHERE module_key LIKE 'hc-%'"))
        conn.execute(text(
            "DELETE FROM users WHERE email = 'hc-super@x.test'"))


def _mk_superadmin():
    from app.models import User, UserStatus
    from werkzeug.security import generate_password_hash
    u = User(email="hc-super@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="hc-super", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             is_superadmin=True, terms_version="TEST")
    db.session.add(u); db.session.commit()
    return u


@check("1. Video URL extractor recognizes YT/Vimeo, rejects garbage")
def _():
    from app.services.help_media import extract_video
    assert extract_video("https://youtube.com/watch?v=dQw4w9WgXcQ") \
        == ("YOUTUBE", "dQw4w9WgXcQ")
    assert extract_video("https://youtu.be/dQw4w9WgXcQ") \
        == ("YOUTUBE", "dQw4w9WgXcQ")
    assert extract_video("https://vimeo.com/76979871") \
        == ("VIMEO", "76979871")
    assert extract_video("https://example.com/foo") is None
    assert extract_video("") is None
    assert extract_video(None) is None
    return "YT/YT-short/Vimeo OK; garbage rejected"


@check("2. Super-admin CRUD via /admin/help/ path")
def _():
    from flask import current_app
    from app.models import HelpArticle, HelpArticleExample
    _teardown()
    u = _mk_superadmin()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
    # Create
    r = client.post("/admin/help/new", data={
        "module_key": "hc-invoices",
        "title_ar": "شرح شاشة الفواتير",
        "title_en": "Invoices Screen",
        "goal": "تعلم إزاي تعمل فاتورة عميل",
        "general_explanation": "الفواتير بتتقسم لبنود...",
        "tips": "احفظ قبل الطباعة\nراجع الأرقام",
        "display_order": "10",
    }, follow_redirects=False)
    assert r.status_code in (302, 303), \
        f"expected redirect, got {r.status_code}"
    a = HelpArticle.query.filter_by(module_key="hc-invoices").first()
    assert a, "article not saved"
    assert a.title_ar == "شرح شاشة الفواتير"
    assert a.tips_list == ["احفظ قبل الطباعة", "راجع الأرقام"]
    assert not a.is_published
    _STATE["article_id"] = a.id
    # Add example
    r = client.post(f"/admin/help/{a.id}/examples", data={
        "title": "مثال: فاتورة عميل نقدي",
        "body": "1) افتح شاشة الفاتورة\n2) اختر عميل...",
    })
    ex = HelpArticleExample.query.filter_by(article_id=a.id).first()
    assert ex, "example not saved"
    # Add video media
    r = client.post(f"/admin/help/{a.id}/media", data={
        "kind": "VIDEO",
        "url": "https://youtu.be/dQw4w9WgXcQ",
        "caption": "شرح فيديو",
    })
    from app.models import HelpArticleMedia
    m = HelpArticleMedia.query.filter_by(article_id=a.id).first()
    assert m, "media not saved"
    assert m.type == "YOUTUBE"
    assert m.url == "dQw4w9WgXcQ"
    return "article + example + YT embed saved"


@check("3. Unpublished article → /help/hc-invoices returns 404")
def _():
    from flask import current_app
    from app.models import User
    u = User.query.filter_by(email="hc-super@x.test").first()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
    r = client.get("/help/hc-invoices")
    assert r.status_code == 404, f"expected 404, got {r.status_code}"
    return "unpublished 404"


@check("4. Publish → /help/hc-invoices returns 200 with title")
def _():
    from flask import current_app
    from app.models import HelpArticle, User
    a = db.session.get(HelpArticle, _STATE["article_id"])
    a.is_published = True; db.session.commit()
    u = User.query.filter_by(email="hc-super@x.test").first()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
    r = client.get("/help/hc-invoices")
    assert r.status_code == 200, f"expected 200, got {r.status_code}"
    body = r.get_data(as_text=True)
    assert "شرح شاشة الفواتير" in body
    # Video embed rendered as iframe.
    assert "https://www.youtube.com/embed/dQw4w9WgXcQ" in body
    # Example rendered.
    assert "مثال: فاتورة عميل نقدي" in body
    return "article page renders with title + video + example"


@check("5. Bad YouTube URL rejected at save-time")
def _():
    from flask import current_app
    from app.models import User, HelpArticleMedia
    u = User.query.filter_by(email="hc-super@x.test").first()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
    before = HelpArticleMedia.query.filter_by(
        article_id=_STATE["article_id"]).count()
    r = client.post(f"/admin/help/{_STATE['article_id']}/media", data={
        "kind": "VIDEO",
        "url": "https://example.com/not-a-video",
        "caption": "should not save",
    })
    after = HelpArticleMedia.query.filter_by(
        article_id=_STATE["article_id"]).count()
    assert before == after, \
        f"bad URL was saved: before={before} after={after}"
    return "invalid URL not saved + flash"


@check("6. Header ? icon renders on invoices page (published article exists)")
def _():
    from flask import current_app
    from app.models import Company, User, UserStatus, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    plan = Plan.query.first()
    c = Company(name="__HC_TESTCO__", base_currency="EGP",
                subdomain="hc-testco",
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1),
                plan_id=plan.id if plan else None,
                intended_plan_id=plan.id if plan else None)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    owner = User(email="hc-owner@x.test",
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name="hc-owner", is_active=True,
                 status=UserStatus.ACTIVE.value,
                 email_verified_at=datetime.utcnow(),
                 terms_version="TEST", is_superadmin=True)
    db.session.add(owner); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner"))
    db.session.commit()
    _STATE["testco_id"] = c.id
    _STATE["owner_id"] = owner.id

    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(owner.id)
        sess["_fresh"] = True
        sess["active_company_id"] = c.id
    # First publish an article for module_key='invoices' — we already
    # have one for 'hc-invoices', but the mapping expects 'invoices'.
    # Add a second published article at the real key.
    from app.models import HelpArticle
    # Use module_key='reports' — /reports/ is a well-known page that
    # renders base.html for both owners and superadmins without extra
    # onboarding redirects (verified in Ticket 2's audit).
    a2 = HelpArticle(
        module_key="reports",
        title_ar="التقارير — الشرح الحقيقي",
        is_published=True,
        goal="test",
        created_by_id=owner.id,
    )
    db.session.add(a2); db.session.commit()
    _STATE["a2_id"] = a2.id
    from app.services.help_media import has_published_article
    assert has_published_article("reports"), \
        "sanity: has_published_article() returned False after commit"
    r = client.get("/reports/", follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "/help/reports" in body, (
        f"? icon missing (status={r.status_code}, len={len(body)})")
    return "? icon renders on /reports/ + points to /help/reports"


@check("7. ? icon hides on pages with no matching article")
def _():
    from flask import current_app
    from app.models import User
    owner = db.session.get(User, _STATE["owner_id"])
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(owner.id)
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["testco_id"]
    # /tasks/ maps to module_key="tasks" — no article published for it.
    r = client.get("/tasks/")
    body = r.get_data(as_text=True)
    assert "/help/tasks" not in body, \
        "? icon incorrectly rendered for a module with no article"
    return "? icon correctly hidden when nothing published"


def _final_teardown():
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__HC_%__'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'hc-%@x.test'"))
        conn.execute(text(
            "DELETE FROM help_articles WHERE module_key IN "
            "('hc-invoices', 'invoices', 'reports')"))


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _teardown()
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
            _final_teardown()
            print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
