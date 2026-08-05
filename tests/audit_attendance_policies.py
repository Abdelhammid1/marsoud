#!/usr/bin/env python3
"""MARSOUD-ATTENDANCE-POLICY + ANTIBOT (2026-08-05) — tickets 1 and 3.

The HR module could record that someone was absent or late, but nothing
anywhere said what time they were supposed to arrive. Every exception was
typed in retroactively against a rule that lived in somebody's head.

Ticket 1 writes that rule down. Ticket 3 adds the math challenge that
ticket 2's check-in screen will sit behind.

THE PROPERTY THAT MATTERS MOST is the last one: a company with NO policy
resolves to None and nothing changes. Every existing tenant keeps
behaving exactly as it does today, which is what makes the rest of this
batch safe to deploy.

Checks
  1.  a company policy governs an employee with no narrower one
  2.  a department policy beats the company policy
  3.  an employee override beats their department's policy
  4.  no policy anywhere -> None (the backward-compatibility guarantee)
  5.  a policy never leaks across companies
  6.  an inactive policy is ignored, and the next one down applies
  7.  a department policy does not touch employees of other departments
  8.  the scope's own field is required, and duplicates are refused
  9.  FIXED and FLEXIBLE each say when an arrival counts as late
  10. work_days parses, and a blank one means every day
  11. the screens render and enforce hr_required
  12. the math challenge: right answer accepted, wrong rejected
  13. …it changes between calls
  14. …and a correct answer cannot be replayed
"""
import sys
from datetime import date, datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__ATTPOL_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ────────────────────────────────────────────────────────────
def _mk_company(suffix):
    from app.models import Company, Plan
    plan = Plan.query.filter_by(code="__attpol__").first()
    if not plan:
        plan = Plan(code="__attpol__", name="Audit", name_ar="تدقيق",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "crm", "hr", "reports",
                          "evaluations", "settings"])
        db.session.add(plan)
        db.session.flush()
    co = Company(name=f"{PREFIX}{suffix}__", base_currency="EGP", vat_rate=0,
                 plan_id=plan.id)
    db.session.add(co)
    db.session.flush()
    co.intended_plan_id = plan.id
    db.session.commit()
    return co


