#!/usr/bin/env python3
"""MARSOUD-SUPERADMIN-CONTROL-01 T3 (2026-08-08) — Plan Builder audit.

Eleven checks covering the two-pane GET, legacy redirects, unified
save (create + update, including inline quotas + SAR price), and
the delete guard.
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
PREFIX = "__T3_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _p(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


# ─── Fixture ───────────────────────────────────────────────────
def _setup(*, with_company=False, extra_plan=False):
    """One super-admin + one fixture plan. Optionally: a second
    plan and a company assigned to the fixture plan."""
    _teardown()
    from app.models import (
        Company, Plan, User, UserStatus, Quota, ENF_BLOCK,
        QUOTA_USERS,
    )
    from app.models.user import user_companies
    from werkzeug.security import generate_password_hash

    plan = Plan(code=f"{PREFIX.lower()}p1", name="T3-Plan-1",
                 name_ar="خطة T3",
                 allowed_subitems=None,
                 price_monthly=99, price_yearly=990,
                 is_active=True)
    plan.set_modules(["accounting", "sales"])
    db.session.add(plan); db.session.flush()

    # Give it one seeded quota so the update path can toggle it.
    db.session.add(Quota(plan_id=plan.id, quota_type=QUOTA_USERS,
                          included_amount=5,
                          enforcement_mode=ENF_BLOCK))

    plan2 = None
    if extra_plan:
        plan2 = Plan(code=f"{PREFIX.lower()}p2", name="T3-Plan-2",
                      name_ar="خطة T3-2",
                      allowed_subitems=None, is_active=True)
        plan2.set_modules(["reports"])
        db.session.add(plan2); db.session.flush()

    sa = User(
        email=f"{PREFIX}sa@x.test", full_name="super admin",
        is_active=True, is_superadmin=True,
        status=UserStatus.ACTIVE.value,
        email_verified_at=datetime.utcnow(),
        terms_version="TEST",
        password_hash=generate_password_hash(
            "x", method="pbkdf2:sha256"))
    db.session.add(sa); db.session.flush()

    company = None
    if with_company:
        company = Company(name=f"{PREFIX}CO", base_currency="EGP",
                           subdomain="t3co",
                           subscription_started_at=datetime.utcnow(),
                           subscription_expires_at=(
                               datetime.utcnow() + timedelta(days=365)),
                           intended_plan_id=plan.id,
                           plan_id=plan.id)
        db.session.add(company); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=sa.id, company_id=company.id, role="owner"))

    db.session.commit()

    _STATE.update(
        plan_id=plan.id,
        plan2_id=(plan2.id if plan2 else None),
        superadmin_id=sa.id,
        company_id=(company.id if company else None),
    )


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__T3_%'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    try:
                        conn.execute(text(
                            f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                            {"c": cid})
                    except Exception:
                        pass
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE '__T3_%@x.test'"))

        # Nuke fixture plans + their child rows (SQLite doesn't
        # enforce ondelete=CASCADE without PRAGMA).
        pids = [r[0] for r in conn.execute(text(
            "SELECT id FROM plans WHERE code LIKE '__t3__%' "
            "OR code LIKE '__t3_%'"))]
        for pid in pids:
            conn.execute(text(
                "DELETE FROM quotas WHERE plan_id = :p"), {"p": pid})
            conn.execute(text(
                "DELETE FROM plan_prices WHERE plan_id = :p"),
                {"p": pid})
        conn.execute(text(
            "DELETE FROM plans WHERE code LIKE '__t3__%' "
            "OR code LIKE '__t3_%'"))
        # Sweep orphan quotas / plan_prices from prior aborted runs
        # (SQLite PK reuse can attach them to a fresh plan).
        conn.execute(text(
            "DELETE FROM quotas WHERE plan_id NOT IN "
            "(SELECT id FROM plans)"))
        conn.execute(text(
            "DELETE FROM plan_prices WHERE plan_id NOT IN "
            "(SELECT id FROM plans)"))
        # Scrub coupons that referenced our fixture plans in JSON.
        conn.execute(text(
            "DELETE FROM coupons WHERE code LIKE '__T3_%'"))


def _login(client, user_id):
    with client.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True


def _fresh_client():
    """Fresh test_client with the super-admin logged in. Also
    clears Flask-Login's cached current_user + the ORM identity
    map to avoid the DetachedInstanceError seen across earlier
    audits (T6 / T10 hit the same shape)."""
    from flask import g
    try:
        g.pop("_login_user", None)
    except (KeyError, AttributeError, RuntimeError):
        pass
    db.session.expire_all()
    db.session.remove()
    app = _STATE["app"]
    c = app.test_client()
    _login(c, _STATE["superadmin_id"])
    return c


# ─── Checks ────────────────────────────────────────────────────
@check("1. GET /admin/plans (no query) preselects the first plan")
def _():
    _setup()
    r = _fresh_client().get("/admin/plans")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # Fixture plan's Arabic name renders in the form.
    assert "خطة T3" in body
    # Delete-zone visible (edit mode → the guard message or the button).
    assert "حذف الباقة" in body


@check("2. GET /admin/plans?plan_id=new renders empty form")
def _():
    _setup()
    r = _fresh_client().get("/admin/plans?plan_id=new")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # No `value="خطة T3"` in create mode.
    assert 'value="خطة T3"' not in body
    # The code input is present and NOT readonly.
    assert 'name="code"' in body
    assert 'readonly' not in body.split('name="code"')[1][:200]


@check("3. GET /admin/plans/new -> 302 to ?plan_id=new")
def _():
    _setup()
    r = _fresh_client().get("/admin/plans/new",
                              follow_redirects=False)
    assert r.status_code in (301, 302), r.status_code
    assert "plan_id=new" in (r.headers.get("Location") or ""), \
        r.headers.get("Location")


@check("4. GET /admin/plans/<id>/edit -> 302 to ?plan_id=<id>")
def _():
    _setup()
    pid = _STATE["plan_id"]
    r = _fresh_client().get(f"/admin/plans/{pid}/edit",
                              follow_redirects=False)
    assert r.status_code in (301, 302), r.status_code
    assert f"plan_id={pid}" in (r.headers.get("Location") or "")


@check("5. POST /admin/plans/save (create) inserts Plan + Quotas")
def _():
    from app.models import Plan, Quota, QUOTA_USERS, QUOTA_STORAGE_BYTES
    _setup()
    r = _fresh_client().post("/admin/plans/save", data={
        "plan_id": "",
        "code": f"{PREFIX.lower()}newp",
        "name": "T3-NEW",
        "name_ar": "خطة T3 الجديدة",
        "description": "created via T3 audit",
        "price_monthly": "50",
        "price_yearly": "500",
        "modules": ["accounting", "hr"],
        "submit_subitems": "1",
        "subitems": [],
        # Two quotas seeded, two skipped.
        "included_users": "20",
        "enforcement_users": "BLOCK",
        "price_extra_users": "",
        "included_storage_bytes": "1073741824",
        "enforcement_storage_bytes": "ALLOW_NOTIFY",
        "price_extra_storage_bytes": "0.05",
        "included_ai_tokens_month": "",
        "enforcement_ai_tokens_month": "",
        "price_extra_ai_tokens_month": "",
        "included_branches": "",
        "enforcement_branches": "",
        "price_extra_branches": "",
    })
    assert r.status_code in (301, 302), r.status_code
    p = Plan.query.filter_by(code=f"{PREFIX.lower()}newp").first()
    assert p is not None
    assert set(p.modules) == {"accounting", "hr"}, p.modules
    qs = {q.quota_type: q for q in
          Quota.query.filter_by(plan_id=p.id).all()}
    assert QUOTA_USERS in qs, list(qs)
    assert qs[QUOTA_USERS].included_amount == 20
    assert QUOTA_STORAGE_BYTES in qs
    assert float(qs[QUOTA_STORAGE_BYTES].price_per_extra_unit) == 0.05


@check("6. POST /admin/plans/save (update) upserts+deletes Quotas")
def _():
    from app.models import Plan, Quota, QUOTA_USERS, QUOTA_BRANCHES
    _setup()
    pid = _STATE["plan_id"]
    # Original: users quota only, included=5.
    r = _fresh_client().post("/admin/plans/save", data={
        "plan_id": str(pid),
        "code": f"{PREFIX.lower()}p1",
        "name": "T3-Plan-1",
        "name_ar": "خطة T3",
        "description": "",
        "price_monthly": "99",
        "price_yearly": "990",
        "modules": ["accounting"],
        "submit_subitems": "1",
        # Delete users quota (all blank) + add branches quota.
        "included_users": "",
        "enforcement_users": "",
        "price_extra_users": "",
        "included_branches": "3",
        "enforcement_branches": "BLOCK",
        "price_extra_branches": "",
    })
    assert r.status_code in (301, 302), r.status_code
    p = db.session.get(Plan, pid)
    assert p.modules == ["accounting"], p.modules
    qs = {q.quota_type for q in
          Quota.query.filter_by(plan_id=pid).all()}
    assert QUOTA_USERS not in qs, "users quota not deleted"
    assert QUOTA_BRANCHES in qs, "branches quota not inserted"


@check("7. POST /admin/plans/save writes SAR PlanPrice row")
def _():
    from app.models import PlanPrice
    _setup()
    pid = _STATE["plan_id"]
    r = _fresh_client().post("/admin/plans/save", data={
        "plan_id": str(pid),
        "code": f"{PREFIX.lower()}p1",
        "name": "T3-Plan-1",
        "name_ar": "خطة T3",
        "description": "",
        "price_monthly": "99", "price_yearly": "990",
        "price_monthly_sar": "70",
        "price_yearly_sar": "700",
        "modules": ["accounting", "sales"],
    })
    assert r.status_code in (301, 302)
    row = PlanPrice.query.filter_by(plan_id=pid, currency="SAR").first()
    assert row is not None
    assert float(row.price_monthly) == 70
    assert float(row.price_yearly) == 700


@check("8. plans_delete refuses when a Company points at plan_id")
def _():
    from app.models import Plan
    _setup(with_company=True)
    pid = _STATE["plan_id"]
    r = _fresh_client().post(f"/admin/plans/{pid}/delete",
                                follow_redirects=False)
    assert r.status_code in (301, 302)
    # Plan still exists.
    assert db.session.get(Plan, pid) is not None


@check("9. plans_delete refuses when only intended_plan_id points")
def _():
    from app.models import Plan, Company
    _setup(with_company=True)
    pid = _STATE["plan_id"]
    # Clear plan_id but keep intended_plan_id.
    c = db.session.get(Company, _STATE["company_id"])
    c.plan_id = None
    db.session.commit()

    r = _fresh_client().post(f"/admin/plans/{pid}/delete",
                                follow_redirects=False)
    assert r.status_code in (301, 302)
    assert db.session.get(Plan, pid) is not None


@check("10. plans_delete on unused plan removes plan + children")
def _():
    from app.models import Plan, Quota, PlanPrice, Coupon
    _setup(extra_plan=True)
    pid = _STATE["plan_id"]
    # Attach a coupon that mentions this plan_id in its JSON.
    coupon = Coupon(code=f"{PREFIX}COUP", discount_type="PERCENT",
                     discount_value=10, active=True)
    coupon.set_plan_ids([pid, _STATE["plan2_id"]])
    db.session.add(coupon); db.session.flush()
    # Attach a SAR PlanPrice + more Quotas so cleanup is visible.
    db.session.add(PlanPrice(plan_id=pid, currency="SAR",
                              price_monthly=70, price_yearly=700))
    db.session.commit()

    r = _fresh_client().post(f"/admin/plans/{pid}/delete",
                                follow_redirects=False)
    assert r.status_code in (301, 302), r.status_code
    assert db.session.get(Plan, pid) is None, "plan not deleted"
    assert Quota.query.filter_by(plan_id=pid).count() == 0
    assert PlanPrice.query.filter_by(plan_id=pid).count() == 0
    # Coupon scrubbed: only plan2_id remains.
    coup = Coupon.query.filter_by(code=f"{PREFIX}COUP").first()
    assert coup is not None
    assert coup.plan_ids == [_STATE["plan2_id"]], coup.plan_ids


@check("11. plans_delete logs plan_delete action")
def _():
    from app.models import PlatformAuditLog
    _setup()
    pid = _STATE["plan_id"]
    before = PlatformAuditLog.query.filter_by(action="plan_delete").count()
    r = _fresh_client().post(f"/admin/plans/{pid}/delete",
                                follow_redirects=False)
    assert r.status_code in (301, 302)
    after = PlatformAuditLog.query.filter_by(action="plan_delete").count()
    assert after == before + 1, f"before={before} after={after}"


# ─── Runner ────────────────────────────────────────────────────
def main():
    app = create_app()
    _STATE["app"] = app
    passed = failed = 0
    failures = []
    with app.app_context():
        for label, fn in CHECKS:
            try:
                fn()
                passed += 1
                _p(f"  [OK] {label}")
            except AssertionError as e:
                failed += 1
                failures.append((label, str(e)))
                _p(f"  [FAIL] {label}: {e}")
            except Exception as e:
                failed += 1
                failures.append((label, f"{type(e).__name__}: {e}"))
                _p(f"  [ERROR] {label}: {type(e).__name__}: {e}")
        _teardown()
    _p("")
    _p(f"audit_plan_builder: {passed} passed, {failed} failed")
    if failures:
        for label, err in failures:
            _p(f"  - {label} :: {err}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
