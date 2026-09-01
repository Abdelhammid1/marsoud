#!/usr/bin/env python3
"""MARSOUD-TKT-CUSTOMER-COMMENTS-NOTES (Abdelhamid 2026-08-31) —
two internal-only surfaces on /customers/<id>:
  * Comments — threaded discussion for the team (partners.manage
    gated), mirroring TaskComment's shape.
  * Notes — free-text log with author + timestamp, newest first.

Both never render on the customer portal.

Checks:
  1. Models CustomerComment + CustomerNote exist + carry
     customer_id / company_id / user_id / content / created_at.
  2. Migration 8a63ad9bca7e registered and chained.
  3. Routes registered: POST add/delete for both comments + notes.
  4. Add comment via HTTP persists a row + redirects to
     /customers/<id>#comments.
  5. Add note via HTTP persists a row + redirects to
     /customers/<id>#notes.
  6. Blank content is refused (flash error, no row created).
  7. Delete comment removes the row.
  8. Viewer (no partners.manage) cannot access the endpoints —
     rides the same require_permission gate as the rest of the
     write actions on customers.
  9. Template renders both sections + the "لا يظهر للعميل" badge.
"""
import os
import re
import sys
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


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _boot(prefix, role):
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan, Customer
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from app.services.subsidiary import ensure_customer_account

    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        f"SELECT id FROM companies WHERE name LIKE '__{prefix}__%'"))]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        f"DELETE FROM users WHERE email LIKE '%__{prefix.lower()}__%'"))
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="Audit", name_ar="A",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales"])
        db.session.add(plan); db.session.flush()

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
    u = User(email=f"user__{prefix.lower()}__@x.io",
             full_name=f"User {prefix}",
             is_active=True, email_verified_at=datetime.utcnow(),
             terms_version=tv, terms_accepted_at=datetime.utcnow())
    u.set_password("pw12345678")
    db.session.add(u); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role=role))
    db.session.commit()

    cust = Customer(company_id=c.id, name="عميل الاختبار")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust); db.session.commit()
    return u.email, c.id, u.id, cust.id


def _teardown(prefix):
    from sqlalchemy import text, inspect
    from app import db
    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        f"SELECT id FROM companies WHERE name LIKE '__{prefix}__%'"))]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        f"DELETE FROM users WHERE email LIKE '%__{prefix.lower()}__%'"))
    db.session.commit()


@check("1. models exist + carry the expected columns")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        from app.models import CustomerComment, CustomerNote
        for model in (CustomerComment, CustomerNote):
            cols = {c.name for c in model.__table__.columns}
            required = {"id", "customer_id", "company_id", "user_id",
                        "content", "created_at"}
            missing = required - cols
            assert not missing, \
                f"{model.__name__} missing columns: {missing}"
    return "both models present with the 6 required columns"


@check("2. migration 8a63ad9bca7e registered + chained")
def _():
    src = _read("migrations/versions/"
                "8a63ad9bca7e_customer_comments_notes.py")
    assert 'revision = "8a63ad9bca7e"' in src
    assert 'down_revision = "5e44c9a13a1c"' in src, \
        "must chain from 5e44c9a13a1c (previous ticket 7 migration)"
    return "migration properly chained"


@check("3. routes registered (add + delete for comments + notes)")
def _():
    from app import create_app
    app = create_app()
    endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    for name in ("customers.add_comment", "customers.delete_comment",
                 "customers.add_note", "customers.delete_note"):
        assert name in endpoints, f"missing endpoint: {name}"
    return "all 4 write routes registered"


@check("4. POST /customers/<id>/comments persists + redirects to #comments")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        from app.models import CustomerComment
        email, cid, uid, cust_id = _boot("CCN1", "owner")
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.post(
                    f"/customers/{cust_id}/comments",
                    data={"content": "أول تعليق داخلي"},
                    follow_redirects=False)
                assert r.status_code in (302, 303), \
                    f"expected redirect; got {r.status_code}"
                assert "#comments" in r.location, \
                    f"redirect should anchor to #comments; got {r.location}"

            rows = CustomerComment.query.filter_by(customer_id=cust_id).all()
            assert len(rows) == 1
            assert rows[0].content == "أول تعليق داخلي"
            assert rows[0].user_id == uid
            return "comment persisted + anchored redirect"
        finally:
            _teardown("CCN1")


@check("5. POST /customers/<id>/notes persists + redirects to #notes")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        from app.models import CustomerNote
        email, cid, uid, cust_id = _boot("CCN2", "owner")
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.post(
                    f"/customers/{cust_id}/notes",
                    data={"content": "ملاحظة: يفضل الدفع نقداً"},
                    follow_redirects=False)
                assert r.status_code in (302, 303)
                assert "#notes" in r.location

            rows = CustomerNote.query.filter_by(customer_id=cust_id).all()
            assert len(rows) == 1
            assert rows[0].content == "ملاحظة: يفضل الدفع نقداً"
            return "note persisted + anchored redirect"
        finally:
            _teardown("CCN2")


@check("6. blank content is refused (flash error, no row created)")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        from app.models import CustomerComment, CustomerNote
        email, cid, uid, cust_id = _boot("CCN3", "owner")
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                # Blank comment
                r = client.post(
                    f"/customers/{cust_id}/comments",
                    data={"content": "   "})
                assert r.status_code in (302, 303)
                # Blank note
                r = client.post(
                    f"/customers/{cust_id}/notes",
                    data={"content": ""})
                assert r.status_code in (302, 303)
            assert CustomerComment.query.filter_by(customer_id=cust_id).count() == 0
            assert CustomerNote.query.filter_by(customer_id=cust_id).count() == 0
            return "blank content correctly rejected"
        finally:
            _teardown("CCN3")


@check("7. delete comment removes the row")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        from app.models import CustomerComment
        email, cid, uid, cust_id = _boot("CCN4", "owner")
        try:
            cm = CustomerComment(customer_id=cust_id, company_id=cid,
                                 user_id=uid, content="سيتم حذفه")
            db.session.add(cm); db.session.commit()
            cm_id = cm.id

            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.post(
                    f"/customers/{cust_id}/comments/{cm_id}/delete")
                assert r.status_code in (302, 303)
            assert db.session.get(CustomerComment, cm_id) is None
            return "comment deleted"
        finally:
            _teardown("CCN4")


@check("8. viewer role (no partners.manage) cannot add comments")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        from app.models import CustomerComment
        email, cid, uid, cust_id = _boot("CCN5", "viewer")
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.post(
                    f"/customers/{cust_id}/comments",
                    data={"content": "viewer trying"},
                    follow_redirects=False)
                # require_permission bounces via redirect to dashboard
                # — key thing is the row was NOT created.
                assert r.status_code in (302, 303)
            assert CustomerComment.query.filter_by(customer_id=cust_id).count() == 0, \
                "viewer must not be able to write comments"
            return "viewer blocked by partners.manage gate"
        finally:
            _teardown("CCN5")


@check("9. template renders both sections + hidden-from-customer badge")
def _():
    src = _read("app/templates/customers/view.html")
    # Two sections
    assert 'id="comments"' in src and 'id="notes"' in src, \
        "template must render both #comments and #notes sections"
    # Internal-only badges — both surfaces are labeled as hidden
    # from the customer, matching the ticket's AC.
    assert src.count("لا يظهر للعميل") >= 2, \
        "both sections should carry the 'لا يظهر للعميل' badge"
    # Permission gate wraps the whole block
    assert "has_permission('partners.manage')" in src, \
        "both sections must sit inside the partners.manage gate"
    return "template shape matches the ticket contract"


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
