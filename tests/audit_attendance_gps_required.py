#!/usr/bin/env python3
"""MARSOUD-MOBILE-TKT-04 (2026-08-18) — mandatory GPS audit.

Verifies:
  A. POST /api/v1/my/attendance/checkin with empty body → 400
     `gps_required`.
  B. Same with lat only (no lng) → 400.
  C. With both lat + lng → 201 (checkin succeeds).
  D. Setting `attendance_gps_required=false` in platform_settings
     → empty body POST now succeeds (safety valve).
  E. Same 400 rejection on /checkout.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db  # noqa: E402


CHECKS = []
CO_NAME = "__GPS_REQ_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text
    from app.models import Company, User, PlatformSetting
    from app.models.user import user_companies

    # Reset the toggle so tests are independent.
    PlatformSetting.query.filter_by(
        key="attendance_gps_required").delete()
    db.session.commit()

    ids = [c.id for c in Company.query.filter(
        Company.name.like(f"{CO_NAME}%")).all()]
    if ids:
        for t in reversed(db.metadata.sorted_tables):
            if "company_id" in t.c:
                try:
                    db.session.execute(
                        t.delete().where(t.c.company_id.in_(ids)))
                except Exception:
                    db.session.rollback()
        db.session.commit()
    for u in User.query.filter(
            User.email.like(f"{CO_NAME.lower()}%@x.local")).all():
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u.id))
        db.session.delete(u)
    db.session.commit()
    for cid in ids:
        try:
            db.session.execute(
                text("DELETE FROM companies WHERE id = :i"),
                {"i": cid})
        except Exception:
            db.session.rollback()
    db.session.commit()


def _setup():
    from datetime import timedelta
    from app.models import (Company, User, Plan, Employee)
    from app.models.user import user_companies
    from app.services.legal import get_terms_version
    from app.services.api_tokens import generate_token

    _teardown()
    tv = get_terms_version()
    now = datetime.utcnow()

    plan = Plan.query.filter_by(code="growth").first() \
           or Plan.query.filter_by(code="pro").first()

    u = User(email=f"{CO_NAME.lower()}_owner@x.local",
             full_name="Owner", terms_version=tv,
             terms_accepted_at=now)
    u.set_password("Passw0rd!audit1")
    db.session.add(u); db.session.flush()
    co = Company(name=f"{CO_NAME}_A", base_currency="EGP",
                 plan_id=plan.id if plan else None,
                 subscription_started_at=now,
                 subscription_expires_at=now + timedelta(days=30))
    db.session.add(co); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=co.id, role="owner"))
    # Employee row so _my_employee_or_404 succeeds.
    emp = Employee(company_id=co.id, user_id=u.id,
                    name=u.full_name, is_active=True)
    db.session.add(emp)
    db.session.commit()

    # Bearer token for API auth
    raw_token, _tok = generate_token(u, "audit:gps-required")
    _STATE.update(dict(u=u, co=co, emp=emp,
                        token=raw_token))


def _api(method, url, body=None):
    """Bearer-authenticated helper."""
    from flask import g as flask_g
    if "_login_user" in flask_g:
        del flask_g._login_user
    app = _STATE["app"]
    c = app.test_client()
    headers = {
        "Authorization": f"Bearer {_STATE['token']}",
        "X-Company-Id": str(_STATE["co"].id),
    }
    kwargs = dict(headers=headers)
    if body is not None:
        kwargs["json"] = body
    return c.open(url + f"?company_id={_STATE['co'].id}",
                   method=method, **kwargs)


# ─── A. Missing coords rejected ───────────────────────────────────────
@check("A1: POST /my/attendance/checkin with {} → 400 gps_required")
def A1():
    r = _api("POST", "/api/v1/my/attendance/checkin", body={})
    assert r.status_code == 400, (r.status_code, r.data[:200])
    body = json.loads(r.data)
    assert body.get("error") == "gps_required", body
    assert "GPS" in body.get("message_ar", ""), body


@check("A2: POST with lat only → 400 gps_required")
def A2():
    r = _api("POST", "/api/v1/my/attendance/checkin",
             body={"lat": 24.7})
    assert r.status_code == 400, r.status_code
    body = json.loads(r.data)
    assert body.get("error") == "gps_required", body


# ─── C. Both coords succeed ───────────────────────────────────────────
@check("C1: POST with both lat + lng → 201 checkin OK")
def C1():
    # First cleanup any existing checkin for today
    from datetime import date
    from sqlalchemy import text
    db.session.execute(text(
        "DELETE FROM attendance_checkins WHERE employee_id = :e"),
        {"e": _STATE["emp"].id})
    db.session.commit()
    r = _api("POST", "/api/v1/my/attendance/checkin",
             body={"lat": 24.7, "lng": 46.7})
    assert r.status_code == 201, (r.status_code, r.data[:200])
    body = json.loads(r.data)
    assert body.get("ok") is True, body


# ─── D. Toggle disables enforcement ───────────────────────────────────
@check("D1: attendance_gps_required=false → {} POST succeeds")
def D1():
    from app.models import PlatformSetting
    from datetime import datetime as _dt
    from sqlalchemy import text
    # Turn the toggle off
    db.session.add(PlatformSetting(
        key="attendance_gps_required",
        value="false",
        updated_at=_dt.utcnow(),
    ))
    db.session.commit()
    # Clear the checkin from C1
    db.session.execute(text(
        "DELETE FROM attendance_checkins WHERE employee_id = :e"),
        {"e": _STATE["emp"].id})
    db.session.commit()

    r = _api("POST", "/api/v1/my/attendance/checkin", body={})
    assert r.status_code == 201, (r.status_code, r.data[:200])

    # Reset toggle so subsequent test runs start clean
    PlatformSetting.query.filter_by(
        key="attendance_gps_required").delete()
    db.session.commit()


# ─── E. Checkout also enforced ────────────────────────────────────────
@check("E1: POST /my/attendance/checkout with {} → 400 gps_required")
def E1():
    r = _api("POST", "/api/v1/my/attendance/checkout", body={})
    assert r.status_code == 400, r.status_code
    body = json.loads(r.data)
    assert body.get("error") == "gps_required", body


# ─── Runner ───────────────────────────────────────────────────────────
def main():
    app = create_app()
    _STATE["app"] = app
    with app.app_context():
        _setup()
        try:
            failed = []
            for label, fn in CHECKS:
                try:
                    fn()
                    print(f"  [OK]   {label}")
                except Exception as e:
                    failed.append((label, e))
                    print(f"  [FAIL] {label}\n         -> {e}")
            total = len(CHECKS)
            ok = total - len(failed)
            print()
            print(f"{ok}/{total} OK" if not failed
                  else f"{ok}/{total} -- {len(failed)} FAILED")
            return 0 if not failed else 1
        finally:
            _teardown()


if __name__ == "__main__":
    sys.exit(main())
