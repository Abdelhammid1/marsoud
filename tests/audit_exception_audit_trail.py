#!/usr/bin/env python3
"""MARSOUD-EXCEPTION-AUDIT (2026-08-05) — ticket 5.

delete_exception() did a hard db.session.delete(): the row vanished with
no record of who removed it or why. An attendance exception is money — it
deducts a day's pay — so removing one silently is exactly what an audit
needs to be able to see.

THE LOAD-BEARING HALF IS NOT THE COLUMNS, IT IS THE QUERY SWEEP. Every
read that feeds payroll has to exclude cancelled rows, or cancelling one
goes on costing the employee. The filter lives in ONE place,
`active_exceptions()`, because scattered copies are how one ends up
missing.

And the opposite for the UI: a cancelled row STAYS on the attendance
screen, struck through and marked ملغى. Hiding it would defeat the audit
trail this ticket exists to create.

Checks
  1.  cancelling requires a reason
  2.  the row survives, stamped with who/when/why
  3.  a cancelled exception cannot be cancelled twice
  4.  leave-linked exceptions are still refused
  5.  THE ONE THAT MATTERS: a cancelled exception costs nothing at payroll
  6.  …and the day is freed, so it can be re-recorded
  7.  a cancelled row is invisible to the reporting queries
  8.  …but still visible on the HR screen, marked
  9.  a cancelled exception no longer blocks a leave request
  10. cancelling a leave request still hard-deletes its exceptions
  11. every payroll-feeding query goes through active_exceptions()
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

# MARSOUD-4-BRANCH-REPAIR (2026-08-08) — refuse unscoped bulk
# deletes on the attendance tables (prod-data-loss incident).
import tests._audit_guard as _audit_guard  # noqa: E402
_audit_guard.install()

CHECKS = []
PREFIX = "__EXCAUD_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import Company, Plan, Employee, User
    from app.services.seed_coa import seed_default_coa
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company

    plan = Plan.query.filter_by(code="__excaud__").first()
    if not plan:
        plan = Plan(code="__excaud__", name="Audit", name_ar="تدقيق",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "crm", "hr", "reports",
                          "evaluations", "settings"])
        db.session.add(plan)
        db.session.flush()
    co = Company(name=f"{PREFIX}CO__", base_currency="EGP", vat_rate=0,
                 plan_id=plan.id)
    db.session.add(co)
    db.session.flush()
    seed_default_coa(co.id)
    co.intended_plan_id = plan.id
    db.session.commit()
    ensure_roles_ready_for_company(co.id)

    u = User(email=f"{PREFIX}hr@audit.local", full_name="ExcAud HR",
             is_active=True, terms_version=get_terms_version(),
             terms_accepted_at=datetime.utcnow())
    u.set_password("Passw0rd!audit1")
    db.session.add(u)
    db.session.flush()
    db.session.commit()
    set_membership_role(u.id, co.id, "owner")

    emp = Employee(company_id=co.id, name="موظف التدقيق", basic_salary=6000,
                   status="ACTIVE", start_date=date(2025, 1, 1), user_id=u.id)
    db.session.add(emp)
    db.session.commit()
    _STATE.update(cid=co.id, uid=u.id, emp=emp.id)


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
    db.session.execute(text("DELETE FROM plans WHERE code='__excaud__'"))
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text("DELETE FROM user_companies WHERE user_id=:u"),
                           {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _reset():
    # MARSOUD-4-BRANCH-REPAIR (2026-08-08) — was
    #   AttendanceException.query.delete()
    # → wiped every tenant's exceptions if run against a DB with
    # real data. Scope the delete to the fixture company only.
    from app.models import AttendanceException
    _cid = _STATE["cid"]
    AttendanceException.query.filter_by(company_id=_cid).delete()
    db.session.commit()


def _absence(day=None):
    from app.models import AttendanceExceptionType
    from app.services.leave import create_exception
    return create_exception(
        company_id=_STATE["cid"], employee_id=_STATE["emp"],
        date_=day or date(2026, 5, 4),
        type_=AttendanceExceptionType.ABSENT, note="غياب للتدقيق")


def _client():
    c = _STATE["app"].test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(_STATE["uid"])
        s["_fresh"] = True
        s["active_company_id"] = _STATE["cid"]
    return c


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. cancelling requires a reason")
def _():
    from app.services.leave import cancel_exception, LeaveError
    from app.models import AttendanceException
    _reset()
    ex = _absence()
    for bad in (None, "", "   "):
        try:
            cancel_exception(ex, reason=bad, actor_id=_STATE["uid"])
            raise AssertionError(f"cancelled with reason={bad!r}")
        except LeaveError:
            pass
    db.session.refresh(ex)
    assert ex.is_cancelled is False, "a refused cancellation still stamped it"
    return "None, empty and whitespace all refused"


@check("2. the row survives, stamped with who, when and why")
def _():
    from app.models import AttendanceException
    from app.services.leave import cancel_exception
    _reset()
    ex = _absence()
    ex_id = ex.id
    cancel_exception(ex, reason="خطأ إدخال من الموارد البشرية",
                     actor_id=_STATE["uid"])
    row = db.session.get(AttendanceException, ex_id)
    assert row is not None, "the row was deleted instead of cancelled"
    assert row.is_cancelled is True
    assert row.cancelled_by == _STATE["uid"]
    assert row.cancelled_at is not None
    assert "خطأ إدخال" in row.cancel_reason
    return f"row {ex_id} kept, cancelled_by={row.cancelled_by}"


@check("3. a cancelled exception cannot be cancelled twice")
def _():
    from app.services.leave import cancel_exception, LeaveError
    _reset()
    ex = _absence()
    cancel_exception(ex, reason="مرة أولى", actor_id=_STATE["uid"])
    first_at = ex.cancelled_at
    try:
        cancel_exception(ex, reason="مرة ثانية", actor_id=_STATE["uid"])
        raise AssertionError("cancelled twice")
    except LeaveError:
        pass
    db.session.refresh(ex)
    assert ex.cancelled_at == first_at, "the second attempt moved the stamp"
    assert ex.cancel_reason == "مرة أولى", "the original reason was overwritten"
    return "second attempt refused, first stamp intact"


@check("4. an exception attached to a leave request is still refused")
def _():
    from app.services.leave import cancel_exception, LeaveError
    from app.models import AttendanceException, AttendanceExceptionType
    _reset()
    ex = _absence()
    ex.leave_request_id = 999999
    db.session.commit()
    try:
        cancel_exception(ex, reason="محاولة", actor_id=_STATE["uid"])
        raise AssertionError("a leave-linked exception was cancelled")
    except LeaveError as e:
        msg = str(e)
    db.session.refresh(ex)
    assert ex.is_cancelled is False
    return msg[:52]


@check("5. THE ONE THAT MATTERS: a cancelled exception costs nothing")
def _():
    """The whole point. Cancelling must reach the payslip — otherwise the
    audit trail is cosmetic and the employee still loses the day."""
    from app.models import PayrollLine
    from app.services.leave import cancel_exception, attendance_deductions
    from app.services.payroll import run_payroll
    _reset()
    ex = _absence(date(2026, 5, 4))

    before = attendance_deductions(_STATE["emp"], 2026, 5)
    assert before["absence_days"] == 1.0, (
        f"the fixture absence is not counting at all: {before}")

    cancel_exception(ex, reason="ألغتها الموارد البشرية",
                     actor_id=_STATE["uid"])
    after = attendance_deductions(_STATE["emp"], 2026, 5)
    assert after["absence_days"] == 0.0, (
        f"a cancelled absence still deducts {after['absence_days']} days — "
        "the employee keeps losing the money")
    assert after["has_exceptions"] is False, (
        "the month still reports exceptions after the only one was cancelled")

    run = run_payroll(_STATE["cid"], 2026, 5, line_inputs=None,
                      created_by=_STATE["uid"], send_emails=False)
    db.session.commit()
    line = PayrollLine.query.filter_by(
        run_id=run.id, employee_id=_STATE["emp"]).first()
    assert float(line.absence_deduction) == 0.0, (
        f"the payslip still deducts {line.absence_deduction} for a "
        "cancelled absence")
    return (f"1.0 day before -> 0.0 after; payslip absence_deduction "
            f"{float(line.absence_deduction):.2f}")


@check("6. KNOWN GAP: a cancelled day cannot be re-recorded")
def _():
    """Found by auditing. The table carries a database-level
    UNIQUE(employee_id, date), so the cancelled row still occupies its
    day — HR cannot cancel a wrong ABSENT and then enter the LATE that
    belonged there.

    Asserted as it actually behaves, not as I first assumed, so the day
    it changes someone has to change this check deliberately. The fix is
    a partial unique index (WHERE is_cancelled = false); it is out of
    scope here because the constraint is inline in the original CREATE
    TABLE and batch mode cannot rebuild this table — the `type` Enum
    generates an unnamed CHECK it refuses to copy.

    No acceptance criterion in ticket 5 depends on this. It is a
    usability gap, and it is written down rather than discovered by an
    HR manager mid-correction.
    """
    from app.models import AttendanceExceptionType, AttendanceException
    from app.services.leave import (cancel_exception, create_exception,
                                    LeaveError)
    _reset()
    day = date(2026, 5, 6)
    wrong = _absence(day)
    cancel_exception(wrong, reason="نوع خاطئ", actor_id=_STATE["uid"])
    try:
        create_exception(
            company_id=_STATE["cid"], employee_id=_STATE["emp"], date_=day,
            type_=AttendanceExceptionType.LATE, duration_hours=1.5,
            note="التصحيح")
        raise AssertionError(
            "the day is now re-recordable — the unique constraint was "
            "relaxed, so this check and the note in models/leave.py "
            "should both be updated")
    except LeaveError as e:
        # A CLEAN refusal, which is check 12's subject. Before that fix
        # this came back as a raw IntegrityError and took the request
        # with it.
        assert "ملغى" in str(e), f"unclear message: {e}"
    rows = AttendanceException.query.filter_by(
        employee_id=_STATE["emp"], date=day).all()
    assert len(rows) == 1 and rows[0].is_cancelled, (
        "the refused insert left something behind")
    return "re-recording refused cleanly by UNIQUE(employee_id, date)"


@check("7. a cancelled row is invisible to the reporting queries")
def _():
    from app.services.leave import cancel_exception, exceptions_in_period
    _reset()
    ex = _absence(date(2026, 5, 8))
    assert len(exceptions_in_period(_STATE["cid"], 2026, 5)) == 1
    cancel_exception(ex, reason="إلغاء", actor_id=_STATE["uid"])
    default = exceptions_in_period(_STATE["cid"], 2026, 5)
    assert len(default) == 0, (
        f"the default query still returns {len(default)} cancelled rows — "
        "any future report would count them")
    with_cancelled = exceptions_in_period(_STATE["cid"], 2026, 5,
                                          include_cancelled=True)
    assert len(with_cancelled) == 1, "opting in did not return the row"
    return "excluded by default, returned only when asked for"


@check("8. …but still visible on the HR screen, marked")
def _():
    from app.services.leave import cancel_exception
    _reset()
    ex = _absence(date(2026, 5, 11))
    cancel_exception(ex, reason="سبب ظاهر في الشاشة", actor_id=_STATE["uid"])
    body = _client().get("/hr/attendance?year=2026&month=5").get_data(
        as_text=True)
    assert "ملغى" in body, (
        "the cancelled exception vanished from the attendance screen — "
        "the audit trail is invisible")
    assert "سبب ظاهر في الشاشة" in body, "the reason is not shown"
    for leak in ("{{", "{%", "{#", "#}"):
        assert leak not in body, f"the screen leaks {leak}"
    return "row shown, marked ملغى, with its reason"


@check("9. a cancelled exception no longer blocks a leave request")
def _():
    """The clash check feeds a user-facing refusal. A cancelled day is a
    free day."""
    from app.services.leave import cancel_exception, active_exceptions
    from app.models import AttendanceException
    _reset()
    day = date(2026, 5, 13)
    ex = _absence(day)
    clash = active_exceptions().filter(
        AttendanceException.employee_id == _STATE["emp"],
        AttendanceException.date == day).count()
    assert clash == 1, "the fixture exception is not clashing to begin with"
    cancel_exception(ex, reason="إلغاء", actor_id=_STATE["uid"])
    clash_after = active_exceptions().filter(
        AttendanceException.employee_id == _STATE["emp"],
        AttendanceException.date == day).count()
    assert clash_after == 0, (
        "a cancelled exception still blocks a leave request for that day")
    return "clash 1 -> 0 after cancelling"


@check("10. cancelling a leave request still hard-deletes its exceptions")
def _():
    """Deliberately NOT soft-cancelled: those rows were generated by the
    approval, not entered by a person, so removing them is the exact
    inverse of creating them and the LeaveRequest carries the audit
    trail. Soft-cancelling would also leave them blocking their days."""
    src = (ROOT / "app/services/leave.py").read_text(encoding="utf-8")
    i = src.index("if was_approved:")
    window = src[i:i + 700]
    assert ".delete()" in window, (
        "the leave-cancellation path no longer hard-deletes; if that was "
        "deliberate, this check needs rewriting")
    assert "MARSOUD-EXCEPTION-AUDIT" in window, (
        "the deliberate difference is not explained in the code")
    return "hard delete kept, and the reason is written down"


@check("11. every payroll-feeding query goes through active_exceptions()")
def _():
    """The filter is one line, which is exactly why it needs one home.
    This fails if someone adds a new raw query to a payroll path."""
    src = (ROOT / "app/services/leave.py").read_text(encoding="utf-8")
    lines = src.splitlines()

    # Three raw uses are legitimate and named here explicitly, so a
    # FOURTH one — someone adding a query to a payroll path — fails this
    # check instead of quietly costing an employee a day's pay.
    ALLOWED = (
        "return AttendanceException.query.filter(",      # active_exceptions
        "q = (AttendanceException.query if include_cancelled",  # opt-in
        "AttendanceException.query.filter_by(leave_request_id=req.id)",
    )
    raw = [n for n, line in enumerate(lines, 1)
           if "AttendanceException.query" in line
           and not any(a in line for a in ALLOWED)]
    assert not raw, (
        f"raw AttendanceException.query at lines {raw} — route them "
        "through active_exceptions() or a cancelled row will keep "
        "costing the employee")
    from app.services.leave import active_exceptions
    from app.models import AttendanceException
    sql = str(active_exceptions())
    assert "is_cancelled" in sql, (
        f"active_exceptions() does not filter on is_cancelled: {sql}")
    return f"one raw query (the helper), filter confirmed in its SQL"


@check("12. a refused replacement is a clean error, not a 500")
def _():
    """Found by auditing. The duplicate check inside create_exception only
    sees ACTIVE rows, but the table's UNIQUE(employee_id, date) counts
    cancelled ones — so a cancelled day refused its replacement as a raw
    IntegrityError. Every caller catches LeaveError and none catches
    that, so it surfaced as a 500 on the check-in endpoint and would
    have killed the nightly absence sweep on its first bad day."""
    from app.models import AttendanceExceptionType, AttendanceCheckin
    from app.services.leave import (create_exception, cancel_exception,
                                    LeaveError)
    from app.services.attendance import create_policy, check_in
    from datetime import datetime, time as _time
    _reset()
    day = date(2026, 5, 20)
    ex = _absence(day)
    cancel_exception(ex, reason="إلغاء", actor_id=_STATE["uid"])

    try:
        create_exception(
            company_id=_STATE["cid"], employee_id=_STATE["emp"], date_=day,
            type_=AttendanceExceptionType.LATE, duration_hours=1)
        raise AssertionError(
            "the replacement was accepted — the unique constraint was "
            "relaxed, so check 6 and this one both need revisiting")
    except LeaveError as e:
        msg = str(e)
    assert "ملغى" in msg, f"the message does not explain why: {msg}"

    # the session must still be usable, or the whole request dies anyway
    other = create_exception(
        company_id=_STATE["cid"], employee_id=_STATE["emp"],
        date_=date(2026, 5, 21), type_=AttendanceExceptionType.ABSENT)
    assert other.id is not None, "the session was left unusable"

    # and the reachable path that found it: a late check-in on a
    # cancelled day must not 500
    # MARSOUD-4-BRANCH-REPAIR (2026-08-08) — scope to fixture company.
    AttendanceCheckin.query.filter_by(company_id=_STATE["cid"]).delete()
    db.session.commit()
    today_ex = _absence(date.today())
    cancel_exception(today_ex, reason="إلغاء", actor_id=_STATE["uid"])
    create_policy(company_id=_STATE["cid"], scope="COMPANY",
                  policy_type="FIXED", start_time=_time(9, 0),
                  end_time=_time(17, 0), work_days="0,1,2,3,4,5,6")
    from app.models import Employee
    emp = db.session.get(Employee, _STATE["emp"])
    row, exc = check_in(emp, now=datetime.combine(date.today(), _time(10, 0)))
    assert row is not None, "the check-in itself failed"
    assert exc is None, "an exception was created on a day that refuses one"
    return f"clean LeaveError; session survives; late check-in did not 500"


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
                    print(f"PASS  {label}\n        ⇒ {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}\n        ⇒ {type(e).__name__}: {e}")
                    failed += 1
        finally:
            _teardown()
            print("\n(fixture cleaned up)")
    print(f"\n────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