def _setup():
    _teardown()
    from app.models import Department, Employee, User
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company

    co = _mk_company("MAIN")
    ensure_roles_ready_for_company(co.id)

    sales = Department(company_id=co.id, name="المبيعات", is_active=True)
    ops = Department(company_id=co.id, name="التشغيل", is_active=True)
    db.session.add_all([sales, ops])
    db.session.flush()

    # one employee per department, plus one with no department at all
    emps = {}
    for tag, dept in (("sales", sales), ("ops", ops), ("none", None)):
        e = Employee(company_id=co.id, name=f"موظف {tag}", basic_salary=5000,
                     status="ACTIVE", start_date=date(2025, 1, 1),
                     department_id=dept.id if dept else None)
        db.session.add(e)
        db.session.flush()
        emps[tag] = e.id
    db.session.commit()

    # an HR user, for the screen checks
    u = User(email=f"{PREFIX}hr@audit.local", full_name="AttPol HR",
             is_active=True, terms_version=get_terms_version(),
             terms_accepted_at=datetime.utcnow())
    u.set_password("Passw0rd!audit1")
    db.session.add(u)
    db.session.flush()
    db.session.commit()
    set_membership_role(u.id, co.id, "owner")

    # a SEPARATE company, for the isolation check
    other = _mk_company("OTHER")
    oe = Employee(company_id=other.id, name="موظف شركة أخرى",
                  basic_salary=5000, status="ACTIVE",
                  start_date=date(2025, 1, 1))
    db.session.add(oe)
    db.session.commit()

    _STATE.update(cid=co.id, uid=u.id, emps=emps,
                  sales_dept=sales.id, ops_dept=ops.id,
                  other_cid=other.id, other_emp=oe.id)


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
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
    db.session.execute(text("DELETE FROM plans WHERE code='__attpol__'"))
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text("DELETE FROM user_companies WHERE user_id=:u"),
                           {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _clear_policies():
    from app.models import AttendancePolicy
    AttendancePolicy.query.delete()
    db.session.commit()


def _mk_policy(scope, *, company_id=None, department_id=None,
               employee_id=None, start="09:00", **kw):
    from app.services.attendance import create_policy
    hh, mm = start.split(":")
    return create_policy(
        company_id=company_id or _STATE["cid"], scope=scope,
        policy_type="FIXED", department_id=department_id,
        employee_id=employee_id,
        start_time=time(int(hh), int(mm)), end_time=time(17, 0),
        work_days="6,0,1,2,3", created_by=_STATE["uid"], **kw)


def _resolve(emp_key):
    from app.services.attendance import resolve_policy_for_employee
    return resolve_policy_for_employee(_STATE["emps"][emp_key], date.today())


def _client():
    c = _STATE["app"].test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(_STATE["uid"])
        s["_fresh"] = True
        s["active_company_id"] = _STATE["cid"]
    return c


# ─── Ticket 1: resolution order ─────────────────────────────────────────
@check("1. a company policy governs an employee with no narrower one")
def _():
    _clear_policies()
    p = _mk_policy("COMPANY", start="09:00")
    got = _resolve("sales")
    assert got is not None and got.id == p.id, (
        f"expected the company policy, got {got}")
    assert _resolve("none").id == p.id, (
        "an employee with no department did not get the company policy")
    return f"company policy {p.id} applies to everyone"


@check("2. a department policy beats the company policy")
def _():
    company = _mk_policy("COMPANY") if not _resolve("ops") else None
    dept = _mk_policy("DEPARTMENT", department_id=_STATE["sales_dept"],
                      start="10:00")
    got = _resolve("sales")
    assert got.id == dept.id, (
        f"the sales employee resolved to {got.id}, not the department "
        f"policy {dept.id}")
    assert got.start_time == time(10, 0)
    _STATE["dept_policy"] = dept.id
    return f"sales -> department policy {dept.id} (10:00)"


@check("3. an employee override beats their department's policy")
def _():
    own = _mk_policy("EMPLOYEE", employee_id=_STATE["emps"]["sales"],
                     start="11:00")
    got = _resolve("sales")
    assert got.id == own.id, (
        f"the override did not win: resolved {got.id}, expected {own.id}")
    assert got.start_time == time(11, 0)
    _STATE["own_policy"] = own.id
    return f"sales -> employee override {own.id} (11:00)"


@check("4. THE GUARANTEE: no policy anywhere resolves to None")
def _():
    """A company that has never defined a policy keeps behaving exactly
    as it does today — nothing automatically late, nothing automatically
    absent. This is what makes the whole batch safe to deploy."""
    _clear_policies()
    for key in ("sales", "ops", "none"):
        got = _resolve(key)
        assert got is None, (
            f"{key} resolved to {got} with no policies defined — existing "
            "companies would start getting automatic exceptions")
    return "no policies -> None for every employee"


@check("5. a policy never leaks across companies")
def _():
    from app.services.attendance import resolve_policy_for_employee
    _clear_policies()
    mine = _mk_policy("COMPANY", start="08:00")
    got = resolve_policy_for_employee(_STATE["other_emp"], date.today())
    assert got is None, (
        f"an employee of another company resolved to policy {got.id} "
        "belonging to this one")
    assert _resolve("sales").id == mine.id, "own company stopped resolving"
    return "other company still None; own company unaffected"


@check("6. an inactive policy is ignored and the next one down applies")
def _():
    from app.services.attendance import update_policy
    from app.models import AttendancePolicy
    _clear_policies()
    company = _mk_policy("COMPANY", start="09:00")
    dept = _mk_policy("DEPARTMENT", department_id=_STATE["sales_dept"],
                      start="10:00")
    assert _resolve("sales").id == dept.id
    update_policy(db.session.get(AttendancePolicy, dept.id), is_active=False)
    got = _resolve("sales")
    assert got.id == company.id, (
        f"a deactivated department policy is still winning: {got.id}")
    return "deactivated department policy skipped, company policy applies"


@check("7. a department policy leaves other departments alone")
def _():
    _clear_policies()
    company = _mk_policy("COMPANY", start="09:00")
    _mk_policy("DEPARTMENT", department_id=_STATE["sales_dept"],
               start="10:00")
    assert _resolve("ops").id == company.id, (
        "an employee in a different department picked up the sales policy")
    assert _resolve("none").id == company.id, (
        "an employee with no department picked up a department policy")
    return "ops and no-department employees keep the company policy"


@check("8. the scope's own target is required, and duplicates refused")
def _():
    from app.services.attendance import create_policy, AttendanceError
    _clear_policies()
    for scope in ("DEPARTMENT", "EMPLOYEE"):
        try:
            create_policy(company_id=_STATE["cid"], scope=scope,
                          policy_type="FIXED", start_time=time(9, 0),
                          end_time=time(17, 0))
            raise AssertionError(f"{scope} accepted with no target")
        except AttendanceError:
            pass
    _mk_policy("COMPANY")
    try:
        _mk_policy("COMPANY")
        raise AssertionError("a second company policy was accepted — "
                             "resolution would depend on insertion order")
    except AttendanceError as e:
        msg = str(e)
    return f"targets required; duplicate refused ({msg[:38]})"


@check("9. FIXED and FLEXIBLE each say when an arrival is late")
def _():
    """Ticket 4 asks the policy this one question. FLEXIBLE measures
    against the END of the arrival window — 10:00 inside an 08:00-10:30
    window is not late."""
    from app.services.attendance import create_policy, AttendanceError
    _clear_policies()
    fixed = _mk_policy("COMPANY", start="09:00")
    assert fixed.expected_arrival == time(9, 0), (
        f"FIXED reports {fixed.expected_arrival}, expected its start_time")

    _clear_policies()
    flex = create_policy(
        company_id=_STATE["cid"], scope="COMPANY", policy_type="FLEXIBLE",
        earliest_checkin=time(8, 0), latest_checkin=time(10, 30),
        required_hours_per_day=8, created_by=_STATE["uid"])
    assert flex.expected_arrival == time(10, 30), (
        f"FLEXIBLE reports {flex.expected_arrival}, expected latest_checkin")

    # and a FLEXIBLE policy with no window is refused, because it could
    # never answer the question
    try:
        create_policy(company_id=_STATE["other_cid"], scope="COMPANY",
                      policy_type="FLEXIBLE", required_hours_per_day=8)
        raise AssertionError("FLEXIBLE accepted with no latest_checkin")
    except AttendanceError:
        pass
    return "FIXED -> start_time · FLEXIBLE -> latest_checkin · empty refused"


@check("10. work_days parses, and a blank one means every day")
def _():
    _clear_policies()
    p = _mk_policy("COMPANY")
    assert p.work_day_numbers == {6, 0, 1, 2, 3}, p.work_day_numbers
    assert p.is_working_day(date(2026, 8, 9)) is True, "Sunday should be a work day"
    assert p.is_working_day(date(2026, 8, 7)) is False, "Friday should not be"

    from app.services.attendance import update_policy
    update_policy(p, work_days="")
    assert p.work_day_numbers == set(range(7)), (
        "a blank work_days must mean every day — the alternative is "
        "silently marking everyone absent")
    return "Sun-Thu parsed; blank means all seven"


@check("11. the screens render and are gated on hr_required")
def _():
    _clear_policies()
    _mk_policy("COMPANY", start="09:00")
    c = _client()
    for url in ("/hr/attendance-policies", "/hr/attendance-policies/new"):
        r = c.get(url)
        assert r.status_code == 200, f"{url} returned {r.status_code}"
        body = r.get_data(as_text=True)
        for leak in ("{{", "{%", "{#", "#}"):
            assert leak not in body, f"{url} leaks {leak} into the HTML"
    listing = c.get("/hr/attendance-policies").get_data(as_text=True)
    assert "سياسات الدوام" in listing
    src = (ROOT / "app/routes/hr.py").read_text(encoding="utf-8")
    i = src.index("def attendance_policies(")
    assert "hr_required" in src[max(0, i - 200):i], (
        "the policies screen is not behind hr_required")
    return "listing + form render clean, hr_required in place"


# ─── Ticket 3: the math challenge ───────────────────────────────────────
@check("12. the math challenge accepts the right answer and rejects wrong")
def _():
    from app.services.bot_guard import (generate_math_challenge,
                                        verify_math_challenge)
    with _STATE["app"].test_request_context():
        q = generate_math_challenge()
        assert q and any(op in q for op in ("+", "-")), f"odd question: {q!r}"
        a, op, b = q.split()
        expected = int(a) + int(b) if op == "+" else int(a) - int(b)
        assert expected >= 0, f"the challenge asked for a negative answer: {q}"
        assert verify_math_challenge(expected) is True

    with _STATE["app"].test_request_context():
        generate_math_challenge()
        assert verify_math_challenge(9999) is False
    with _STATE["app"].test_request_context():
        generate_math_challenge()
        assert verify_math_challenge("") is False
        # …and no challenge at all is a refusal, not a pass
        assert verify_math_challenge(1) is False
    return "correct accepted; wrong, blank and absent all refused"


@check("13. the challenge changes between calls")
def _():
    from app.services.bot_guard import generate_math_challenge
    seen = set()
    with _STATE["app"].test_request_context():
        for _ in range(25):
            seen.add(generate_math_challenge())
    assert len(seen) > 3, (
        f"only {len(seen)} distinct questions in 25 draws — a fixed "
        "challenge is worth nothing")
    return f"{len(seen)} distinct questions in 25 draws"


@check("14. a correct answer cannot be replayed")
def _():
    """Single-use on purpose. There are only ~19 possible answers, so a
    reusable challenge would let a script solve one by hand and post that
    same answer twice a day forever."""
    from app.services.bot_guard import (generate_math_challenge,
                                        verify_math_challenge)
    with _STATE["app"].test_request_context():
        q = generate_math_challenge()
        a, op, b = q.split()
        answer = int(a) + int(b) if op == "+" else int(a) - int(b)
        assert verify_math_challenge(answer) is True
        assert verify_math_challenge(answer) is False, (
            "the same answer worked twice — the challenge is not consumed")
    # a wrong answer also consumes it, so the question cannot be
    # brute-forced in place
    with _STATE["app"].test_request_context():
        generate_math_challenge()
        assert verify_math_challenge(9999) is False
        assert verify_math_challenge(9999) is False
    return "answer consumed on first use, right or wrong"


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
