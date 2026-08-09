#!/usr/bin/env python3
"""MARSOUD-ATTENDANCE-VIEW-01 (2026-08-08) — HR-side check-in
visibility audit.

Eight checks covering the two acceptance angles + guards:
  1. checkins_in_period() shape (dict keyed by (emp, date))
  2. checkins_in_period(employee_id=X) filters
  3. GET /hr/attendance shows the emerald "حاضر" chip for a
     day with a check-in and no exception
  4. Same GET shows "لم يسجّل" for a past day with neither
  5. Same GET keeps rendering the existing 4 exception dots
  6. Employee-name column is a link to the new detail route
  7. GET /hr/attendance/employee/<id> renders 200 with rows
     for each day of the month; check-in day shows time
  8. Cross-tenant employee_id → 404; team_member → 403
"""
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

# MARSOUD-4-BRANCH-REPAIR (2026-08-08) — refuse unscoped bulk
# deletes on the attendance tables (prod-data-loss incident).
import tests._audit_guard as _audit_guard  # noqa: E402
_audit_guard.install()


CHECKS = []
PREFIX = "__HRATT_"
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
def _setup():
    """Two companies for the cross-tenant guard. Each has 1
    Employee + 1 HR user + 1 team_member user."""
    _teardown()
    from app.models import Company, Plan, Employee, User
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company

    plan = Plan.query.filter_by(code="__hratt__").first()
    if not plan:
        plan = Plan(code="__hratt__", name="HRAtt", name_ar="حضور HR",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "crm", "hr",
                          "reports", "settings"])
        db.session.add(plan); db.session.flush()

    def _mk_co(tag):
        co = Company(name=f"{PREFIX}CO_{tag}__", base_currency="EGP",
                     vat_rate=0, plan_id=plan.id)
        db.session.add(co); db.session.flush()
        co.intended_plan_id = plan.id
        db.session.commit()
        ensure_roles_ready_for_company(co.id)
        return co

    def _mk_user(co, tag, role):
        u = User(
            email=f"{PREFIX}{co.id}_{tag}@audit.local",
            full_name=tag, is_active=True,
            terms_version=get_terms_version(),
            terms_accepted_at=datetime.utcnow())
        u.set_password("Passw0rd!audit1")
        db.session.add(u); db.session.flush()
        set_membership_role(u.id, co.id, role)
        return u.id

    long_ago = date.today() - timedelta(days=400)

    def _mk_emp(co, name, user_id=None):
        e = Employee(company_id=co.id, name=name,
                     basic_salary=Decimal("5000"),
                     status="ACTIVE", start_date=long_ago,
                     user_id=user_id)
        db.session.add(e); db.session.flush()
        return e.id

    co_a = _mk_co("A")
    co_b = _mk_co("B")

    hr_a = _mk_user(co_a, "hr", "hr_manager")
    tm_a = _mk_user(co_a, "tm", "team_member")
    emp_a1 = _mk_emp(co_a, f"{PREFIX}emp_A1")
    emp_a2 = _mk_emp(co_a, f"{PREFIX}emp_A2")

    _mk_user(co_b, "hr", "hr_manager")
    emp_b = _mk_emp(co_b, f"{PREFIX}emp_B")

    db.session.commit()
    _STATE.update(
        cid_a=co_a.id, cid_b=co_b.id,
        hr_a=hr_a, tm_a=tm_a,
        emp_a1=emp_a1, emp_a2=emp_a2, emp_b=emp_b,
    )


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    for co in Company.query.filter(
            Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        db.session.execute(text(
            "DELETE FROM companies WHERE id=:c"), {"c": cid})
        db.session.commit()
    db.session.execute(text(
        "DELETE FROM plans WHERE code='__hratt__'"))
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text(
            "DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _reset_att():
    # MARSOUD-4-BRANCH-REPAIR (2026-08-08) — was
    #   AttendanceCheckin.query.delete()
    #   AttendanceException.query.delete()
    # → wiped every tenant's attendance if run against a DB with
    # real data (2026-08-08 prod-data-loss incident: 50 exceptions
    # + 12 check-ins nuked, restored from backup). Scope to the
    # two fixture companies only.
    from app.models import AttendanceCheckin, AttendanceException
    for _cid in (_STATE["cid_a"], _STATE["cid_b"]):
        AttendanceCheckin.query.filter_by(company_id=_cid).delete()
        AttendanceException.query.filter_by(company_id=_cid).delete()
    db.session.commit()


def _client_as(user_id, company_id):
    """Fresh test client bound to (user_id, company_id)."""
    c = _STATE["app"].test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["active_company_id"] = company_id
    return c


def _mk_checkin(company_id, employee_id, on_date, *, hi=9, ho=17):
    from app.models import AttendanceCheckin
    r = AttendanceCheckin(
        company_id=company_id, employee_id=employee_id,
        date=on_date,
        check_in_time=datetime.combine(on_date, time(hi, 0)),
        check_out_time=datetime.combine(on_date, time(ho, 0)))
    db.session.add(r); db.session.commit()
    return r


def _mk_ex(company_id, employee_id, on_date, ex_type):
    """Insert an AttendanceException directly. Bypasses the
    service (which enforces its own rules) so we can seed a
    specific test scenario cheaply."""
    from app.models import AttendanceException, AttendanceExceptionType
    ex = AttendanceException(
        company_id=company_id, employee_id=employee_id,
        date=on_date,
        type=AttendanceExceptionType[ex_type])
    db.session.add(ex); db.session.commit()
    return ex


# ─── Checks ────────────────────────────────────────────────────
@check("1. checkins_in_period returns dict keyed by (emp_id, date)")
def _():
    from app.services.attendance import checkins_in_period
    _reset_att()
    today = date.today()
    ym = (today.year, today.month)
    _mk_checkin(_STATE["cid_a"], _STATE["emp_a1"], today)

    out = checkins_in_period(_STATE["cid_a"], ym[0], ym[1])
    assert isinstance(out, dict), type(out)
    assert (_STATE["emp_a1"], today) in out, list(out.keys())


@check("2. checkins_in_period filters by employee_id when given")
def _():
    from app.services.attendance import checkins_in_period
    _reset_att()
    today = date.today()
    _mk_checkin(_STATE["cid_a"], _STATE["emp_a1"], today)
    _mk_checkin(_STATE["cid_a"], _STATE["emp_a2"], today)

    out = checkins_in_period(
        _STATE["cid_a"], today.year, today.month,
        employee_id=_STATE["emp_a1"])
    keys = set(out.keys())
    assert (_STATE["emp_a1"], today) in keys
    assert (_STATE["emp_a2"], today) not in keys, \
        "employee_id filter leaked"


@check("3. GET /hr/attendance shows حاضر chip for a check-in day")
def _():
    _reset_att()
    today = date.today()
    _mk_checkin(_STATE["cid_a"], _STATE["emp_a1"], today)

    c = _client_as(_STATE["hr_a"], _STATE["cid_a"])
    r = c.get(f"/hr/attendance?year={today.year}&month={today.month}")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # Emerald chip + hover tooltip that reveals the check-in time.
    assert "att-present" in body, "presence chip class missing"
    assert "دخل:" in body, "check-in tooltip text missing"


@check("4. GET /hr/attendance shows 'لم يسجّل' for a past day with neither")
def _():
    _reset_att()
    today = date.today()
    c = _client_as(_STATE["hr_a"], _STATE["cid_a"])
    r = c.get(f"/hr/attendance?year={today.year}&month={today.month}")
    body = r.get_data(as_text=True)
    # The chip class + the "no-record" title marker only appear when
    # at least one past day has neither ex nor ci — trivially true
    # unless today is the 1st of the month.
    if today.day > 1:
        assert "att-no-record" in body, "no-record chip missing"
        assert "لم يسجّل" in body


@check("5. Existing 4 exception color dots still render")
def _():
    _reset_att()
    today = date.today()
    d = date(today.year, today.month, 1)
    if d >= today:
        # Extreme edge (running on the 1st): skip — same case is
        # already covered by audit_exception_audit_trail.
        return
    _mk_ex(_STATE["cid_a"], _STATE["emp_a1"], d, "ABSENT")

    c = _client_as(_STATE["hr_a"], _STATE["cid_a"])
    r = c.get(f"/hr/attendance?year={today.year}&month={today.month}")
    body = r.get_data(as_text=True)
    # Rose dot for ABSENT — the existing 4-branch dispatcher must
    # still fire when an exception exists.
    assert "bg-rose-500" in body, \
        "existing ABSENT rose dot missing — regression"


@check("6. Employee name in grid links to the detail route")
def _():
    _reset_att()
    today = date.today()
    c = _client_as(_STATE["hr_a"], _STATE["cid_a"])
    r = c.get(f"/hr/attendance?year={today.year}&month={today.month}")
    body = r.get_data(as_text=True)
    # The url_for target should be present for our fixture emp.
    needle = f"/hr/attendance/employee/{_STATE['emp_a1']}"
    assert needle in body, f"detail link {needle!r} not in grid"


@check("7. Detail route renders per-day rows for the fixture employee")
def _():
    _reset_att()
    today = date.today()
    _mk_checkin(_STATE["cid_a"], _STATE["emp_a1"], today,
                 hi=9, ho=17)

    c = _client_as(_STATE["hr_a"], _STATE["cid_a"])
    r = c.get(
        f"/hr/attendance/employee/{_STATE['emp_a1']}"
        f"?year={today.year}&month={today.month}")
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    # Today's date + check-in time surface.
    assert today.isoformat() in body, "today's date row missing"
    assert "09:00" in body, "check-in time missing"
    assert "17:00" in body, "check-out time missing"


@check("8. Cross-tenant employee_id -> 404; team_member -> 403")
def _():
    _reset_att()
    today = date.today()

    # HR of company A trying to view company B's employee → 404.
    c_hr = _client_as(_STATE["hr_a"], _STATE["cid_a"])
    r_cross = c_hr.get(
        f"/hr/attendance/employee/{_STATE['emp_b']}"
        f"?year={today.year}&month={today.month}",
        follow_redirects=False)
    assert r_cross.status_code == 404, \
        f"cross-tenant employee_id should 404, got {r_cross.status_code}"

    # Flask-Login memoizes current_user on g._login_user per
    # app-context; the outer app.app_context() in main() carries
    # hr_a across into the next request. Force a fresh load so the
    # team_member's session cookie actually resolves to team_member.
    from flask import g
    try:
        g.pop("_login_user", None)
    except (KeyError, AttributeError, RuntimeError):
        pass
    db.session.expire_all()
    db.session.remove()

    c_tm = _client_as(_STATE["tm_a"], _STATE["cid_a"])
    r_tm = c_tm.get(
        f"/hr/attendance?year={today.year}&month={today.month}",
        follow_redirects=False)
    assert r_tm.status_code == 403, \
        f"team_member should get 403, got {r_tm.status_code}"


@check("9. endpoint_to_subitem maps detail route to 'hr.attendance'")
def _():
    """Would have caught the 302/403 the boss saw: hr.employee_attendance_detail
    fell through to 'hr.index' via the generic 'hr.*' branch, so a tenant
    with hr.attendance but NOT hr.index in allowed_subitems 403'd the
    detail page. Explicit mapping now groups both under hr.attendance."""
    from app.services.plan_gating import endpoint_to_subitem
    assert endpoint_to_subitem("hr.attendance") == "hr.attendance"
    assert endpoint_to_subitem("hr.employee_attendance_detail") == "hr.attendance", \
        "detail route must inherit the attendance gate, not hr.index"


@check("10. detail route stays 200 when hr.index is NOT in the plan's subitems")
def _():
    """Regression proof for the 2026-08-08 revert scenario. Reproduce the
    exact plan config the boss's tenant had: allowed_subitems includes
    hr.attendance but excludes hr.index. Before the fix, the detail
    request 403'd via enforce_subitem_gating."""
    from app.models import Plan, Company
    # Force a fresh Flask-Login user_loader + ORM session — check 8's
    # cached current_user + stale plan relationship leak forward and
    # make the fixture's plan changes invisible to the next request.
    from flask import g
    try:
        g.pop("_login_user", None)
    except (KeyError, AttributeError, RuntimeError):
        pass
    db.session.expire_all()
    db.session.remove()

    plan = Plan.query.filter_by(code="__hratt__").first()
    original = plan.subitems
    try:
        plan.set_subitems(["hr.attendance"])   # explicitly exclude hr.index
        db.session.commit()
        # Sanity: verify the endpoint mapper agrees before hitting HTTP.
        from app.services.plan_gating import (
            endpoint_to_subitem, subitem_allowed,
        )
        co = db.session.get(Company, _STATE["cid_a"])
        assert endpoint_to_subitem("hr.employee_attendance_detail") == "hr.attendance"
        assert subitem_allowed("hr.attendance", co) is True, \
            "sanity: subitem_allowed should pass for hr.attendance now"

        client = _client_as(_STATE["hr_a"], _STATE["cid_a"])
        r = client.get(
            f"/hr/attendance/employee/{_STATE['emp_a1']}"
            f"?year={date.today().year}&month={date.today().month}",
            follow_redirects=False)
        assert r.status_code == 200, \
            f"detail route 403'd when hr.attendance was allowed but hr.index wasn't; got {r.status_code}"
    finally:
        # Refresh + restore the original subitems.
        db.session.expire_all()
        plan = Plan.query.filter_by(code="__hratt__").first()
        if plan is not None:
            plan.set_subitems(original)
            db.session.commit()


# ─── Runner ────────────────────────────────────────────────────
def main():
    app = create_app()
    _STATE["app"] = app
    passed = failed = 0
    failures = []
    with app.app_context():
        _setup()
        try:
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
                    failures.append(
                        (label, f"{type(e).__name__}: {e}"))
                    _p(f"  [ERROR] {label}: {type(e).__name__}: {e}")
        finally:
            _teardown()
    _p("")
    _p(f"audit_hr_attendance_view: {passed} passed, {failed} failed")
    if failures:
        for label, err in failures:
            _p(f"  - {label} :: {err}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
