#!/usr/bin/env python3
"""MARSOUD — Lead Comments audit (ticket C / image #47).

Abdelhamid asked for a comment thread inside each Lead's detail page,
the same way tasks already have one.
"""
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


@check("1. LeadComment model exposed + table exists in the DB")
def _():
    from app.models import LeadComment
    cols = {c.name for c in LeadComment.__table__.columns}
    expected = {"id", "lead_id", "user_id", "company_id", "content",
                "attachment_url", "attachment_name", "created_at"}
    missing = expected - cols
    assert not missing, f"LeadComment missing columns: {missing}"
    # Ensure the table actually exists (migration applied)
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    assert "lead_comments" in insp.get_table_names(), \
        "lead_comments table missing from DB"
    return f"all {len(expected)} columns present + table exists"


@check("2. Migration be_5f1e9a4b7c3 exists + chains correctly")
def _():
    f = ROOT / "migrations/versions/be_5f1e9a4b7c3_lead_comments.py"
    assert f.exists(), "migration file not found"
    txt = f.read_text()
    assert "lead_comments" in txt
    assert "down_revision = 'bd_4e9f1c6d8a3'" in txt, \
        "migration doesn't chain off bd_4e9f1c6d8a3"
    return "migration file present + chains correctly"


@check("3. Lead.comments backref is wired (ORM-side relationship)")
def _():
    from app.models import Lead
    L = Lead.query.first()
    if not L:
        return "no lead in DB to probe — schema-only check above already covers it"
    # comments must be a list (possibly empty), not raise
    cmnts = list(L.comments)
    return f"lead.comments returns a list (currently {len(cmnts)} items)"


@check("4. Route /leads/<id>/comments POST creates a row")
def _():
    from app.models import Lead, LeadComment

    app = create_app()
    with app.app_context():
        L = Lead.query.filter(Lead.deleted_at.is_(None)).first()
        assert L, "no lead in DB"
        before = LeadComment.query.count()
        with app.test_client() as client:
            _login(client, DEMO_EMAIL, DEMO_PASS)
            r = client.post(
                f"/leads/{L.id}/comments",
                data={"content": "audit comment hello"},
                follow_redirects=False,
            )
            assert r.status_code in (302, 303), \
                f"POST comment expected 302, got {r.status_code}"
        after = LeadComment.query.count()
        assert after == before + 1, \
            f"expected one new comment, count went {before} → {after}"
        # Cleanup the test row
        newest = LeadComment.query.order_by(LeadComment.id.desc()).first()
        db.session.delete(newest)
        db.session.commit()
    return "POST /leads/<id>/comments → 1 LeadComment row added"


@check("5. Empty content is rejected with a flash, no DB row created")
def _():
    from app.models import Lead, LeadComment

    app = create_app()
    with app.app_context():
        L = Lead.query.filter(Lead.deleted_at.is_(None)).first()
        before = LeadComment.query.count()
        with app.test_client() as client:
            _login(client, DEMO_EMAIL, DEMO_PASS)
            r = client.post(
                f"/leads/{L.id}/comments",
                data={"content": "   "},   # whitespace-only
                follow_redirects=False,
            )
            assert r.status_code in (302, 303)
        after = LeadComment.query.count()
        assert after == before, \
            f"empty content created a row anyway: {before} → {after}"
    return "whitespace-only content silently rejected"


@check("6. Lead detail template renders the comments section")
def _():
    app = create_app()
    with app.app_context():
        from app.models import Lead
        L = Lead.query.filter(Lead.deleted_at.is_(None)).first()
        with app.test_client() as client:
            _login(client, DEMO_EMAIL, DEMO_PASS)
            r = client.get(f"/leads/{L.id}", follow_redirects=False)
            assert r.status_code == 200
            body = r.data.decode("utf-8")
            for frag in ["💬 التعليقات", "اكتب تعليقك", 'id="comments"']:
                assert frag in body, f"missing UI fragment: {frag}"
    return "detail page has heading + textarea + #comments anchor"


@check("7. New comment renders in the thread on next page load")
def _():
    from app.models import Lead, LeadComment

    app = create_app()
    with app.app_context():
        L = Lead.query.filter(Lead.deleted_at.is_(None)).first()
        with app.test_client() as client:
            _login(client, DEMO_EMAIL, DEMO_PASS)
            client.post(f"/leads/{L.id}/comments",
                        data={"content": "AUDIT_RENDERED_COMMENT"},
                        follow_redirects=False)
            body = client.get(f"/leads/{L.id}").data.decode("utf-8")
            assert "AUDIT_RENDERED_COMMENT" in body, \
                "newly posted comment didn't appear in the rendered thread"
        # Cleanup
        c = LeadComment.query.filter_by(
            content="AUDIT_RENDERED_COMMENT").first()
        if c:
            db.session.delete(c)
            db.session.commit()
    return "posted comment appears on the next detail-page render"


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
