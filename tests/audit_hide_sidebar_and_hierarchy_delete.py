#!/usr/bin/env python3
"""MARSOUD (Abdelhamid 2026-07-22) — two-in-one bug audit:

  A. /choose-plan renders WITHOUT the sidebar (no navigation escape
     until the owner picks a plan).
  B. Deleting a ProductGroup from /products/hierarchy/groups/<id>/delete
     STICKS — the next visit to /hierarchy no longer auto-recreates the
     'عام' default (that was silently undoing the delete).
"""
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__HS_%__'"))]
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
            "DELETE FROM users WHERE email LIKE 'hs-%@x.test'"))
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id NOT IN "
                    "(SELECT id FROM companies)"))


def _mk_owner_company(suffix):
    from app.models import Company, User, UserStatus
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    c = Company(name=f"__HS_{suffix}__", base_currency="EGP",
                subdomain=f"hs-{suffix.lower()}",
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email=f"hs-{suffix.lower()}@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name=f"hs-{suffix}", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    return u, c


@check("A. /choose-plan renders WITHOUT the sidebar")
def _():
    from flask import current_app
    u, c = _mk_owner_company("PLAN")
    # /choose-plan only fires when intended_plan_id is NULL — which
    # matches a fresh signup.
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
        sess["active_company_id"] = c.id
    r = client.get("/choose-plan")
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)
    # The sidebar has a stable id — its absence is the signal we hid it.
    assert 'id="sidebar"' not in body, \
        "sidebar rendered on /choose-plan (should be hidden)"
    # Also verify the sidebar-navigation strings aren't emitted.
    # The Arabic label 'المشاريع' is a sidebar link, not a page word.
    assert "المشاريع" not in body, "sidebar link leaked into page"
    # Confirm the page body still rendered.
    assert "اختر باقتك" in body
    return "sidebar absent + choose-plan content present"


@check("B. Deleting a ProductGroup sticks — no auto-recreate")
def _():
    from app.models import ProductGroup
    u, c = _mk_owner_company("HIER")
    # This bug lives entirely in _ensure_default_hierarchy — a pure
    # function. Test it at the service level to skip the request-
    # middleware chain (choose-plan / terms / verified-email).
    from app.routes.products import _ensure_default_hierarchy

    # First call — no groups exist, so 'عام' is seeded.
    _ensure_default_hierarchy(c.id)
    db.session.commit()
    g_row = ProductGroup.query.filter_by(
        company_id=c.id, name="عام").first()
    assert g_row, "default 'عام' was not seeded on first call"

    # Owner adds their own group.
    other = ProductGroup(company_id=c.id, name="اختبار")
    db.session.add(other); db.session.commit()

    # Owner deletes 'عام' (via the route logic — same flow as prod).
    db.session.delete(g_row); db.session.commit()

    # A subsequent call MUST NOT recreate 'عام' — that was the bug.
    _ensure_default_hierarchy(c.id)
    db.session.commit()
    remaining = ProductGroup.query.filter_by(company_id=c.id).all()
    names = {g.name for g in remaining}
    assert "عام" not in names, \
        f"'عام' auto-recreated after delete: {names}"
    assert "اختبار" in names, "surviving group vanished"

    # Sanity: a fresh company with ZERO groups still gets the default
    # (so first-time signups don't miss the seed).
    from app.models import Company
    from app.services.seed_coa import seed_default_coa
    fresh = Company(name="__HS_FRESH__", base_currency="EGP",
                     subdomain="hs-fresh")
    db.session.add(fresh); db.session.flush()
    seed_default_coa(fresh.id)
    db.session.commit()
    _ensure_default_hierarchy(fresh.id)
    db.session.commit()
    fresh_g = ProductGroup.query.filter_by(
        company_id=fresh.id, name="عام").first()
    assert fresh_g, "seed didn't fire for a fresh company"
    return f"delete sticks: {names} + fresh company still seeds"


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
            _teardown()
            print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
