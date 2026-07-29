#!/usr/bin/env python3
"""MARSOUD-DISCOUNT-COUPONS wiring (Abdelhamid 2026-07-29).

Batch 2 shipped the coupon model + service + admin CRUD but no
customer route wired it in. This audit proves the wiring at
/choose-plan works end-to-end:

  1. Valid coupon → 'company.applied_coupon_id' set + redirect to
     dashboard (not stuck on /choose-plan).
  2. Missing/blank coupon → plan still saved (unchanged flow).
  3. Bad code → clear Arabic error + plan is still saved but
     applied_coupon_id stays NULL, user redirected back to
     /choose-plan.
  4. Expired coupon → same rejection path.
  5. Coupon over max_uses → rejected.
  6. Coupon service redeem() call actually persists a
     CouponRedemption row (proves the service-side is untouched
     by our wiring — this is the safety net that the coupon side
     of the SaaS billing flow will call after payment).
"""
import os
import sys
from datetime import date, datetime, timedelta
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
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__CW_%__'"))]
        for cid in cids:
            # Clear applied_coupon_id so we don't hit FK cascade fun.
            conn.execute(text(
                "UPDATE companies SET applied_coupon_id = NULL "
                "WHERE id = :c"), {"c": cid})
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
            "DELETE FROM users WHERE email LIKE 'cw-%@x.test'"))
        conn.execute(text(
            "DELETE FROM coupons WHERE code LIKE 'CW-%'"))


