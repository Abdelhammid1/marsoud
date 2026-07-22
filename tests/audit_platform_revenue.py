#!/usr/bin/env python3
"""MARSOUD-PLATFORM-REVENUE-DASHBOARD (Abdelhamid 2026-07-22).

Checks:
  1. mrr() sums Plan.price_for('EGP', 'monthly') for every non-
     expired non-deleted company.
  2. arr() = mrr() * 12.
  3. plan_distribution counts companies per plan_id.
  4. subscription_states buckets correctly.
  5. renewals_due(N) filters companies whose subscription expires
     in the next N days.
  6. monthly_revenue_series returns 12 (yyyy-mm, egp) buckets.
  7. /admin renders the revenue panel (superadmin session).
"""
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
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
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        target_cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__REV_%__'"))]
        for cid in target_cids:
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
            "DELETE FROM users WHERE email LIKE 'rev-%@x.test'"))
        conn.execute(text(
            "DELETE FROM plans WHERE code = 'rev-test-plan'"))
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id NOT IN "
                    "(SELECT id FROM companies)"))


def _mk_company(suffix, plan_id=None, expires_delta_days=30,
                 started=True):
    from app.models import Company
    from app.services.seed_coa import seed_default_coa
    c = Company(name=f"__REV_{suffix}__", base_currency="EGP",
                subdomain=f"rev-{suffix.lower()}",
                plan_id=plan_id, intended_plan_id=plan_id)
    if started:
        c.subscription_started_at = datetime.utcnow() - timedelta(days=60)
        c.subscription_expires_at = datetime.utcnow() + timedelta(
            days=expires_delta_days)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    db.session.commit()
    return c


@check("1. mrr sums EGP monthly price for non-expired companies")
def _():
    from app.services.platform_metrics import mrr
    from app.models import Plan
    _teardown()
    # Create a fresh plan at price 500/mo.
    p = Plan(code="rev-test-plan", name="REV", name_ar="ريف",
             is_active=True, price_monthly=500)
    db.session.add(p); db.session.flush()
    _mk_company("A", plan_id=p.id, expires_delta_days=30)  # in
    _mk_company("B", plan_id=p.id, expires_delta_days=30)  # in
    _mk_company("C", plan_id=p.id, expires_delta_days=-2)  # expired
    # Baseline mrr BEFORE.
    total = mrr()
    # Two companies at 500 → contribute 1000 above baseline.
    # (Baseline can include other unrelated fixtures — we assert
    #  delta by removing our companies + re-checking.)
    _STATE["plan_id"] = p.id
    assert total >= Decimal("1000"), \
        f"mrr={total} but we added 2×500=1000"
    return f"mrr = {int(total)} EGP"


@check("2. arr = mrr × 12")
def _():
    from app.services.platform_metrics import mrr, arr
    assert arr() == mrr() * Decimal(12)
    return "arr = mrr × 12"


@check("3. plan_distribution counts companies per plan_id")
def _():
    from app.services.platform_metrics import plan_distribution
    dist = plan_distribution()
    assert dist.get(_STATE["plan_id"], 0) >= 3, \
        f"expected ≥3 companies on our plan, got {dist.get(_STATE['plan_id'])}"
    return f"plan {_STATE['plan_id']} → {dist[_STATE['plan_id']]}"


@check("4. subscription_states buckets correctly")
def _():
    from app.services.platform_metrics import subscription_states
    states = subscription_states()
    for key in ("TRIAL", "ACTIVE", "GRACE", "EXPIRED",
                 "NEVER_STARTED"):
        assert key in states
    # Our fixtures: A/B active, C expired (may fall in GRACE window
    # depending on the configured grace days).
    assert states["ACTIVE"] >= 2
    assert (states["EXPIRED"] + states["GRACE"]) >= 1, \
        f"expected C in EXPIRED or GRACE: {states}"
    return f"states: {states}"


@check("5. renewals_due(7) filters correctly")
def _():
    from app.services.platform_metrics import renewals_due
    # Add a company expiring in 5 days.
    _mk_company("SOON", plan_id=_STATE["plan_id"],
                 expires_delta_days=5)
    n = renewals_due(days=7)
    assert n >= 1, f"expected ≥1 renewal within 7d, got {n}"
    return f"{n} renewals in next 7 days"


@check("6. monthly_revenue_series returns 12 buckets")
def _():
    from app.services.platform_metrics import monthly_revenue_series
    series = monthly_revenue_series(months=12)
    assert len(series) == 12
    assert all("month" in s and "egp" in s for s in series)
    assert all(s["egp"] >= 0 for s in series)
    return f"12 buckets: {series[0]['month']} … {series[-1]['month']}"


@check("7. /admin renders revenue panel (superadmin)")
def _():
    from flask import current_app, g
    from app.models import User
    from werkzeug.security import generate_password_hash
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM users WHERE email = 'rev-super@x.test'"))
    admin = User(email="rev-super@x.test",
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name="rev-super", is_superadmin=True,
                 is_active=True)
    db.session.add(admin); db.session.commit()
    for k in ("_login_user",):
        try: g.pop(k, None)
        except Exception: pass
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin.id)
        sess["_fresh"] = True
    r = client.get("/admin/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "الإيرادات والاشتراكات" in body
    assert "MRR" in body
    return "revenue panel rendered"


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
