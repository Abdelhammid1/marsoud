#!/usr/bin/env python3
"""MARSOUD-MOBILE-ACCOUNT-500-GUARD-01 (2026-09-16) —
`GET /api/v1/my/account` used to 500 on any broken FK anywhere in
the 6 sub-queries (payslips, leave types/balances/requests,
today_checkin, advance active+repayments, advance requests).  The
mobile home screen showed "internal error" with no clue which
section was at fault.

This suite guards the isolation: if ONE sub-query throws, the
endpoint returns 200 with:
  · the sections that succeeded, populated normally
  · the failed section as an empty default ([] or None)
  · `sections_failed: ["<section name>"]` so the mobile app can
    surface a soft banner instead of an empty home screen

Checks:
  1. Happy path — every section returns real data, sections_failed=[]
  2. One serializer raises → 200, sections_failed lists the section,
     the OTHER sections still populate
  3. active_advance_for raises → still returns advance:{active:null,
     repayments:[], requests:[...]} instead of 500
  4. Response shape stays valid JSON with all top-level keys, so
     the mobile client's `.when` handler still hits `data`
"""
import os
import sys
from pathlib import Path
from datetime import datetime, date, timedelta
from decimal import Decimal
from unittest.mock import patch

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _boot(prefix):
    """Fresh company + owner + linked Employee + a bearer token."""
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan, Employee, EmployeeStatus
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from app.services.api_tokens import generate_token

    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        "SELECT id FROM companies WHERE name LIKE :p"),
        {"p": f"__{prefix}__%"})]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"),
                    {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        "DELETE FROM users WHERE email LIKE :p"),
        {"p": f"%__{prefix.lower()}__%"})
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C",
                    allowed_subitems=None)
        db.session.add(plan)
    plan.set_modules(["accounting", "hr"])
    db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency="EGP",
                subdomain=prefix.lower(), plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.commit()
    seed_default_coa(c.id); db.session.commit()

    # Pick up the platform's current terms_version so the gate that
    # `api_v1_auth.login` enforces doesn't refuse our bearer.
    from app.services.legal import get_terms_version
    _current_tv = get_terms_version() or "v1.0"

    email = f"emp__{prefix.lower()}__@x.io"
    u = User(email=email,
             full_name=f"Emp {prefix}", is_active=True,
             email_verified_at=datetime.utcnow(),
             terms_version=_current_tv,
             terms_accepted_at=datetime.utcnow())
    u.set_password("Pw12345678!")
    db.session.add(u); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="employee"))
    db.session.commit()

    emp = Employee(
        company_id=c.id, employee_number=f"EMP-{prefix}",
        name=f"Emp {prefix}", email=email,
        job_title="Tester",
        start_date=date.today() - timedelta(days=180),
        contract_type="FULL_TIME",
        status=EmployeeStatus.ACTIVE,
        basic_salary=Decimal("10000"),
        allowances=Decimal("1000"),
        deductions=Decimal("0"),
        is_active=True,
        user_id=u.id,
    )
    db.session.add(emp); db.session.commit()

    raw, _tok = generate_token(u, f"test:{prefix}")
    return c.id, u.id, emp.id, raw


@check("1. Happy path — all sections load, sections_failed=[]")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        cid, uid, eid, tok = _boot("MA1")
        client = app.test_client()
        r = client.get(
            f"/api/v1/my/account?company_id={cid}",
            headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, (r.status_code, r.get_data()[:400])
        data = r.get_json()
        assert data["sections_failed"] == [], data["sections_failed"]
        # Employee section populated with the real record.
        assert data["employee"], "employee section missing"
        assert data["tenure_label"] != ""
        return "sections_failed=[] on healthy account"


@check("2. When employee_full raises → endpoint still 200, "
        "employee={} + sections_failed=['employee']")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        cid, uid, eid, tok = _boot("MA2")
        client = app.test_client()
        with patch("app.services.api_serializers.employee_full",
                    side_effect=RuntimeError("simulated FK break")):
            r = client.get(
                f"/api/v1/my/account?company_id={cid}",
                headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, (r.status_code, r.get_data()[:400])
        data = r.get_json()
        assert "employee" in data["sections_failed"], (
            f"expected 'employee' in sections_failed, got "
            f"{data['sections_failed']}")
        # Other sections still populated with their (empty) defaults.
        assert data["employee"] == {}
        assert isinstance(data["payslips"], list)
        assert isinstance(data["leave"], dict)
        return "one-section failure isolated"


@check("3. active_advance_for raises → advance:{active:null,...} "
        "+ sections_failed lists 'advance.active_and_repayments'")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        cid, uid, eid, tok = _boot("MA3")
        client = app.test_client()
        with patch("app.services.advances.active_advance_for",
                    side_effect=RuntimeError("advance service broken")):
            r = client.get(
                f"/api/v1/my/account?company_id={cid}",
                headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        data = r.get_json()
        assert "advance.active_and_repayments" in data["sections_failed"], (
            f"sections_failed = {data['sections_failed']}")
        assert data["advance"]["active"] is None
        assert data["advance"]["repayments"] == []
        return "advance service failure isolated"


@check("4. Response shape stays complete even when 2 sections fail")
def _():
    from app import create_app, db
    from app.models import LeaveType, PayrollLine, PayrollRun
    app = create_app()
    with app.app_context():
        cid, uid, eid, tok = _boot("MA4")
        # Seed a real payslip + leave type so the list comprehensions
        # actually invoke the serializers — an empty query never
        # would.
        lt = LeaveType(
            company_id=cid, name="Annual",
            accrual_per_month=Decimal("1.75"),
            max_balance=Decimal("21"), is_paid=True, is_active=True)
        db.session.add(lt)
        run = PayrollRun(
            company_id=cid, period_year=date.today().year,
            period_month=date.today().month)
        db.session.add(run); db.session.flush()
        pl = PayrollLine(
            run_id=run.id, employee_id=eid,
            basic=Decimal("10000"), net=Decimal("10000"))
        db.session.add(pl); db.session.commit()

        client = app.test_client()
        # Now patch the two serializers used inside those list
        # comprehensions.
        with patch("app.services.api_serializers.payroll_line_brief",
                    side_effect=RuntimeError("payslip serializer broken")), \
             patch("app.services.api_serializers.leave_type_brief",
                    side_effect=RuntimeError("leave type serializer broken")):
            r = client.get(
                f"/api/v1/my/account?company_id={cid}",
                headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, (r.status_code, r.get_data()[:400])
        data = r.get_json()
        # Every top-level key must still be present so the mobile
        # side's `.when(data:...)` handler still hits the data branch.
        for k in ("employee", "tenure_label", "payslips", "leave",
                  "advance", "today_checkin", "sections_failed"):
            assert k in data, f"missing top-level key: {k}"
        assert "payslips" in data["sections_failed"], (
            f"expected 'payslips' in {data['sections_failed']}")
        assert "leave.types" in data["sections_failed"], (
            f"expected 'leave.types' in {data['sections_failed']}")
        assert data["payslips"] == []
        assert data["leave"]["types"] == []
        # Untouched sections still populate.
        assert isinstance(data["leave"]["balances"], list)
        assert isinstance(data["leave"]["requests"], list)
        return "shape preserved across 2 concurrent section failures"


def main():
    from app import create_app
    _ = create_app()
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
