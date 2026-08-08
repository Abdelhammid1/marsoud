#!/usr/bin/env python3
"""MARSOUD-VIOLATION-POLICY (2026-08-05) — ticket 6.

Every acceptance criterion in the ticket is one check here. Each has
been verified to FAIL against pre-batch code before this file was
committed (git-stash the service edits, run once, confirm red, unstash)
so the suite proves it exercises the new behaviour.

The load-bearing check is #1 — the byte-for-byte no-policy regression.
The four EXPECTED_* constants below are payroll numbers captured from
a run against pre-batch HEAD, and #1 compares against them, not against
what the new code computes. That is the whole protection against a
same-code-vs-same-code false pass.

Checks
  1.  no violation policy defined -> payroll numbers byte-identical
  2.  daily cap forgives lateness inside it entirely
  3.  monthly pool absorbs the residual, day-by-day
  4.  approved LatePermissionRequest clears its day first
  5.  excused absence deducts less than unexcused
  6.  cross-tenant leak: company A cannot save a policy pointing at B
  7.  permission request refuses hours > policy.permission_max_hours
  8.  Nth+1 permission request in a month is refused
  9.  cancelled permission no longer clears the day
"""
import sys
from datetime import date, datetime, timedelta
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
PREFIX = "__VIOLPOL_"
_STATE = {}

# ─── Captured from a run against pre-batch HEAD (commit a66238f). ──────
#     A regression is anyone changing these values, not this test.
BASIC_SALARY = 6000
FIXTURE_YEAR = 2026
FIXTURE_MONTH = 6
FIXTURE_ABSENT_DATES = [date(2026, 6, 5), date(2026, 6, 6)]
FIXTURE_LATE_DATE = date(2026, 6, 7)
FIXTURE_LATE_HOURS = Decimal("0.5")

