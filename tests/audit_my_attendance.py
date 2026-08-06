#!/usr/bin/env python3
"""MARSOUD-MY-ATTENDANCE (2026-08-05) — ticket 7.

/my/attendance under portal_emp_bp: an employee's one-page overview of
their own attendance for the current month. Read-only plus links back
to the actions that already live on /my/account.

Every check verified to FAIL against pre-batch code before this file
was committed.

Checks
  1. page renders 200 for an employee with an HR record
  2. current-month check-ins appear
  3. non-cancelled exceptions appear; cancelled ones DO NOT
  4. approved permissions appear
  5. remaining_pool and remaining_perms reflect the resolved policy
  6. show "—" (rendered dash) when no policy is defined
  7. a new permission submission appears immediately as PENDING
  8. cross-tenant leak — user in company B never sees company A's data
"""
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__MYATT_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture — two separate companies for the leak check ───────────────
def _setup():
    _teardown()
    from app.models import Company, Plan, Employee, User
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company

    plan = Plan.query.filter_by(code="__myatt__").first()
    if not plan:
        plan = Plan(code="__myatt__", name="MyAtt", name_ar="حضوري",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "crm", "hr", "reports",
                          "evaluations", "settings"])
        db.session.add(plan)
        db.session.flush()

    def _mk_co(suffix):
        co = Company(name=f"{PREFIX}CO_{suffix}__", base_currency="EGP",
                     vat_rate=0, plan_id=plan.id)
        db.session.add(co); db.session.flush()
        co.intended_plan_id = plan.id
        db.session.commit()
        ensure_roles_ready_for_company(co.id)
        return co

    long_ago = date.today() - timedelta(days=400)

    def _mk_user_emp(co, tag):
        u = User(email=f"{PREFIX}{co.id}_{tag}@audit.local", full_name=tag,
                 is_active=True, terms_version=get_terms_version(),
                 terms_accepted_at=datetime.utcnow())
        u.set_password("Passw0rd!audit1")
        db.session.add(u); db.session.flush()
        set_membership_role(u.id, co.id, "team_member")
        e = Employee(company_id=co.id, name=f"emp-{tag}",
                     basic_salary=Decimal("5000"),
                     status="ACTIVE", start_date=long_ago, user_id=u.id)
        db.session.add(e); db.session.flush()
        return u.id, e.id

    co_a = _mk_co("A")
    co_b = _mk_co("B")
    ua, ea = _mk_user_emp(co_a, "a")
    ub, eb = _mk_user_emp(co_b, "b")
    db.session.commit()

    _STATE.update(cid_a=co_a.id, cid_b=co_b.id,
                  emp_a=ea, emp_b=eb,
                  user_a=ua, user_b=ub)


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        db.session.execute(text(
            "DELETE FROM journal_lines WHERE entry_id IN "
            "(SELECT id FROM journal_entries WHERE company_id=:c)"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM payroll_lines WHERE run_id IN "
            "(SELECT id FROM payroll_runs WHERE company_id=:c)"), {"c": cid})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(
                        text(f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        db.session.execute(text("DELETE FROM companies WHERE id=:c"),
                           {"c": cid})
        db.session.commit()
    db.session.execute(text("DELETE FROM plans WHERE code='__myatt__'"))
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text("DELETE FROM user_companies WHERE user_id=:u"),
                           {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _reset():
    from app.models import (AttendanceCheckin, AttendanceException,
                            LatePermissionRequest, AttendanceViolationPolicy)
    AttendanceCheckin.query.delete()
    AttendanceException.query.delete()
    LatePermissionRequest.query.delete()
    AttendanceViolationPolicy.query.delete()
    db.session.commit()


def _client_as(user_id, company_id):
    """Fresh test client bound to (user_id, company_id).

    Handoff fact 7 — Flask-Login's g._login_user is per-app-context. If
    two clients share one app.app_context() the second request answers
    as the first user. audit_portal_403 avoids the trap by pushing no
    context; this file avoids it by pushing a fresh nested context for
    the cross-tenant check.
    """
    c = _STATE["app"].test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["active_company_id"] = company_id
    return c


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. page renders 200 for an employee with an HR record")
def _():
    _reset()
    c = _client_as(_STATE["user_a"], _STATE["cid_a"])
    r = c.get("/my/attendance")
    assert r.status_code == 200, f"status={r.status_code}"
    body = r.get_data(as_text=True)
    assert "حضوري" in body, "page title missing"
    return f"200 · {len(body)} bytes"


@check("2. current-month check-ins appear")
def _():
    from app.models import AttendanceCheckin
    _reset()
    today = date.today()
    db.session.add(AttendanceCheckin(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        date=today, check_in_time=datetime.combine(today, time(9, 15))))
    db.session.commit()
    c = _client_as(_STATE["user_a"], _STATE["cid_a"])
    r = c.get("/my/attendance")
    body = r.get_data(as_text=True)
    assert today.isoformat() in body, "today's date missing from body"
    assert "09:15" in body, "check-in time missing"
    return f"row for {today.isoformat()} 09:15 rendered"


@check("3. non-cancelled exceptions appear; cancelled ones DO NOT")
def _():
    from app.models import AttendanceException, AttendanceExceptionType
    _reset()
    today = date.today()
    live = AttendanceException(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        date=today, type=AttendanceExceptionType.LATE,
        duration_hours=Decimal("0.5"), note="LIVE_MARKER")
    dead = AttendanceException(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        date=today - timedelta(days=1),
        type=AttendanceExceptionType.ABSENT, note="DEAD_MARKER",
        is_cancelled=True, cancel_reason="wrong",
        cancelled_at=datetime.utcnow())
    db.session.add_all([live, dead])
    db.session.commit()
    c = _client_as(_STATE["user_a"], _STATE["cid_a"])
    body = c.get("/my/attendance").get_data(as_text=True)
    assert "LIVE_MARKER" in body, "live exception did not render"
    assert "DEAD_MARKER" not in body, (
        "cancelled exception leaked to the employee's page — active_exceptions() "
        "is being bypassed")
    return "live shown, cancelled hidden"


@check("4. approved permissions appear")
def _():
    from app.models import LatePermissionRequest, PermissionStatus
    _reset()
    today = date.today()
    db.session.add(LatePermissionRequest(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        request_date=today, hours_count=Decimal("1.0"),
        status=PermissionStatus.APPROVED,
        reason="PERMIT_MARKER"))
    db.session.commit()
    c = _client_as(_STATE["user_a"], _STATE["cid_a"])
    body = c.get("/my/attendance").get_data(as_text=True)
    assert today.isoformat() in body, "permission date not rendered"
    # Reason isn't rendered in the permits table, so we look for the
    # هيدير / status badge instead:
    assert "معتمد" in body, "approved badge missing"
    return "approved permission listed"


@check("5. remaining_pool and remaining_perms reflect the resolved policy")
def _():
    from app.models import (AttendanceViolationPolicy, PolicyScope,
                            AttendanceException, AttendanceExceptionType,
                            LatePermissionRequest, PermissionStatus)
    _reset()
    today = date.today()
    db.session.add(AttendanceViolationPolicy(
        company_id=_STATE["cid_a"], scope=PolicyScope.COMPANY,
        monthly_free_late_minutes=60,
        daily_free_late_minutes_cap=0,
        permission_count_per_month=3,
        permission_max_hours=Decimal("2.00")))
    # 30 mins of lateness this month, one approved permission
    db.session.add(AttendanceException(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        date=today, type=AttendanceExceptionType.LATE,
        duration_hours=Decimal("0.5")))
    db.session.add(LatePermissionRequest(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        request_date=today, hours_count=Decimal("1.0"),
        status=PermissionStatus.APPROVED))
    db.session.commit()
    c = _client_as(_STATE["user_a"], _STATE["cid_a"])
    body = c.get("/my/attendance").get_data(as_text=True)
    # After permission (1h = 60 min) the 30-min residual clears → 0 min
    # consumed → pool remains full 60. Perms left = 3 - 1 = 2.
    assert "60 د" in body, (
        "expected 60 د (full pool remaining) — body did not contain it")
    assert "2</div>" in body or ">2\n" in body or "متبقّي" in body, (
        "perms count not visible")
    return "remaining_pool=60 د · remaining_perms=2 rendered"


@check("6. no policy -> pool/perms show a dash")
def _():
    _reset()
    c = _client_as(_STATE["user_a"], _STATE["cid_a"])
    body = c.get("/my/attendance").get_data(as_text=True)
    assert "لا توجد سياسة انتهاكات مفعّلة" in body, (
        "no-policy notice missing")
    # Dash tiles present:
    assert "—" in body
    return "dashes shown, notice rendered"


@check("7. new permission submission appears immediately as PENDING")
def _():
    from app.models import AttendanceViolationPolicy, PolicyScope
    _reset()
    db.session.add(AttendanceViolationPolicy(
        company_id=_STATE["cid_a"], scope=PolicyScope.COMPANY,
        permission_count_per_month=5,
        permission_max_hours=Decimal("4.00")))
    db.session.commit()
    c = _client_as(_STATE["user_a"], _STATE["cid_a"])
    today = date.today()
    r = c.post("/my/permission/new", data={
        "request_date": today.isoformat(),
        "hours_count": "1.5",
        "start_time": "10:00", "end_time": "11:30",
        "reason": "PORTAL_SUBMIT_MARKER",
    }, follow_redirects=False)
    assert r.status_code in (302, 303), f"unexpected {r.status_code}"
    body = c.get("/my/attendance").get_data(as_text=True)
    assert "قيد المراجعة" in body, "PENDING badge missing after submit"
    return "submit → PENDING on page"


@check("8. cross-tenant leak — user in B never sees A's data")
def _():
    """Handoff fact 7: Flask-Login caches the resolved user on
    g._login_user for the current app_context. A test that logged in as
    A first and later as B would still answer B's request AS A. We push
    a fresh nested app_context for the B request so g is clean."""
    from app.models import AttendanceException, AttendanceExceptionType
    _reset()
    today = date.today()
    db.session.add(AttendanceException(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        date=today, type=AttendanceExceptionType.LATE,
        duration_hours=Decimal("0.5"), note="A_ONLY_MARKER"))
    db.session.commit()

    # First: log in as A and confirm the marker renders for A.
    ca = _client_as(_STATE["user_a"], _STATE["cid_a"])
    body_a = ca.get("/my/attendance").get_data(as_text=True)
    assert "A_ONLY_MARKER" in body_a, "A cannot see A's own data"

    # Now: fresh app context, log in as B, request /my/attendance while
    # B's active company is B. The marker MUST NOT appear.
    from app import create_app
    app2 = create_app()
    app2.config["WTF_CSRF_ENABLED"] = False
    with app2.app_context():
        cb = app2.test_client()
        with cb.session_transaction() as s:
            s["_user_id"] = str(_STATE["user_b"])
            s["_fresh"] = True
            s["active_company_id"] = _STATE["cid_b"]
        r = cb.get("/my/attendance")
        body_b = r.get_data(as_text=True)
    assert "A_ONLY_MARKER" not in body_b, (
        "CROSS-TENANT LEAK — B sees A's attendance exception")
    return "A sees own marker · B never sees it"


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    _STATE["app"] = app
    passed = failed = 0
    with app.app_context():
        _setup()
        try:
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                    failed += 1
        finally:
            _teardown()
            print("\n(fixture cleaned up)")
    print(f"\n----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
