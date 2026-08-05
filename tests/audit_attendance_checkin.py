#!/usr/bin/env python3
"""MARSOUD-ATTENDANCE-CHECKIN (2026-08-05) — tickets 2 and 4.

Until now the only attendance data was the exception: HR typing in, after
the fact, that someone was absent or late. Nobody recorded the ordinary
case of turning up, so there was nothing to measure an exception against.

Ticket 2 lets the employee record it themselves. Ticket 4 compares that
record against the policy from ticket 1 and creates the exception without
anyone typing anything.

THE DESIGN DECISION worth restating, because the tickets disagreed and
Zyad chose: THE EXCEPTION RECORDS THE RAW FACT. An employee 30 minutes
late gets an exception of 30 minutes even where the company allows 20
free — allowances are applied when the DEDUCTION is computed (ticket 6).
Netting the grace off here would make the attendance record disagree with
what happened and would then be subtracted twice. Check 8 pins it.

AND THE GUARANTEE, again: no policy → no automatic exception. A company
that has defined nothing keeps behaving exactly as it does today.

Checks
  1.  a check-in records time and coordinates
  2.  a refused location still records the check-in
  3.  one check-in per employee per day; the second is refused
  4.  check-out updates the same row, and needs a check-in first
  5.  check-out cannot be recorded twice
  6.  the math challenge gates both endpoints
  7.  a late arrival creates exactly one LATE exception
  8.  …carrying the TRUE minutes, not minutes-minus-grace
  9.  an on-time arrival creates nothing
  10. no policy -> no exception (the guarantee)
  11. a non-working day creates nothing
  12. a day that already has an exception is left alone
  13. the absence sweep marks who never arrived
  14. …and is idempotent across repeated ticks
  15. …and never judges today, only a finished day
  16. the portal renders and the endpoints reject a foreign employee
"""
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__ATTCHK_"
_STATE = {}


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

    plan = Plan.query.filter_by(code="__attchk__").first()
    if not plan:
        plan = Plan(code="__attchk__", name="Audit", name_ar="تدقيق",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "crm", "hr", "reports",
                          "evaluations", "settings"])
        db.session.add(plan)
        db.session.flush()

    co = Company(name=f"{PREFIX}CO__", base_currency="EGP", vat_rate=0,
                 plan_id=plan.id)
    db.session.add(co)
    db.session.flush()
    co.intended_plan_id = plan.id
    db.session.commit()
    ensure_roles_ready_for_company(co.id)

    users, emps = {}, {}
    for tag in ("one", "two"):
        u = User(email=f"{PREFIX}{tag}@audit.local", full_name=tag,
                 is_active=True, terms_version=get_terms_version(),
                 terms_accepted_at=datetime.utcnow())
        u.set_password("Passw0rd!audit1")
        db.session.add(u)
        db.session.flush()
        set_membership_role(u.id, co.id, "team_member")
        e = Employee(company_id=co.id, name=f"موظف {tag}", basic_salary=5000,
                     status="ACTIVE", start_date=date(2025, 1, 1),
                     user_id=u.id)
        db.session.add(e)
        db.session.flush()
        users[tag], emps[tag] = u.id, e.id
    db.session.commit()
    _STATE.update(cid=co.id, users=users, emps=emps)


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        # journal_lines and payroll_lines have NO company_id, so the
        # generic sweep below never touched them. Orphan lines survived
        # every run and were then counted against whatever account ids
        # got recycled next — which showed up as audit_payroll_ledger
        # reporting "should have 2 movements, got 4".
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
    db.session.execute(text("DELETE FROM plans WHERE code='__attchk__'"))
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text("DELETE FROM user_companies WHERE user_id=:u"),
                           {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _emp(tag="one"):
    from app.models import Employee
    return db.session.get(Employee, _STATE["emps"][tag])


def _reset():
    """Clear check-ins, exceptions and policies between checks."""
    from app.models import AttendanceCheckin, AttendanceException, AttendancePolicy
    AttendanceCheckin.query.delete()
    AttendanceException.query.delete()
    AttendancePolicy.query.delete()
    db.session.commit()


def _policy(start="09:00", work_days="0,1,2,3,4,5,6"):
    """A FIXED policy. work_days defaults to EVERY day so the checks do
    not silently pass or fail depending on which weekday they run on."""
    from app.services.attendance import create_policy
    hh, mm = start.split(":")
    return create_policy(
        company_id=_STATE["cid"], scope="COMPANY", policy_type="FIXED",
        start_time=time(int(hh), int(mm)), end_time=time(17, 0),
        work_days=work_days)


def _at(hh, mm, day=None):
    d = day or date.today()
    return datetime.combine(d, time(hh, mm))


def _client(tag="one"):
    c = _STATE["app"].test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(_STATE["users"][tag])
        s["_fresh"] = True
        s["active_company_id"] = _STATE["cid"]
    return c


# ─── Ticket 2 ───────────────────────────────────────────────────────────
@check("1. a check-in records the time and the coordinates")
def _():
    from app.services.attendance import check_in
    _reset()
    row, exc = check_in(_emp(), lat=30.0444, lng=31.2357, now=_at(9, 0))
    assert row.check_in_time == _at(9, 0)
    assert abs(row.check_in_lat - 30.0444) < 1e-6
    assert abs(row.check_in_lng - 31.2357) < 1e-6
    assert row.company_id == _STATE["cid"]
    assert row.date == date.today()
    return f"in at {row.check_in_time.strftime('%H:%M')} @ 30.0444,31.2357"


@check("2. a refused location still records the check-in")
def _():
    """The ticket is explicit: location is evidence when offered, never a
    gate. A browser that denies permission must not block attendance."""
    from app.services.attendance import check_in
    _reset()
    row, _ = check_in(_emp(), lat=None, lng=None, now=_at(9, 0))
    assert row.check_in_time is not None, "no coordinates blocked the check-in"
    assert row.check_in_lat is None and row.check_in_lng is None
    return "recorded with no coordinates"


@check("3. one check-in per employee per day; the second is refused")
def _():
    from app.models import AttendanceCheckin
    from app.services.attendance import check_in, AttendanceError
    _reset()
    check_in(_emp(), now=_at(9, 0))
    try:
        check_in(_emp(), now=_at(11, 0))
        raise AssertionError("a second check-in was accepted for the same day")
    except AttendanceError as e:
        msg = str(e)
    assert "09:00" in msg, f"the message does not say when: {msg}"
    assert AttendanceCheckin.query.filter_by(
        employee_id=_emp().id, date=date.today()).count() == 1
    return f"second refused: {msg[:44]}"


@check("4. check-out updates the same row, and needs a check-in first")
def _():
    from app.models import AttendanceCheckin
    from app.services.attendance import check_in, check_out, AttendanceError
    _reset()
    try:
        check_out(_emp(), now=_at(17, 0))
        raise AssertionError("check-out was accepted with no check-in")
    except AttendanceError:
        pass
    check_in(_emp(), now=_at(9, 0))
    row = check_out(_emp(), lat=30.1, lng=31.2, now=_at(17, 30))
    assert AttendanceCheckin.query.filter_by(
        employee_id=_emp().id, date=date.today()).count() == 1, (
        "check-out created a second row instead of updating the first")
    assert row.check_out_time == _at(17, 30)
    assert row.worked_hours == 8.5, f"worked_hours={row.worked_hours}"
    return f"one row, 09:00 -> 17:30 = {row.worked_hours}h"


@check("5. check-out cannot be recorded twice, or before the check-in")
def _():
    from app.services.attendance import check_in, check_out, AttendanceError
    _reset()
    check_in(_emp(), now=_at(9, 0))
    check_out(_emp(), now=_at(17, 0))
    try:
        check_out(_emp(), now=_at(18, 0))
        raise AssertionError("a second check-out was accepted")
    except AttendanceError:
        pass
    _reset()
    check_in(_emp(), now=_at(9, 0))
    try:
        check_out(_emp(), now=_at(8, 0))
        raise AssertionError("a check-out BEFORE the check-in was accepted")
    except AttendanceError:
        pass
    return "double check-out and backwards check-out both refused"


@check("6. the math challenge gates both endpoints")
def _():
    """Ticket 3's whole purpose. Verified through the real HTTP route,
    because that is where a script would attack."""
    from app.models import AttendanceCheckin
    _reset()
    c = _client()
    # a wrong answer writes nothing
    c.get("/my/account")                       # mints a challenge
    c.post("/my/attendance/checkin", data={"math_answer": "99999"})
    assert AttendanceCheckin.query.count() == 0, (
        "a check-in was recorded with a wrong challenge answer")
    # no answer at all writes nothing
    c.get("/my/account")
    c.post("/my/attendance/checkin", data={})
    assert AttendanceCheckin.query.count() == 0, (
        "a check-in was recorded with no challenge answer")

    # and the correct answer works
    from app.services import bot_guard
    with c.session_transaction() as s:
        s[bot_guard._MATH_SESSION_KEY] = 7
    c.post("/my/attendance/checkin", data={"math_answer": "7"})
    assert AttendanceCheckin.query.count() == 1, (
        "a correct answer did not let the check-in through")
    return "wrong and missing answers refused; correct accepted"


# ─── Ticket 4 ───────────────────────────────────────────────────────────
@check("7. a late arrival creates exactly one LATE exception")
def _():
    from app.models import AttendanceException, AttendanceExceptionType
    from app.services.attendance import check_in
    _reset()
    _policy(start="09:00")
    row, exc = check_in(_emp(), now=_at(9, 30))
    assert exc is not None, "a 30-minute late arrival created no exception"
    assert exc.type == AttendanceExceptionType.LATE
    assert AttendanceException.query.filter_by(
        employee_id=_emp().id, date=date.today()).count() == 1
    assert exc.company_id == _STATE["cid"]
    return f"one LATE exception, {float(exc.duration_hours)}h"


@check("8. THE DECISION: the exception carries the TRUE lateness")
def _():
    """Tickets 4 and 6 disagreed. Zyad chose: the record is the raw fact,
    and every allowance is applied at deduction time (ticket 6). If the
    grace were netted off here it would be subtracted twice, and the
    attendance record would disagree with what actually happened."""
    from app.services.attendance import check_in
    _reset()
    _policy(start="09:00")
    _row, exc = check_in(_emp(), now=_at(9, 30))
    minutes = round(float(exc.duration_hours) * 60)
    assert minutes == 30, (
        f"the exception says {minutes} minutes; the employee was 30 late. "
        "A grace must not be netted off here")
    assert "09:30" in (exc.note or ""), (
        f"the note does not record the real arrival: {exc.note!r}")
    return f"arrived 09:30 vs 09:00 -> exception of {minutes} minutes"


@check("9. an on-time arrival creates nothing")
def _():
    from app.models import AttendanceException
    from app.services.attendance import check_in
    _reset()
    _policy(start="09:00")
    for when in (_at(8, 30), _at(9, 0)):
        _reset()
        _policy(start="09:00")
        _row, exc = check_in(_emp(), now=when)
        assert exc is None, (
            f"arriving at {when.strftime('%H:%M')} was marked late")
    assert AttendanceException.query.count() == 0
    return "early and exactly-on-time both clean"


@check("10. THE GUARANTEE: no policy -> no automatic exception")
def _():
    from app.models import AttendanceException
    from app.services.attendance import check_in
    _reset()                                   # no policy at all
    _row, exc = check_in(_emp(), now=_at(23, 59))
    assert exc is None, (
        "an exception was created for a company with no policy — every "
        "existing tenant would start collecting automatic lateness")
    assert AttendanceException.query.count() == 0
    return "23:59 arrival, no policy, nothing recorded"


@check("11. a non-working day creates nothing")
def _():
    from app.services.attendance import check_in
    _reset()
    today = date.today()
    others = ",".join(str(d) for d in range(7) if d != today.weekday())
    _policy(start="09:00", work_days=others)
    _row, exc = check_in(_emp(), now=_at(23, 0))
    assert exc is None, "someone was marked late on a non-working day"
    return "late arrival on a rest day is not an exception"


@check("12. a day that already has an exception is left alone")
def _():
    """create_exception refuses a duplicate, and evaluate_checkin must
    treat that as 'HR already ruled on this day', not as an error."""
    from app.models import AttendanceException, AttendanceExceptionType
    from app.services.leave import create_exception
    from app.services.attendance import check_in
    _reset()
    _policy(start="09:00")
    manual = create_exception(
        company_id=_STATE["cid"], employee_id=_emp().id, date_=date.today(),
        type_=AttendanceExceptionType.APPROVED_LEAVE, note="إجازة معتمدة")
    _row, exc = check_in(_emp(), now=_at(11, 0))
    assert exc is None, "the automatic path overrode a manual exception"
    rows = AttendanceException.query.filter_by(
        employee_id=_emp().id, date=date.today()).all()
    assert len(rows) == 1 and rows[0].id == manual.id, (
        "the manual exception was replaced or duplicated")
    assert rows[0].type == AttendanceExceptionType.APPROVED_LEAVE
    return "the manual APPROVED_LEAVE survived untouched"


@check("13. the absence sweep marks whoever never arrived")
def _():
    from app.models import AttendanceException, AttendanceExceptionType
    from app.services.attendance import check_in, mark_absent_for_date
    _reset()
    _policy(start="09:00")
    yesterday = date.today() - timedelta(days=1)
    # employee "one" attended, "two" did not
    check_in(_emp("one"), now=_at(9, 0, yesterday))
    res = mark_absent_for_date(_STATE["cid"], yesterday)
    rows = AttendanceException.query.filter_by(date=yesterday).all()
    assert len(rows) == 1, f"{len(rows)} absences created, expected 1"
    assert rows[0].employee_id == _STATE["emps"]["two"], (
        "the wrong employee was marked absent")
    assert rows[0].type == AttendanceExceptionType.ABSENT
    assert res["created"] == 1
    assert res["skipped"].get("attended") == 1, (
        f"the attending employee was not reported as skipped: {res}")
    _STATE["sweep_date"] = yesterday
    return f"1 absent, 1 attended · {res['skipped']}"


@check("14. …and the sweep is idempotent across repeated ticks")
def _():
    from app.models import AttendanceException
    from app.services.attendance import mark_absent_for_date
    yesterday = _STATE["sweep_date"]
    before = AttendanceException.query.filter_by(date=yesterday).count()
    for _ in range(3):
        res = mark_absent_for_date(_STATE["cid"], yesterday)
        assert res["created"] == 0, f"a repeat sweep created {res['created']}"
    after = AttendanceException.query.filter_by(date=yesterday).count()
    assert after == before, f"absences {before} -> {after} after 3 sweeps"
    return f"{before} absences, unchanged after 3 more sweeps"


@check("15. the sweep never judges today, only a finished day")
def _():
    """Running against today would mark absent everyone who simply has
    not arrived yet."""
    from app.models import AttendanceException
    from app.services.attendance import sweep_absences
    _reset()
    _policy(start="09:00")
    summary = sweep_absences(now=date.today(), company_id=_STATE["cid"])
    assert summary["date"] == (date.today() - timedelta(days=1)).isoformat(), (
        f"the sweep targeted {summary['date']}, not yesterday")
    assert AttendanceException.query.filter_by(date=date.today()).count() == 0, (
        "somebody was marked absent for a day that has not finished")
    return f"targeted {summary['date']}, today untouched"


@check("16. the portal renders, and an outsider cannot check in")
def _():
    from app.models import AttendanceCheckin
    _reset()
    c = _client()
    body = c.get("/my/account").get_data(as_text=True)
    assert c.get("/my/account").status_code == 200
    for leak in ("{{", "{%", "{#", "#}"):
        assert leak not in body, f"the portal leaks {leak} into the HTML"
    assert "تسجيل حضور" in body, "the check-in button is missing"
    assert "الحضور والانصراف" in body

    # a logged-in user with no Employee row in this company is refused
    from app.models import User
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    outsider = User(email=f"{PREFIX}outsider@audit.local", full_name="out",
                    is_active=True, terms_version=get_terms_version(),
                    terms_accepted_at=datetime.utcnow())
    outsider.set_password("Passw0rd!audit1")
    db.session.add(outsider)
    db.session.flush()
    db.session.commit()
    set_membership_role(outsider.id, _STATE["cid"], "team_member")

    # NB the Flask-Login trap this repo keeps hitting: inside ONE app
    # context, g._login_user is cached, so every later test-client
    # request answers as the FIRST user who logged in — the outsider's
    # POST came back 302 because it was being served as employee "one",
    # and this check was proving nothing. A NESTED app context gets a
    # fresh `g`, which is what actually isolates the second identity.
    from app.services import bot_guard
    with _STATE["app"].app_context():
        oc = _STATE["app"].test_client()
        with oc.session_transaction() as s:
            s["_user_id"] = str(outsider.id)
            s["_fresh"] = True
            s["active_company_id"] = _STATE["cid"]
            s[bot_guard._MATH_SESSION_KEY] = 5
        r = oc.post("/my/attendance/checkin", data={"math_answer": "5"})
        status = r.status_code
    assert status == 403, (
        f"a user with no Employee row got {status}, expected 403")
    assert AttendanceCheckin.query.count() == 0, (
        "a user with no Employee record recorded attendance")
    return "portal renders clean; non-employee refused with 403"


@check("17. DEFINITION OF DONE: late check-in -> exception -> payslip")
def _():
    """Ticket 4's own end-to-end. Nothing in the payroll wiring was
    touched — HR-07 already turns an AttendanceException into money, so
    this proves the new automatic exception reaches it exactly the way a
    hand-typed one always has.

    ON THE AMOUNT: 30 minutes on a 6000 salary deducts 12.00, not the
    12.50 the arithmetic suggests. attendance_deductions rounds late_days
    to 2 decimals (0.5h/8h = 0.0625 -> 0.06) before multiplying by the
    daily rate. That rounding is PRE-EXISTING HR-07 behaviour, not
    something this batch introduced, and ticket 6 replaces this path with
    compute_late_deduction anyway. Pinned at the real number so a future
    change to it is a deliberate decision rather than a surprise.
    """
    from app.models import PayrollLine
    from app.services.payroll import run_payroll
    from app.services.attendance import check_in
    from app.services.seed_coa import seed_default_coa
    from app.services.ledger import get_account_by_code

    # The fixture deliberately has no chart of accounts — every other
    # check in this suite works without one, and seeding it up front
    # would slow all sixteen down for the sake of this one. Posting a
    # payroll journal does need it.
    if get_account_by_code(_STATE["cid"], "1110") is None:
        seed_default_coa(_STATE["cid"])
        db.session.commit()

    _reset()
    _policy(start="09:00")

    # A past month, so the run is not competing with today's fixtures.
    late_day = datetime(2026, 6, 3, 9, 30)
    _row, exc = check_in(_emp(), now=late_day)
    assert exc is not None, "the late check-in produced no exception"
    assert round(float(exc.duration_hours) * 60) == 30

    run = run_payroll(_STATE["cid"], 2026, 6, line_inputs=None,
                      created_by=None, send_emails=False)
    db.session.commit()
    line = PayrollLine.query.filter_by(
        run_id=run.id, employee_id=_emp().id).first()
    assert line is not None, "no payslip line for the employee"

    assert line.attendance_auto_calculated is True, (
        "the payslip did not mark the deduction as auto-calculated — "
        "HR-07 did not see the automatic exception")
    late = float(line.late_deduction)
    assert late > 0, (
        f"late_deduction is {late}: the automatic exception never reached "
        "the payslip")
    daily = float(_emp().basic_salary) / 30.0
    expected = round(round(0.5 / 8.0, 2) * daily, 2)
    assert abs(late - expected) < 0.005, (
        f"late_deduction {late}, expected {expected}")
    return (f"09:30 vs 09:00 -> 30 min -> payslip deduction {late:.2f} "
            f"(auto={line.attendance_auto_calculated})")


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