EXPECTED_ATT_DED = {
    "absence_days": 2.0,
    "late_days": 0.06,
    "approved_days": 0.0,
    "has_exceptions": True,
}
EXPECTED_ABSENCE_DEDUCTION = 400.0
EXPECTED_LATE_DEDUCTION = 12.0
EXPECTED_NET = 5588.0
EXPECTED_WORKING_DAYS = 30
EXPECTED_ABSENCES_COUNT = 2
EXPECTED_ATT_AUTO = True


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ────────────────────────────────────────────────────────────
def _setup():
    _teardown()
    from app.models import Company, Plan, Employee, User
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.seed_coa import seed_default_coa
    from app.services.subsidiary import ensure_employee_account

    plan = Plan.query.filter_by(code="__violpol__").first()
    if not plan:
        plan = Plan(code="__violpol__", name="ViolAudit", name_ar="تدقيق",
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
        seed_default_coa(co.id)
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
                     basic_salary=Decimal(str(BASIC_SALARY)),
                     status="ACTIVE", start_date=long_ago, user_id=u.id)
        db.session.add(e); db.session.flush()
        ensure_employee_account(e)
        return u.id, e.id

    co_a = _mk_co("A")
    co_b = _mk_co("B")
    u_a, e_a = _mk_user_emp(co_a, "a")
    u_b, e_b = _mk_user_emp(co_b, "b")
    db.session.commit()

    _STATE.update(cid_a=co_a.id, cid_b=co_b.id,
                  emp_a=e_a, emp_b=e_b,
                  user_a=u_a, user_b=u_b)


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
    db.session.execute(text("DELETE FROM plans WHERE code='__violpol__'"))
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text("DELETE FROM user_companies WHERE user_id=:u"),
                           {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _reset_month():
    """Wipe the month's exceptions, permissions and payroll rows without
    tearing down the companies. Between checks so each starts clean."""
    from app.models import (AttendanceException, LatePermissionRequest,
                            PayrollRun, PayrollLine, JournalEntry, JournalLine,
                            AttendanceViolationPolicy)
    from sqlalchemy import text
    for cid in (_STATE["cid_a"], _STATE["cid_b"]):
        eids = [r.id for r in JournalEntry.query.filter_by(
            company_id=cid).all()]
        if eids:
            JournalLine.query.filter(
                JournalLine.entry_id.in_(eids)
            ).delete(synchronize_session=False)
        rids = [r.id for r in PayrollRun.query.filter_by(
            company_id=cid).all()]
        if rids:
            PayrollLine.query.filter(
                PayrollLine.run_id.in_(rids)
            ).delete(synchronize_session=False)
    # MARSOUD-4-BRANCH-REPAIR (2026-08-08) — these 5 deletes were
    # tenant-agnostic; a run against a DB with real data would
    # nuke every company's rows (prod-data-loss pattern). Scope
    # to the two fixture companies established at setup.
    for _cid in (_STATE["cid_a"], _STATE["cid_b"]):
        AttendanceException.query.filter_by(company_id=_cid).delete()
        LatePermissionRequest.query.filter_by(company_id=_cid).delete()
        PayrollRun.query.filter_by(company_id=_cid).delete()
        (JournalEntry.query
         .filter(JournalEntry.company_id == _cid,
                 JournalEntry.source_type.in_(
                     ["payroll", "payroll_settlement"]))
         .delete(synchronize_session=False))
        AttendanceViolationPolicy.query.filter_by(company_id=_cid).delete()
    db.session.commit()


def _seed_baseline_month(cid, emp_id):
    """The exact fixture that produced the EXPECTED_* constants — 2 ABSENT
    (unexcused, is_excused=False) plus one LATE(0.5h)."""
    from app.models import AttendanceException, AttendanceExceptionType
    for d in FIXTURE_ABSENT_DATES:
        db.session.add(AttendanceException(
            company_id=cid, employee_id=emp_id,
            date=d, type=AttendanceExceptionType.ABSENT))
    db.session.add(AttendanceException(
        company_id=cid, employee_id=emp_id,
        date=FIXTURE_LATE_DATE,
        type=AttendanceExceptionType.LATE,
        duration_hours=FIXTURE_LATE_HOURS))
    db.session.commit()


def _run_and_read(cid, emp_id):
    from app.services.payroll import run_payroll
    from app.services.leave import attendance_deductions
    from app.models import PayrollLine
    info = attendance_deductions(emp_id, FIXTURE_YEAR, FIXTURE_MONTH)
    run = run_payroll(cid, FIXTURE_YEAR, FIXTURE_MONTH,
                      created_by=None, send_emails=False)
    line = PayrollLine.query.filter_by(
        run_id=run.id, employee_id=emp_id).first()
    return info, line


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. no violation policy -> payroll numbers byte-identical to pre-batch")
def _():
    _reset_month()
    _seed_baseline_month(_STATE["cid_a"], _STATE["emp_a"])
    info, line = _run_and_read(_STATE["cid_a"], _STATE["emp_a"])
    assert info == EXPECTED_ATT_DED, (
        f"attendance_deductions()={info!r}, expected {EXPECTED_ATT_DED!r} — "
        "byte-for-byte regression BROKEN. The no-policy branch has drifted.")
    assert float(line.absence_deduction) == EXPECTED_ABSENCE_DEDUCTION, (
        f"absence_deduction={line.absence_deduction}, "
        f"expected {EXPECTED_ABSENCE_DEDUCTION}")
    assert float(line.late_deduction) == EXPECTED_LATE_DEDUCTION, (
        f"late_deduction={line.late_deduction}, "
        f"expected {EXPECTED_LATE_DEDUCTION}")
    assert float(line.net) == EXPECTED_NET, (
        f"net={line.net}, expected {EXPECTED_NET}")
    return (f"absence={line.absence_deduction} late={line.late_deduction} "
            f"net={line.net} — matches pre-batch to the byte")


@check("2. daily cap forgives lateness inside it entirely")
def _():
    """cap=20/day, pool=0 → a 15-minute lateness costs nothing; a
    45-minute one charges only the excess (25 min)."""
    from app.models import (AttendanceException, AttendanceExceptionType,
                            AttendanceViolationPolicy, PolicyScope)
    _reset_month()
    db.session.add(AttendanceViolationPolicy(
        company_id=_STATE["cid_a"], scope=PolicyScope.COMPANY,
        daily_free_late_minutes_cap=20,
        monthly_free_late_minutes=0,
    ))
    db.session.add(AttendanceException(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        date=date(FIXTURE_YEAR, FIXTURE_MONTH, 10),
        type=AttendanceExceptionType.LATE,
        duration_hours=Decimal("0.25")))          # 15 min ≤ cap
    db.session.commit()
    from app.services.leave import attendance_deductions
    info = attendance_deductions(_STATE["emp_a"], FIXTURE_YEAR, FIXTURE_MONTH)
    assert info["late_days"] == 0.0, (
        f"15 min inside a 20-min cap still cost {info['late_days']} days — "
        "cap is not being applied")

    # Same day, now 45 min: charge (45-20)=25 min → 25/60/8 = 0.052 → 0.05
    # MARSOUD-4-BRANCH-REPAIR (2026-08-08) — was
    # db.session.query(AttendanceException).delete() (unscoped).
    (db.session.query(AttendanceException)
     .filter(AttendanceException.company_id == _STATE["cid_a"])
     .delete(synchronize_session=False))
    db.session.commit()
    db.session.add(AttendanceException(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        date=date(FIXTURE_YEAR, FIXTURE_MONTH, 11),
        type=AttendanceExceptionType.LATE,
        duration_hours=Decimal("0.75")))          # 45 min
    db.session.commit()
    info = attendance_deductions(_STATE["emp_a"], FIXTURE_YEAR, FIXTURE_MONTH)
    expected = round(25.0 / 60.0 / 8.0, 2)
    assert info["late_days"] == expected, (
        f"45 min above a 20-min cap charged {info['late_days']} days, "
        f"expected {expected}")
    return f"15 min → 0.00 days · 45 min above 20-min cap → {expected} days"


@check("3. monthly pool absorbs the residual across days")
def _():
    """cap=30/day, pool=20/month, three 45-min days: each day leaves 15
    over cap → 45 total residual → pool covers 20 → 25 min charged →
    25/60/8 = 0.052 → 0.05.

    The residual must EXCEED the pool to prove the pool bounds; a
    scenario where the pool comfortably covers everything would pass
    even if the pool did nothing."""
    from app.models import (AttendanceException, AttendanceExceptionType,
                            AttendanceViolationPolicy, PolicyScope)
    _reset_month()
    db.session.add(AttendanceViolationPolicy(
        company_id=_STATE["cid_a"], scope=PolicyScope.COMPANY,
        daily_free_late_minutes_cap=30,
        monthly_free_late_minutes=20,
    ))
    for d in (12, 13, 14):
        db.session.add(AttendanceException(
            company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
            date=date(FIXTURE_YEAR, FIXTURE_MONTH, d),
            type=AttendanceExceptionType.LATE,
            duration_hours=Decimal("0.75")))
    db.session.commit()
    from app.services.leave import attendance_deductions
    info = attendance_deductions(_STATE["emp_a"], FIXTURE_YEAR, FIXTURE_MONTH)
    expected = round(25.0 / 60.0 / 8.0, 2)
    assert info["late_days"] == expected, (
        f"three 45-min days cap=30 pool=20 → charged {info['late_days']}, "
        f"expected {expected} (25 residual minutes)")

    # And a fourth 45-min day now that the pool is drained: charges the
    # full 15 residual → cumulative expected = (25+15)/60/8 = 0.083 → 0.08
    db.session.add(AttendanceException(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        date=date(FIXTURE_YEAR, FIXTURE_MONTH, 15),
        type=AttendanceExceptionType.LATE,
        duration_hours=Decimal("0.75")))
    db.session.commit()
    info = attendance_deductions(_STATE["emp_a"], FIXTURE_YEAR, FIXTURE_MONTH)
    expected2 = round(40.0 / 60.0 / 8.0, 2)
    assert info["late_days"] == expected2, (
        f"pool drained then fourth day: charged {info['late_days']}, "
        f"expected {expected2}")
    return f"3-day residual {expected} days · 4th day drains pool → {expected2}"


@check("4. approved LatePermissionRequest clears its day BEFORE cap+pool")
def _():
    """cap=0, pool=0 (no allowance), 60-min lateness, but a 60-min
    approved permission → 0 minutes remain → 0 days charged."""
    from app.models import (AttendanceException, AttendanceExceptionType,
                            AttendanceViolationPolicy, PolicyScope,
                            LatePermissionRequest, PermissionStatus)
    _reset_month()
    db.session.add(AttendanceViolationPolicy(
        company_id=_STATE["cid_a"], scope=PolicyScope.COMPANY,
        permission_count_per_month=5,
        permission_max_hours=Decimal("2.00"),
    ))
    d = date(FIXTURE_YEAR, FIXTURE_MONTH, 15)
    db.session.add(AttendanceException(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        date=d, type=AttendanceExceptionType.LATE,
        duration_hours=Decimal("1.0")))
    db.session.add(LatePermissionRequest(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        request_date=d, hours_count=Decimal("1.0"),
        status=PermissionStatus.APPROVED))
    db.session.commit()
    from app.services.leave import attendance_deductions
    info = attendance_deductions(_STATE["emp_a"], FIXTURE_YEAR, FIXTURE_MONTH)
    assert info["late_days"] == 0.0, (
        f"approved permission did not clear the day: {info['late_days']}")
    return "60-min permission covered 60-min lateness → 0 days"


@check("5. excused absence deducts less than unexcused")
def _():
    """excused=0.5, unexcused=3.0. Two absences: one excused, one not
    → 0.5 + 3.0 = 3.5 days."""
    from app.models import (AttendanceException, AttendanceExceptionType,
                            AttendanceViolationPolicy, PolicyScope)
    _reset_month()
    db.session.add(AttendanceViolationPolicy(
        company_id=_STATE["cid_a"], scope=PolicyScope.COMPANY,
        absence_excused_deduction_days=Decimal("0.50"),
        absence_unexcused_deduction_days=Decimal("3.00"),
    ))
    db.session.add(AttendanceException(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        date=date(FIXTURE_YEAR, FIXTURE_MONTH, 20),
        type=AttendanceExceptionType.ABSENT, is_excused=True))
    db.session.add(AttendanceException(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        date=date(FIXTURE_YEAR, FIXTURE_MONTH, 21),
        type=AttendanceExceptionType.ABSENT, is_excused=False))
    db.session.commit()
    from app.services.leave import attendance_deductions
    info = attendance_deductions(_STATE["emp_a"], FIXTURE_YEAR, FIXTURE_MONTH)
    assert info["absence_days"] == 3.5, (
        f"excused+unexcused = {info['absence_days']}, expected 3.5")
    return "excused 0.5 + unexcused 3.0 = 3.5 days"


@check("6. cross-tenant leak: A cannot save a policy pointing at B's employee")
def _():
    """The listing renders p.employee.name — a hand-crafted POST saving
    a foreign employee_id would put B's name on A's screen. Same shape
    as the attendance-policy leak _validate_target catches in ticket 1."""
    from app.services.violation import create_violation_policy, ViolationError
    _reset_month()
    try:
        create_violation_policy(
            company_id=_STATE["cid_a"], scope="EMPLOYEE",
            employee_id=_STATE["emp_b"])   # B's employee, filed against A
    except ViolationError as e:
        return f"refused: {e}"
    assert False, "cross-tenant policy created — the leak is open"


@check("7. permission request refuses hours > policy.permission_max_hours")
def _():
    from app.models import AttendanceViolationPolicy, PolicyScope
    from app.services.violation import (
        submit_permission_request, ViolationError,
    )
    _reset_month()
    db.session.add(AttendanceViolationPolicy(
        company_id=_STATE["cid_a"], scope=PolicyScope.COMPANY,
        permission_count_per_month=5,
        permission_max_hours=Decimal("2.00")))
    db.session.commit()
    try:
        submit_permission_request(
            company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
            request_date=date.today(), hours_count=Decimal("3.0"))
    except ViolationError as e:
        return f"refused: {e}"
    assert False, "over-cap permission accepted"


@check("8. Nth+1 permission request in a month is refused")
def _():
    from app.models import AttendanceViolationPolicy, PolicyScope
    from app.services.violation import (
        submit_permission_request, ViolationError,
    )
    _reset_month()
    db.session.add(AttendanceViolationPolicy(
        company_id=_STATE["cid_a"], scope=PolicyScope.COMPANY,
        permission_count_per_month=2,
        permission_max_hours=Decimal("4.00")))
    db.session.commit()
    submit_permission_request(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        request_date=date(FIXTURE_YEAR, FIXTURE_MONTH, 22),
        hours_count=Decimal("1.0"))
    submit_permission_request(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        request_date=date(FIXTURE_YEAR, FIXTURE_MONTH, 23),
        hours_count=Decimal("1.0"))
    try:
        submit_permission_request(
            company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
            request_date=date(FIXTURE_YEAR, FIXTURE_MONTH, 24),
            hours_count=Decimal("1.0"))
    except ViolationError as e:
        return f"3rd refused with cap=2: {e}"
    assert False, "3rd permission request accepted with cap=2"


@check("9. cancelled permission no longer clears its day")
def _():
    from app.models import (AttendanceException, AttendanceExceptionType,
                            AttendanceViolationPolicy, PolicyScope,
                            LatePermissionRequest, PermissionStatus)
    _reset_month()
    db.session.add(AttendanceViolationPolicy(
        company_id=_STATE["cid_a"], scope=PolicyScope.COMPANY,
        permission_count_per_month=5,
        permission_max_hours=Decimal("2.00")))
    d = date(FIXTURE_YEAR, FIXTURE_MONTH, 25)
    db.session.add(AttendanceException(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        date=d, type=AttendanceExceptionType.LATE,
        duration_hours=Decimal("1.0")))
    p = LatePermissionRequest(
        company_id=_STATE["cid_a"], employee_id=_STATE["emp_a"],
        request_date=d, hours_count=Decimal("1.0"),
        status=PermissionStatus.CANCELLED)
    db.session.add(p)
    db.session.commit()
    from app.services.leave import attendance_deductions
    info = attendance_deductions(_STATE["emp_a"], FIXTURE_YEAR, FIXTURE_MONTH)
    # No cap, no pool, cancelled permission ignored → 60 min charged
    expected = round(60.0 / 60.0 / 8.0, 2)
    assert info["late_days"] == expected, (
        f"cancelled permission still clearing the day: got {info['late_days']}, "
        f"expected {expected}")
    return f"cancelled permission ignored → {expected} days charged"


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
