#!/usr/bin/env python3
"""MARSOUD-MULTI-CURRENCY-PRICING (Abdelhamid 2026-07-22).

Per-plan prices in EGP + SAR (extensible for USD/AED later).
`Plan.price_for(currency, cycle)` prefers a plan_prices row and
falls back to the legacy Plan.price_monthly / price_yearly columns
(which are always in EGP). Register form now defaults to EGP.

Checks:
  1. Legacy Plan with only price_monthly/price_yearly: price_for('EGP')
     returns the legacy value; price_for('SAR') falls back to it.
  2. Plan with a PlanPrice(SAR) row: price_for('SAR') returns THAT
     value; price_for('EGP') still returns the legacy value.
  3. Explicit EGP plan_prices row overrides legacy columns.
  4. Missing currency + missing legacy → None.
  5. Admin POST persists a SAR row; empty SAR inputs delete the row.
  6. Register HTML now defaults selected=EGP.
  7. Auth.py signup route default is EGP.
"""
import os
import sys
from decimal import Decimal
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
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM plan_prices WHERE plan_id IN "
            "(SELECT id FROM plans WHERE code LIKE 'mcp-%')"))
        conn.execute(text("DELETE FROM plans WHERE code LIKE 'mcp-%'"))
        conn.execute(text(
            "DELETE FROM users WHERE email = 'mcp-admin@x.test'"))


@check("1. Legacy plan (no plan_prices rows) — EGP returns legacy, "
       "SAR falls back to EGP")
def _():
    from app.models import Plan
    _teardown()
    p = Plan(code="mcp-legacy", name="Legacy", name_ar="قديم",
             price_monthly=100, price_yearly=1000, is_active=True)
    db.session.add(p); db.session.commit()
    assert p.price_for("EGP", "monthly") == Decimal("100")
    assert p.price_for("EGP", "yearly") == Decimal("1000")
    assert p.price_for("SAR", "monthly") == Decimal("100"), \
        "SAR should fall back to EGP when no SAR row"
    return "legacy EGP + SAR fallback OK"


@check("2. Plan with SAR row: price_for('SAR') returns SAR row, "
       "EGP still returns legacy")
def _():
    from app.models import Plan, PlanPrice
    p = Plan.query.filter_by(code="mcp-legacy").one()
    db.session.add(PlanPrice(
        plan_id=p.id, currency="SAR",
        price_monthly=50, price_yearly=500))
    db.session.commit()
    assert p.price_for("SAR", "monthly") == Decimal("50")
    assert p.price_for("SAR", "yearly") == Decimal("500")
    assert p.price_for("EGP", "monthly") == Decimal("100"), \
        "EGP legacy should still win when only SAR row exists"
    return "SAR row wins for SAR, EGP legacy untouched"


@check("3. Explicit EGP plan_prices row overrides legacy columns")
def _():
    from app.models import Plan, PlanPrice
    p = Plan(code="mcp-explicit", name="Explicit", name_ar="واضح",
             price_monthly=999, is_active=True)   # legacy
    db.session.add(p); db.session.flush()
    db.session.add(PlanPrice(
        plan_id=p.id, currency="EGP", price_monthly=800))
    db.session.commit()
    assert p.price_for("EGP", "monthly") == Decimal("800"), \
        "explicit EGP row should override legacy column"
    return "explicit EGP row overrides legacy"


@check("4. Missing currency + missing legacy → None (safe)")
def _():
    from app.models import Plan
    p = Plan(code="mcp-empty", name="Empty", name_ar="فارغ",
             is_active=True)
    db.session.add(p); db.session.commit()
    assert p.price_for("EGP", "monthly") is None
    assert p.price_for("SAR", "monthly") is None
    return "None when nothing set"


@check("5. Admin POST /admin/plans/<id>/edit upserts SAR row + "
       "empty SAR deletes it")
def _():
    from flask import current_app
    from app.models import Plan, PlanPrice, User
    from werkzeug.security import generate_password_hash
    from sqlalchemy import text
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM users WHERE email = 'mcp-admin@x.test'"))
    admin = User(email="mcp-admin@x.test",
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name="mcp-admin", is_superadmin=True,
                 is_active=True)
    db.session.add(admin); db.session.commit()

    p = Plan.query.filter_by(code="mcp-legacy").one()
    from flask import g
    for k in ("_login_user", "active_company"):
        try: g.pop(k, None)
        except Exception: pass
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin.id)
        sess["_fresh"] = True

    # Set SAR to (200, 2000).
    r = client.post(f"/admin/plans/{p.id}/edit", data={
        "name_ar": p.name_ar, "name": p.name,
        "price_monthly": p.price_monthly, "price_yearly": p.price_yearly,
        "price_monthly_sar": "200",
        "price_yearly_sar": "2000",
        "is_active": "on",
    }, follow_redirects=False)
    assert r.status_code == 302
    db.session.expire_all()
    row = PlanPrice.query.filter_by(plan_id=p.id, currency="SAR").first()
    assert row and float(row.price_monthly) == 200 and float(row.price_yearly) == 2000
    # Now clear SAR via empty inputs.
    r = client.post(f"/admin/plans/{p.id}/edit", data={
        "name_ar": p.name_ar, "name": p.name,
        "price_monthly": p.price_monthly, "price_yearly": p.price_yearly,
        "price_monthly_sar": "",
        "price_yearly_sar": "",
        "is_active": "on",
    }, follow_redirects=False)
    assert r.status_code == 302
    db.session.expire_all()
    row = PlanPrice.query.filter_by(plan_id=p.id, currency="SAR").first()
    assert row is None, "empty SAR should delete the row"
    return "SAR upsert + clear both work"


@check("6. Register HTML now selects EGP by default")
def _():
    template_path = ROOT / "app/templates/auth/register.html"
    text = template_path.read_text(encoding="utf-8")
    assert '<option value="EGP" selected>' in text, \
        "EGP option must be default-selected"
    # SAR should NOT be default-selected anymore.
    import re
    sar_default = re.search(
        r'<option value="SAR"[^>]*selected[^>]*>', text)
    assert sar_default is None, "SAR must NOT be default anymore"
    return "register template defaults to EGP"


@check("7. Signup route Python default is EGP")
def _():
    src = (ROOT / "app/routes/auth.py").read_text(encoding="utf-8")
    assert 'request.form.get("base_currency", "EGP")' in src, \
        "auth.py should default to EGP"
    return "auth.py default = EGP"


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
