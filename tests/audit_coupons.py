#!/usr/bin/env python3
"""MARSOUD-DISCOUNT-COUPONS (Abdelhamid 2026-07-22).

validate() + redeem() service. Super-admin CRUD.

Checks:
  1. PERCENT + FIXED discount math (respects cap at base_price).
  2. Unknown code → CouponError.
  3. Disabled coupon → error.
  4. Expired (valid_until past) → error.
  5. Not-yet-valid (valid_from future) → error.
  6. max_uses global cap → error after cap reached.
  7. max_uses_per_customer cap → error after cap reached FOR THAT company.
  8. applies_to_plan_ids restriction (wrong plan → error, right plan → OK).
  9. redeem() writes a CouponRedemption row.
 10. Super-admin CRUD: create, list, toggle active.
"""
import os
import sys
from datetime import date, timedelta, datetime
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
        conn.execute(text(
            "DELETE FROM coupon_redemptions WHERE coupon_id IN "
            "(SELECT id FROM coupons WHERE code LIKE 'CP-%')"))
        conn.execute(text("DELETE FROM coupons WHERE code LIKE 'CP-%'"))
        target_cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__CP_%__'"))]
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
            "DELETE FROM users WHERE email LIKE 'cp-%@x.test'"))
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id NOT IN "
                    "(SELECT id FROM companies)"))


def _mk_company(suffix):
    from app.models import Company
    from app.services.subscription import activate_default_subscription
    from app.services.seed_coa import seed_default_coa
    c = Company(name=f"__CP_{suffix}__", base_currency="EGP",
                subdomain=f"cp-{suffix.lower()}")
    activate_default_subscription(c, plan_code=None)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    db.session.commit()
    return c


def _mk_coupon(**kw):
    from app.models import Coupon, DISCOUNT_PERCENT
    kw.setdefault("discount_type", DISCOUNT_PERCENT)
    kw.setdefault("discount_value", 10)
    kw.setdefault("active", True)
    kw.setdefault("max_uses_per_customer", 1)
    c = Coupon(**kw)
    db.session.add(c); db.session.commit()
    return c


@check("1. PERCENT + FIXED discount math (respects cap at base_price)")
def _():
    from app.services.coupons import compute_discount
    from app.models import DISCOUNT_PERCENT, DISCOUNT_FIXED
    _teardown()
    p = _mk_coupon(code="CP-PCT10",
                    discount_type=DISCOUNT_PERCENT, discount_value=25)
    f = _mk_coupon(code="CP-FIX50",
                    discount_type=DISCOUNT_FIXED, discount_value=500)
    assert compute_discount(p, 800) == Decimal("200")
    assert compute_discount(f, 800) == Decimal("500")
    # Cap at base_price.
    assert compute_discount(f, 300) == Decimal("300"), \
        "FIXED discount can't exceed base price"
    return "percent + fixed both correct + capped"


@check("2. Unknown code → CouponError")
def _():
    from app.services.coupons import validate, CouponError
    try:
        validate("CP-NOPE", None, 1, 100)
        raised = False
    except CouponError:
        raised = True
    assert raised
    return "unknown → error"


@check("3. Disabled coupon → error")
def _():
    from app.services.coupons import validate, CouponError
    c = _mk_coupon(code="CP-OFF", active=False)
    raised = False
    try:
        validate("CP-OFF", None, 1, 100)
    except CouponError:
        raised = True
    assert raised
    return "disabled → error"


@check("4. Expired coupon → error")
def _():
    from app.services.coupons import validate, CouponError
    c = _mk_coupon(code="CP-EXPIRED",
                    valid_until=date.today() - timedelta(days=1))
    raised = False
    try:
        validate("CP-EXPIRED", None, 1, 100)
    except CouponError:
        raised = True
    assert raised
    return "expired → error"


