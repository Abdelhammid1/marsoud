#!/usr/bin/env python3
"""MARSOUD-TKT-CUSTOMERS-SORT-DATE-FILTER (Abdelhamid 2026-08-31) —
tenant /customers/ page now:
  * Sorts newest first by Customer.created_at (was A→Z by name).
  * Accepts optional ?start_date / ?end_date query params to
    filter the created_at range.

Scope: regular Marsoud tenant view — NOT /admin/companies.

Checks:
  1. Route unchanged (GET /customers/) — invalid dates still 200.
  2. Default (no filter) — rows ordered by created_at DESC.
  3. ?start_date=X&end_date=Y — filters the returned list to
     customers whose created_at falls in the inclusive range.
  4. Only start_date given — filters from that day forward.
  5. Only end_date given — filters up to and including that day.
  6. Invalid date string — silently ignored (page still 200).
  7. Template renders the filter form + preserves values on
     re-submit + shows a clear-filter link only when active.
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


def _boot_with_customers():
    """Create a company + owner user + 5 customers with staggered
    created_at timestamps so we can prove sort + filter both work.
    Returns (email, cid, dict of customer names → id)."""
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan, Customer
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from app.services.subsidiary import ensure_customer_account

    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        "SELECT id FROM companies WHERE name LIKE '__CUST_SF__%'"))]
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
        "DELETE FROM users WHERE email LIKE '%__cust_sf__%'"))
    db.session.commit()

    plan = Plan.query.filter_by(code="__CUST_SF__").first()
    if not plan:
        plan = Plan(code="__CUST_SF__", name="C", name_ar="C",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales"])
        db.session.add(plan); db.session.flush()

    c = Company(name="__CUST_SF__co", base_currency="EGP",
                subdomain="custsf", plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.commit()
    seed_default_coa(c.id); db.session.commit()

    try:
        from app.services.legal import get_terms_version
        tv = get_terms_version() or "audit"
    except Exception:
        tv = "audit"

    u = User(email="user__cust_sf__@x.io", full_name="Cust SF",
             is_active=True, email_verified_at=datetime.utcnow(),
             terms_version=tv, terms_accepted_at=datetime.utcnow())
    u.set_password("pw12345678")
    db.session.add(u); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()

    # 5 customers with staggered created_at so date filters have
    # measurable buckets:
    #   OLDEST  → 2026-08-01
    #   OLD     → 2026-08-05
    #   MIDDLE  → 2026-08-15
    #   RECENT  → 2026-08-25
    #   NEWEST  → 2026-08-31
    stamps = {
        "OLDEST": datetime(2026, 8, 1, 10, 0),
        "OLD":    datetime(2026, 8, 5, 10, 0),
        "MIDDLE": datetime(2026, 8, 15, 10, 0),
        "RECENT": datetime(2026, 8, 25, 10, 0),
        "NEWEST": datetime(2026, 8, 31, 10, 0),
    }
    ids = {}
    for name, ts in stamps.items():
        cust = Customer(company_id=c.id, name=name, created_at=ts)
        db.session.add(cust); db.session.flush()
        ensure_customer_account(cust)
        ids[name] = cust.id
    db.session.commit()
    return u.email, c.id, ids


def _teardown():
    from sqlalchemy import text, inspect
    from app import db
    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        "SELECT id FROM companies WHERE name LIKE '__CUST_SF__%'"))]
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
        "DELETE FROM users WHERE email LIKE '%__cust_sf__%'"))
    db.session.commit()


def _order_of_names_in(html):
    """Return the customer names in the exact order they appear in
    the rendered HTML — proves the ORDER BY reached the template."""
    from re import findall
    names = ["NEWEST", "RECENT", "MIDDLE", "OLD", "OLDEST"]
    positions = [(name, html.find(">" + name + "<")) for name in names]
    positions = [(name, pos) for name, pos in positions if pos >= 0]
    positions.sort(key=lambda p: p[1])
    return [name for name, _ in positions]


@check("1. GET /customers/ endpoint stable, invalid date → 200")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cid, ids = _boot_with_customers()
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.get("/customers/?start_date=NOT-A-DATE")
                assert r.status_code == 200, \
                    f"invalid date should not crash the page; got {r.status_code}"
            return "route stable + tolerant of garbage dates"
        finally:
            _teardown()


@check("2. default sort → newest first (DESC by created_at)")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cid, ids = _boot_with_customers()
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.get("/customers/")
                html = r.data.decode("utf-8")
            order = _order_of_names_in(html)
            expected = ["NEWEST", "RECENT", "MIDDLE", "OLD", "OLDEST"]
            assert order == expected, \
                f"default order wrong;\n  got:      {order}\n  expected: {expected}"
            return "newest-first sort applied"
        finally:
            _teardown()


@check("3. range filter → only customers inside inclusive range")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cid, ids = _boot_with_customers()
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.get(
                    "/customers/?start_date=2026-08-10&end_date=2026-08-25")
                html = r.data.decode("utf-8")
            order = _order_of_names_in(html)
            expected = ["RECENT", "MIDDLE"]   # inclusive both ends
            assert order == expected, \
                f"range filter wrong;\n  got:      {order}\n  expected: {expected}"
            return "inclusive-both-ends filter behaves"
        finally:
            _teardown()


@check("4. start_date only → customers from that day forward")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cid, ids = _boot_with_customers()
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.get("/customers/?start_date=2026-08-20")
                html = r.data.decode("utf-8")
            order = _order_of_names_in(html)
            # NEWEST (Aug 31) + RECENT (Aug 25) qualify; MIDDLE, OLD,
            # OLDEST are before Aug 20 so excluded.
            assert order == ["NEWEST", "RECENT"], \
                f"start-only filter wrong; got {order}"
            return "start-only filter behaves"
        finally:
            _teardown()


@check("5. end_date only → customers up to and including that day")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cid, ids = _boot_with_customers()
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.get("/customers/?end_date=2026-08-15")
                html = r.data.decode("utf-8")
            order = _order_of_names_in(html)
            # MIDDLE (Aug 15) is the newest that qualifies; OLD, OLDEST
            # also qualify. NEWEST, RECENT excluded.
            assert order == ["MIDDLE", "OLD", "OLDEST"], \
                f"end-only filter wrong; got {order}"
            return "end-only + end-inclusive-of-full-day works"
        finally:
            _teardown()


@check("6. invalid dates → all rows returned (silent ignore)")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cid, ids = _boot_with_customers()
        try:
            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.get(
                    "/customers/?start_date=abc&end_date=2026-99-99")
                html = r.data.decode("utf-8")
            order = _order_of_names_in(html)
            # Fell back to no-filter → all 5, in newest-first order
            assert order == ["NEWEST", "RECENT", "MIDDLE", "OLD", "OLDEST"], \
                f"invalid dates should be ignored; got {order}"
            return "invalid dates silently ignored"
        finally:
            _teardown()


@check("7. template: form + preserved values + clear link on active")
def _():
    src = _strip_comments(_read("app/templates/customers/index.html"))
    # form with GET method
    assert re.search(r'<form[^>]*method="GET"', src), \
        "filter form must be method=GET so URLs are bookmarkable"
    assert 'name="start_date"' in src and 'name="end_date"' in src, \
        "form must expose both start_date and end_date inputs"
    # values preserved on re-render
    assert 'value="{{ start_date or \'\' }}"' in src, \
        "start_date input must round-trip its current value"
    assert 'value="{{ end_date or \'\' }}"' in src, \
        "end_date input must round-trip its current value"
    # clear link renders only when a filter is active
    assert "start_date or end_date" in src, \
        "clear link must be gated on either date being present"
    return "form + roundtrip + gated clear link all wired"


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
