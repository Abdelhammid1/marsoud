#!/usr/bin/env python3
"""MARSOUD-TKT-USERS-SEARCH-DATE-PHONE (Abdelhamid 2026-08-31) —
super-admin /admin/users page:
  * Search box now matches name / email / PHONE / linked COMPANY
    name (was name + email only).
  * Optional start_date / end_date range on User.created_at.
  * New phone column in the row table.

Checks:
  1. GET /admin/users stable + tolerant of garbage dates.
  2. Search matches by full_name.
  3. Search matches by email (regression).
  4. Search matches by phone (NEW).
  5. Search matches by linked company name (NEW).
  6. Date range narrows the list by User.created_at.
  7. Template renders the phone column header + cell + preserves
     start/end date values on re-render.
"""
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta

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


def _strip_comments(src):
    return re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)


def _boot_users_fixture():
    """4 users with distinct name/phone/company shapes so we can prove
    every search dimension works. Also one superadmin to log in as.
    Returns (superadmin_email, [{email, name, phone, co_name}, ...])."""
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        "SELECT id FROM companies WHERE name LIKE '__USR_SDP__%'"))]
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
        "DELETE FROM users WHERE email LIKE '%__usr_sdp__%'"))
    db.session.commit()

    plan = Plan.query.filter_by(code="__USR_SDP__").first()
    if not plan:
        plan = Plan(code="__USR_SDP__", name="X", name_ar="X",
                    allowed_subitems=None)
        plan.set_modules(["accounting"])
        db.session.add(plan); db.session.flush()

    try:
        from app.services.legal import get_terms_version
        tv = get_terms_version() or "audit"
    except Exception:
        tv = "audit"

    # superadmin to log in as
    sa = User(email="superadmin__usr_sdp__@x.io", full_name="SA",
              is_active=True, email_verified_at=datetime.utcnow(),
              terms_version=tv, terms_accepted_at=datetime.utcnow(),
              is_superadmin=True)
    sa.set_password("pw12345678")
    db.session.add(sa); db.session.commit()

    # 4 users with staggered created_at + distinct shapes
    layout = [
        # (email, full_name, phone, company_name, created_at)
        ("alpha__usr_sdp__@x.io",   "أحمد الأول",  "+201110000001",
         "شركة ألفا",     datetime(2026, 8, 1, 10, 0)),
        ("beta__usr_sdp__@x.io",    "بيتا يوسف",    "+201220000002",
         "بيتا مؤسسة",    datetime(2026, 8, 15, 10, 0)),
        ("gamma__usr_sdp__@x.io",   "جاما محمد",    "+201330000003",
         "جاما إنتربرايز", datetime(2026, 8, 25, 10, 0)),
        ("delta__usr_sdp__@x.io",   "دلتا سالم",   "+201440000004",
         "دلتا هولدنج",   datetime(2026, 8, 31, 10, 0)),
    ]
    results = []
    for email, name, phone, co_name, ts in layout:
        u = User(email=email, full_name=name, phone=phone, is_active=True,
                 email_verified_at=datetime.utcnow(),
                 terms_version=tv, terms_accepted_at=datetime.utcnow(),
                 created_at=ts)
        u.set_password("pw12345678")
        db.session.add(u); db.session.commit()

        c = Company(name=co_name, base_currency="EGP",
                    subdomain=email.split("@")[0][:20].replace("__", "-"),
                    plan_id=plan.id,
                    intended_plan_id=plan.id,
                    subscription_started_at=datetime.utcnow(),
                    subscription_expires_at=datetime(2999, 1, 1))
        db.session.add(c); db.session.commit()
        seed_default_coa(c.id); db.session.commit()

        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=c.id, role="owner"))
        db.session.commit()
        results.append({"email": email, "name": name, "phone": phone,
                        "co_name": co_name, "id": u.id})
    return sa.email, results


def _teardown():
    from sqlalchemy import text, inspect
    from app import db
    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        "SELECT id FROM companies WHERE name LIKE '__USR_SDP__%' "
        "OR name LIKE 'شركة ألفا' OR name LIKE 'بيتا مؤسسة' "
        "OR name LIKE 'جاما إنتربرايز' OR name LIKE 'دلتا هولدنج'"))]
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
        "DELETE FROM users WHERE email LIKE '%__usr_sdp__%'"))
    db.session.commit()


