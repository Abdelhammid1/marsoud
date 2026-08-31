#!/usr/bin/env python3
"""MARSOUD-TKT-ADMIN-OWNER-COL (Abdelhamid 2026-08-31) — add an owner
/ contact-person column to two subscriber-style lists:

  * Super-admin `/admin/companies` — new "المالك" column between the
    company name and the created-at column. Renders the tenant's
    owner as a link to /admin/users/<id>.
  * Regular tenant `/customers/` — new "الشخص المسؤول" column between
    the customer name and email. Renders Customer.contact_person as
    a link to the existing /customers/<id> view page.

Checks:
  1. Customer model has the new nullable `contact_person` column.
  2. Migration 02e70940195c registered + chained to c68a8e80606b.
  3. Super-admin template renders the "المالك" header + owner cell
     with a link to superadmin.user_detail.
  4. Tenant customers template renders the "الشخص المسؤول" header
     + contact_person cell + customer name is now a link.
  5. companies_with_stats() returns an `owner` field per row that
     is either the owner User or None.
  6. End-to-end: create + save a customer with contact_person via
     /customers/new — persists, appears on the list.
  7. Edit persists contact_person via /customers/<id>/edit.
"""
import os
import re
import sys
from pathlib import Path

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


def _strip_jinja_comments(src):
    return re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)


def _boot_fixture(prefix, role_name):
    from datetime import datetime
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

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
        plan = Plan(code=f"__{prefix}__", name="Audit", name_ar="تدقيق",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "settings"])
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
        user_id=u.id, company_id=c.id, role=role_name))
    db.session.commit()
    return u.email, c.id, u.id


def _teardown_fixture(prefix):
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


@check("1. Customer.contact_person column exists + nullable")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        from app.models import Customer
        col = Customer.__table__.c.get("contact_person")
        assert col is not None, \
            "Customer.contact_person column is missing"
        assert col.nullable is True, \
            "contact_person must be nullable — individuals leave it blank"
        assert "VARCHAR" in str(col.type).upper(), \
            f"contact_person should be a String; got {col.type}"
    return "column present + nullable string"


@check("2. migration 02e70940195c chained to c68a8e80606b + idempotent")
def _():
    src = _read("migrations/versions/02e70940195c_customer_contact_person.py")
    assert 'revision = "02e70940195c"' in src, "wrong revision id"
    assert 'down_revision = "c68a8e80606b"' in src, \
        "must chain from c68a8e80606b (current head at the time)"
    assert "_has_col" in src, \
        "migration must be idempotent for re-runs on partial histories"
    return "revision chained + idempotent guard present"


@check("3. admin/companies.html renders 'المالك' header + owner link")
def _():
    src = _strip_jinja_comments(
        _read("app/templates/admin/companies.html"))
    assert "<th>المالك</th>" in src, \
        "admin/companies.html missing the 'المالك' header"
    assert "superadmin.user_detail" in src, \
        "owner cell must link to superadmin.user_detail"
    assert "r.owner" in src, \
        "owner cell must read from r.owner (set by companies_with_stats)"
    # And the empty-state colspan must have grown by 1 (was 9, now 10)
    assert 'colspan="10"' in src, \
        "empty-state colspan must be 10 after adding the owner column"
    return "header + link + owner + colspan all correct"


@check("4. customers/index.html renders 'الشخص المسؤول' + name is a link")
def _():
    src = _strip_jinja_comments(
        _read("app/templates/customers/index.html"))
    assert "<th>الشخص المسؤول</th>" in src, \
        "customers/index.html missing the 'الشخص المسؤول' header"
    assert "c.contact_person" in src, \
        "contact_person value must be rendered per row"
    # Name should now be a link
    m = re.search(
        r'font-semibold[^>]*>\s*<a href="{{ url_for\(\'customers\.view\'',
        src, re.DOTALL)
    assert m, "customer name is not a link to customers.view"
    # And the empty-state colspan grew from 5 to 6
    assert 'colspan="6"' in src, \
        "empty-state colspan must be 6 after adding the contact column"
    return "header + link + colspan all correct"


@check("5. companies_with_stats returns 'owner' per row (User or None)")
def _():
    from app import create_app, db
    from app.services.superadmin import companies_with_stats
    from app.models import User

    app = create_app()
    with app.app_context():
        email, cid, uid = _boot_fixture("OCOL_S1", "owner")
        try:
            rows = companies_with_stats()
            match = [r for r in rows if r["company"].id == cid]
            assert match, f"fixture company_id={cid} not in stats rows"
            r = match[0]
            assert "owner" in r, "row missing 'owner' key"
            assert isinstance(r["owner"], User), \
                f"owner should be a User; got {type(r['owner']).__name__}"
            assert r["owner"].id == uid, \
                f"owner.id should be {uid}; got {r['owner'].id}"
            return "owner user resolved correctly"
        finally:
            _teardown_fixture("OCOL_S1")


@check("6. POST /customers/new persists contact_person")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        from app.models import Customer
        email, cid, uid = _boot_fixture("OCOL_T1", "owner")
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.post("/customers/new", data={
                    "name": "شركة العميل",
                    "contact_person": "أحمد المسؤول",
                    "email": "co@example.com",
                    "phone": "01000000000",
                }, follow_redirects=False)
                assert r.status_code in (302, 303), \
                    f"expected redirect after create; got {r.status_code}"

            c = Customer.query.filter_by(
                company_id=cid, name="شركة العميل").first()
            assert c, "customer row not persisted"
            assert c.contact_person == "أحمد المسؤول", \
                f"contact_person round-trip broken; got {c.contact_person!r}"

            # And the list page renders both fields with the customer's link
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.get("/customers/")
                assert r.status_code == 200
                html = r.data.decode("utf-8")
                assert "شركة العميل" in html
                assert "أحمد المسؤول" in html
                # Name must render as a link to the view page
                view_url = f"/customers/{c.id}"
                assert view_url in html, \
                    "customer view URL not present as a link on the list"
            return "create + persist + list rendering all work"
        finally:
            _teardown_fixture("OCOL_T1")


@check("7. POST /customers/<id>/edit persists contact_person")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        from app.models import Customer
        email, cid, uid = _boot_fixture("OCOL_T2", "owner")
        try:
            c = Customer(company_id=cid, name="عميل قديم",
                         contact_person=None)
            db.session.add(c); db.session.commit()
            cust_id = c.id

            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.post(f"/customers/{cust_id}/edit", data={
                    "name": "عميل قديم",
                    "contact_person": "المسؤول الجديد",
                }, follow_redirects=False)
                assert r.status_code in (302, 303)

            db.session.refresh(c)
            assert c.contact_person == "المسؤول الجديد", \
                f"edit did not save contact_person; got {c.contact_person!r}"

            # Editing with blank contact_person clears it
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                client.post(f"/customers/{cust_id}/edit", data={
                    "name": "عميل قديم",
                    "contact_person": "",
                })
            db.session.refresh(c)
            assert c.contact_person is None, \
                f"blank should clear contact_person; got {c.contact_person!r}"
            return "edit persists + blank clears"
        finally:
            _teardown_fixture("OCOL_T2")


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