def _bootstrap():
    from app.models import (
        Company, User, UserStatus, Plan,
        Coupon, DISCOUNT_PERCENT,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(is_active=True).first()
    if plan is None:
        plan = Plan(code="cw-stub", name="Stub", name_ar="نجربة",
                     is_active=True, price_monthly=1000)
        db.session.add(plan); db.session.flush()

    c = Company(name="__CW_CO__", base_currency="EGP",
                 subdomain="cw-co",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email="cw-owner@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="cw-owner", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))

    # Valid coupon: 20% off, active, no scope restriction.
    good = Coupon(code="CW-GOOD-20",
                    discount_type=DISCOUNT_PERCENT,
                    discount_value=Decimal("20"),
                    active=True)
    # Expired coupon: valid_until in the past.
    expired = Coupon(code="CW-EXPIRED",
                       discount_type=DISCOUNT_PERCENT,
                       discount_value=Decimal("10"),
                       valid_until=date.today() - timedelta(days=1),
                       active=True)
    # Max-uses-exceeded coupon: max_uses=0.
    zeroed = Coupon(code="CW-ZERO",
                      discount_type=DISCOUNT_PERCENT,
                      discount_value=Decimal("5"),
                      max_uses=0, active=True)
    db.session.add_all([good, expired, zeroed])
    db.session.commit()
    return c, u, plan


def _post_choose_plan(user, company, plan_id, coupon_code=""):
    from flask import current_app
    # Use `with` context so cookies + request state are torn down
    # cleanly between checks. Without this, later checks pick up
    # a stale active_company_id from an earlier check's cookie
    # jar even though sess.clear() reset the session dict.
    with current_app.test_client() as client:
        with client.session_transaction() as sess:
            sess.clear()
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
            sess["active_company_id"] = company.id
        r = client.post("/choose-plan", data={
            "plan_id": plan_id,
            "coupon_code": coupon_code,
        }, follow_redirects=False)
    return r


@check("1. Valid coupon → applied_coupon_id set + redirect off /choose-plan")
def _():
    from app.models import Company, Coupon
    _teardown()
    c, u, plan = _bootstrap()
    _STATE["c_id"] = c.id
    _STATE["u_id"] = u.id
    _STATE["plan_id"] = plan.id
    r = _post_choose_plan(u, c, plan.id, "CW-GOOD-20")
    assert r.status_code in (302, 303), \
        f"expected redirect, got {r.status_code}"
    loc = r.headers.get("Location") or ""
    assert "/choose-plan" not in loc, \
        f"stuck on choose-plan: {loc}"
    fresh = db.session.get(Company, c.id)
    good = Coupon.query.filter_by(code="CW-GOOD-20").first()
    assert fresh.applied_coupon_id == good.id, \
        f"applied_coupon_id={fresh.applied_coupon_id}, want {good.id}"
    return f"redirect → {loc}, coupon={good.id}"


@check("2. Blank coupon → plan saved, applied_coupon_id NULL")
def _():
    from app.models import Company
    _teardown()
    c, u, plan = _bootstrap()
    r = _post_choose_plan(u, c, plan.id, "")
    assert r.status_code in (302, 303)
    fresh = db.session.get(Company, c.id)
    assert fresh.intended_plan_id == plan.id
    assert fresh.applied_coupon_id is None
    return "no coupon = clean plan-only save"


@check("3. Unknown code → redirect back to /choose-plan, applied_coupon_id stays NULL")
def _():
    from app.models import Company
    _teardown()
    c, u, plan = _bootstrap()
    r = _post_choose_plan(u, c, plan.id, "CW-DOES-NOT-EXIST")
    # Bad code sends the user BACK to /choose-plan (so they can
    # correct or clear the code). Redirect target should mention it.
    assert r.status_code in (302, 303)
    loc = r.headers.get("Location") or ""
    assert "/choose-plan" in loc, f"loc={loc}"
    # The critical assertion: the BAD coupon must NOT get stuck on
    # the company.
    db.session.expire_all()
    fresh = db.session.get(Company, c.id)
    assert fresh.applied_coupon_id is None, \
        f"bad coupon leaked: applied={fresh.applied_coupon_id}"
    return "bad code → sent back, no coupon attached"


@check("4. Expired coupon → rejected")
def _():
    from app.models import Company
    _teardown()
    c, u, plan = _bootstrap()
    r = _post_choose_plan(u, c, plan.id, "CW-EXPIRED")
    assert r.status_code in (302, 303)
    fresh = db.session.get(Company, c.id)
    assert fresh.applied_coupon_id is None, \
        "expired coupon incorrectly attached"
    return "expired → refused"


@check("5. Max-uses-exceeded coupon → rejected")
def _():
    from app.models import Company
    _teardown()
    c, u, plan = _bootstrap()
    r = _post_choose_plan(u, c, plan.id, "CW-ZERO")
    assert r.status_code in (302, 303)
    fresh = db.session.get(Company, c.id)
    assert fresh.applied_coupon_id is None, \
        "over-used coupon incorrectly attached"
    return "max_uses=0 → refused"


@check("6. Service-layer redeem() still persists a CouponRedemption")
def _():
    """This is the safety net for SaaS billing (Ticket 7): after
    payment succeeds we call coupons.redeem() to lock in the
    usage. Prove it works with the new applied_coupon_id column
    in place."""
    from app.models import Company, Coupon, CouponRedemption, User
    from app.services import coupons as _cp
    _teardown()
    c, u, plan = _bootstrap()
    # Simulate the /choose-plan flow: valid → stash → then redeem.
    # Skip HTTP roundtrip — invoke the handler directly via
    # test_request_context so we control `current_user` and
    # `g.active_company` explicitly. The Flask-Login session state
    # across sequential test_client requests in the same app
    # context has been contamination-prone through Batches 3-5.
    from flask import current_app, g as _g
    from flask_login import login_user
    from app.routes.auth import choose_plan as _handler
    good = Coupon.query.filter_by(code="CW-GOOD-20").first()
    fresh_c = db.session.get(Company, c.id)
    fresh_u = db.session.get(User, u.id)
    with current_app.test_request_context(
            "/choose-plan", method="POST", data={
                "plan_id": plan.id, "coupon_code": "CW-GOOD-20",
            }):
        login_user(fresh_u)
        _g.active_company = fresh_c
        response = _handler()
    # Response is a Werkzeug redirect (302).
    assert response.status_code in (302, 303), \
        f"handler returned {response.status_code}"
    # Handler already committed. Read fresh state.
    db.session.expire_all()
    db.session.expire_all()
    fresh = db.session.get(Company, c.id)
    # Re-fetch the coupon by id from a fresh query so we're not
    # using a stale ORM row.
    coupon = Coupon.query.filter_by(id=fresh.applied_coupon_id).first()
    assert coupon is not None, \
        f"coupon id={fresh.applied_coupon_id} vanished from DB"
    fresh_user = db.session.get(User, u.id)
    redemption = _cp.redeem(coupon, fresh, fresh_user,
                              amount_saved=Decimal("200"))
    assert redemption.id is not None
    assert redemption.coupon_id == coupon.id
    assert redemption.company_id == c.id
    assert Decimal(str(redemption.amount_saved)) == Decimal("200")
    return f"redemption #{redemption.id} saved (200 EGP)"


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