@check("1. GET /admin/users stable + invalid date → 200")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        sa_email, _ = _boot_users_fixture()
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": sa_email, "password": "pw12345678"})
                r = client.get("/admin/users?start_date=NOT-A-DATE")
                assert r.status_code == 200, \
                    f"garbage date must not 500; got {r.status_code}"
            return "route stable + tolerant of garbage dates"
        finally:
            _teardown()


@check("2. search matches by full_name")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        sa_email, users = _boot_users_fixture()
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": sa_email, "password": "pw12345678"})
                r = client.get("/admin/users?q=دلتا")
                html = r.data.decode("utf-8")
            assert "دلتا سالم" in html, "search by name should find it"
            assert "أحمد الأول" not in html, "should exclude non-matches"
            return "name search works"
        finally:
            _teardown()


@check("3. search matches by email (regression)")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        sa_email, users = _boot_users_fixture()
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": sa_email, "password": "pw12345678"})
                r = client.get("/admin/users?q=beta__usr_sdp__")
                html = r.data.decode("utf-8")
            assert "beta__usr_sdp__@x.io" in html
            assert "delta__usr_sdp__@x.io" not in html
            return "email search still works"
        finally:
            _teardown()


@check("4. search matches by phone (NEW)")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        sa_email, users = _boot_users_fixture()
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": sa_email, "password": "pw12345678"})
                # Search for a partial phone
                r = client.get("/admin/users?q=201330000003")
                html = r.data.decode("utf-8")
            assert "جاما محمد" in html, \
                "phone search should surface the matching user"
            assert "أحمد الأول" not in html, "other users should be filtered out"
            return "phone search works end-to-end"
        finally:
            _teardown()


@check("5. search matches by linked company name (NEW)")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        sa_email, users = _boot_users_fixture()
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": sa_email, "password": "pw12345678"})
                r = client.get("/admin/users?q=إنتربرايز")
                html = r.data.decode("utf-8")
            # "جاما إنتربرايز" is Gamma's company — only the user
            # linked to that company should surface.
            assert "جاما محمد" in html, \
                "search should match on the linked company name"
            assert "بيتا يوسف" not in html
            return "company-name search works"
        finally:
            _teardown()


@check("6. date range narrows the list by User.created_at")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        sa_email, users = _boot_users_fixture()
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": sa_email, "password": "pw12345678"})
                r = client.get(
                    "/admin/users?start_date=2026-08-10&end_date=2026-08-27")
                html = r.data.decode("utf-8")
            # Expected in-range: بيتا (Aug 15), جاما (Aug 25).
            # Expected out: أحمد (Aug 1), دلتا (Aug 31).
            assert "بيتا يوسف" in html and "جاما محمد" in html
            assert "أحمد الأول" not in html and "دلتا سالم" not in html
            return "date range narrows the user list"
        finally:
            _teardown()


@check("7. template: phone column + preserved date inputs + clear link")
def _():
    src = _strip_comments(_read("app/templates/admin/users.html"))
    assert "<th>رقم الجوال</th>" in src, \
        "phone column header must render"
    assert "{{ u.phone or '—' }}" in src, \
        "phone cell must render u.phone with a fallback"
    # Filter form
    assert 'name="start_date"' in src, "start_date input missing"
    assert 'name="end_date"' in src, "end_date input missing"
    assert 'value="{{ start_date or \'\' }}"' in src, \
        "start_date input must round-trip its value"
    assert 'value="{{ end_date or \'\' }}"' in src, \
        "end_date input must round-trip"
    # Clear link only when a filter is active
    assert "q or start_date or end_date" in src, \
        "clear link must be gated on any filter being present"
    # Empty-state colspan bumped from 6 → 7
    assert 'colspan="7"' in src, \
        "empty-state colspan must be 7 after adding the phone column"
    return "phone column + date inputs + clear link + colspan all correct"


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