@check("5. Not-yet-valid coupon → error")
def _():
    from app.services.coupons import validate, CouponError
    c = _mk_coupon(code="CP-FUTURE",
                    valid_from=date.today() + timedelta(days=7))
    raised = False
    try:
        validate("CP-FUTURE", None, 1, 100)
    except CouponError:
        raised = True
    assert raised
    return "not-yet-valid → error"


@check("6. max_uses cap enforced (global)")
def _():
    from app.services.coupons import validate, redeem, CouponError
    from app.models import CouponRedemption
    c = _mk_coupon(code="CP-CAP2", max_uses=2)
    company_a = _mk_company("CAP_A")
    company_b = _mk_company("CAP_B")
    company_c = _mk_company("CAP_C")

    coup, amt = validate("CP-CAP2", company_a, 1, 100)
    redeem(coup, company_a, None, amt)
    coup, amt = validate("CP-CAP2", company_b, 1, 100)
    redeem(coup, company_b, None, amt)

    # Third company should be refused.
    raised = False
    try:
        validate("CP-CAP2", company_c, 1, 100)
    except CouponError:
        raised = True
    assert raised, "global cap should refuse a 3rd redeem"
    return "max_uses cap enforced"


@check("7. max_uses_per_customer cap enforced for the same company")
def _():
    from app.services.coupons import validate, redeem, CouponError
    c = _mk_coupon(code="CP-PC2", max_uses_per_customer=2)
    company = _mk_company("PC")
    for _ in range(2):
        coup, amt = validate("CP-PC2", company, 1, 100)
        redeem(coup, company, None, amt)
    # Third attempt for same company → refused.
    raised = False
    try:
        validate("CP-PC2", company, 1, 100)
    except CouponError:
        raised = True
    assert raised
    return "per-customer cap enforced"


@check("8. applies_to_plan_ids restriction: wrong plan → error, "
       "right plan → OK")
def _():
    from app.services.coupons import validate, CouponError
    c = _mk_coupon(code="CP-PLAN")
    c.set_plan_ids([1])
    db.session.commit()
    company = _mk_company("PLAN")
    # Plan 1 allowed.
    coup, amt = validate("CP-PLAN", company, 1, 100)
    # Plan 999 not allowed.
    raised = False
    try:
        validate("CP-PLAN", company, 999, 100)
    except CouponError:
        raised = True
    assert raised
    return "plan restriction enforced"


@check("9. redeem() writes a CouponRedemption row")
def _():
    from app.services.coupons import validate, redeem
    from app.models import CouponRedemption
    c = _mk_coupon(code="CP-RED")
    company = _mk_company("RED")
    coup, amt = validate("CP-RED", company, 1, 200)
    row = redeem(coup, company, None, amt)
    assert row.id is not None
    assert row.coupon_id == coup.id
    assert row.company_id == company.id
    assert float(row.amount_saved) == float(amt)
    return "redemption row saved"


@check("10. Super-admin: /admin/coupons + POST /new + toggle")
def _():
    from flask import current_app, g
    from app.models import User, Coupon
    from werkzeug.security import generate_password_hash
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM users WHERE email = 'cp-super@x.test'"))
    admin = User(email="cp-super@x.test",
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name="cp-super", is_superadmin=True,
                 is_active=True)
    db.session.add(admin); db.session.commit()

    for k in ("_login_user",):
        try: g.pop(k, None)
        except Exception: pass
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin.id)
        sess["_fresh"] = True

    r = client.get("/admin/coupons")
    assert r.status_code == 200
    r = client.post("/admin/coupons/new", data={
        "code": "CP-NEW", "discount_type": "PERCENT",
        "discount_value": "15",
        "max_uses_per_customer": "1",
    }, follow_redirects=False)
    assert r.status_code == 302
    row = Coupon.query.filter_by(code="CP-NEW").one()
    assert row.active is True
    # Toggle off.
    r = client.post(f"/admin/coupons/{row.id}/toggle",
                     follow_redirects=False)
    assert r.status_code == 302
    db.session.expire_all()
    row = Coupon.query.filter_by(code="CP-NEW").one()
    assert row.active is False
    return "CRUD + toggle work"


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
