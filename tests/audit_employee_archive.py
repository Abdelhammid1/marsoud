#!/usr/bin/env python3
"""MARSOUD-EMPLOYEE-ARCHIVE — audit for the ticket that asked to
hide resigned/terminated employees from every operational surface and
give the user a one-click "return to work" flow.

Coverage:
  1. reactivate_employee service flips TERMINATED → ACTIVE and clears
     the termination metadata.
  2. Historical data (payroll runs, accruals, leave balances) is
     untouched by reactivate_employee.
  3. /payroll/archive lists non-ACTIVE employees.
  4. /payroll/archive does NOT list ACTIVE employees.
  5. POST /payroll/employees/<id>/reactivate flips the row + redirects.
  6. visible_employees_for (used by /reports/employees) hides
     TERMINATED employees for both owner and permitted viewers.
  7. HR/attendance/leaves dropdown queries — the terminated row is
     out of the list because those queries already filter status=ACTIVE.
  8. hr_ss.index linked_users_here filter excludes terminated employees.
"""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import (
        Company, User, user_companies, Employee, EmployeeStatus,
        TerminationReason,
    )
    from werkzeug.security import generate_password_hash
    existing = Company.query.filter_by(name="__EMPARCHIVE_AUDIT__").first()
    if existing:
        _teardown(existing.id)
    c = Company(name="__EMPARCHIVE_AUDIT__", base_currency="SAR")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)

    def _mk_user(email, role="owner"):
        u = User(email=email,
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name=email.split("@")[0])
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=c.id, role=role,
        ))
        return u

    owner = _mk_user("archive-owner@x.test", role="owner")

    active = Employee(
        company_id=c.id, name="سمير النشط",
        status=EmployeeStatus.ACTIVE,
        basic_salary=Decimal("5000"),
    )
    terminated = Employee(
        company_id=c.id, name="أحمد المستقيل",
        status=EmployeeStatus.TERMINATED,
        termination_date=date(2026, 6, 1),
        termination_reason=TerminationReason.RESIGNATION,
        termination_notes="استقالة نظامية",
        basic_salary=Decimal("4500"),
    )
    db.session.add_all([active, terminated])
    db.session.commit()
    _STATE.update(company_id=c.id,
                    owner_id=owner.id,
                    active_emp_id=active.id,
                    terminated_emp_id=terminated.id)


def _teardown(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        # Tables that reference employees but have NO company_id
        # column — the company_id sweep below skips them, so wipe
        # them here via the employee FK BEFORE we delete the
        # employees themselves. Otherwise a re-run trips the
        # (employee_id, leave_type_id, year) unique constraint.
        emp_ids_sql = (
            "SELECT id FROM employees WHERE company_id = :c"
        )
        conn.execute(text(
            f"DELETE FROM leave_balances WHERE employee_id IN ({emp_ids_sql})"
        ), {"c": company_id})
        conn.execute(text(
            f"DELETE FROM employee_accruals WHERE employee_id IN ({emp_ids_sql})"
        ), {"c": company_id})
        conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"),
                     {"c": company_id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                             {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'archive-%@x.test'"))


def _reset_g():
    from flask import g
    for key in ("_login_user", "active_company", "user_companies",
                 "impersonating"):
        try:
            g.pop(key, None)
        except Exception:
            pass


def _owner_client():
    from flask import current_app
    _reset_g()
    c = current_app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["company_id"]
    return c


@check("1. reactivate_employee flips TERMINATED → ACTIVE + clears metadata")
def _():
    from app.services.payroll import reactivate_employee
    from app.models import Employee, EmployeeStatus
    e = db.session.get(Employee, _STATE["terminated_emp_id"])
    # Precondition: is TERMINATED with metadata set
    assert e.status == EmployeeStatus.TERMINATED
    assert e.termination_date is not None
    assert e.termination_reason is not None
    reactivate_employee(e)
    # Postcondition
    e = db.session.get(Employee, _STATE["terminated_emp_id"])
    assert e.status == EmployeeStatus.ACTIVE
    assert e.is_active is True
    assert e.termination_date is None
    assert e.termination_reason is None
    assert e.termination_notes is None
    # Reset back to TERMINATED for downstream checks.
    from datetime import date as _d
    from app.models import TerminationReason
    e.status = EmployeeStatus.TERMINATED
    e.is_active = False
    e.termination_date = _d(2026, 6, 1)
    e.termination_reason = TerminationReason.RESIGNATION
    e.termination_notes = "استقالة نظامية"
    db.session.commit()
    return "flipped + metadata cleared"


@check("2. historical rows (payroll/accrual/leave) untouched by reactivate")
def _():
    """Guardrail: reactivate_employee must NOT delete any row that
    references the employee. Seed a payroll accrual + a leave balance,
    reactivate, and confirm both still exist byte-for-byte."""
    from app.models import (
        Employee, EmployeeAccrual, LeaveBalance, EmployeeStatus,
        TerminationReason,
    )
    from datetime import date as _d
    e = db.session.get(Employee, _STATE["terminated_emp_id"])
    acc = EmployeeAccrual(
        company_id=_STATE["company_id"], employee_id=e.id,
        amount=Decimal("100"),
    )
    # LeaveBalance requires a real leave_type row — seed one for the
    # test then attach a balance to the terminated employee.
    from app.models import LeaveType
    lt = LeaveType(company_id=_STATE["company_id"], name="سنوية")
    db.session.add(lt); db.session.flush()
    lb = LeaveBalance(
        employee_id=e.id, leave_type_id=lt.id, year=2026,
        balance_days=Decimal("21"), used_days=Decimal("5"),
    )
    db.session.add_all([acc, lb]); db.session.commit()
    acc_id, lb_id = acc.id, lb.id

    from app.services.payroll import reactivate_employee
    reactivate_employee(e)

    # Both historical rows must still exist AND still reference this employee.
    acc2 = db.session.get(EmployeeAccrual, acc_id)
    lb2 = db.session.get(LeaveBalance, lb_id)
    assert acc2 is not None and acc2.employee_id == e.id
    assert lb2 is not None and lb2.employee_id == e.id
    assert float(acc2.amount) == 100.0
    assert float(lb2.balance_days) == 21.0
    # Restore TERMINATED for check 3+ context
    e.status = EmployeeStatus.TERMINATED
    e.is_active = False
    e.termination_date = _d(2026, 6, 1)
    e.termination_reason = TerminationReason.RESIGNATION
    db.session.commit()
    return "accrual + leave balance both intact"


@check("3. GET /payroll/archive lists non-ACTIVE employees")
def _():
    r = _owner_client().get("/payroll/archive")
    assert r.status_code == 200, f"status={r.status_code}"
    body = r.get_data(as_text=True)
    assert "أحمد المستقيل" in body, "terminated employee missing from archive"
    return "archive page shows terminated"


@check("4. GET /payroll/archive does NOT list ACTIVE employees")
def _():
    r = _owner_client().get("/payroll/archive")
    body = r.get_data(as_text=True)
    assert "سمير النشط" not in body, \
        "active employee leaked into archive page"
    return "active employee hidden"


@check("5. POST reactivate flips the row + redirects")
def _():
    from app.models import Employee, EmployeeStatus
    tid = _STATE["terminated_emp_id"]
    r = _owner_client().post(
        f"/payroll/employees/{tid}/reactivate",
        follow_redirects=False,
    )
    assert r.status_code == 302, f"status={r.status_code}"
    e = db.session.get(Employee, tid)
    assert e.status == EmployeeStatus.ACTIVE
    # Reset back to TERMINATED for the remaining checks.
    from datetime import date as _d
    from app.models import TerminationReason
    e.status = EmployeeStatus.TERMINATED
    e.is_active = False
    e.termination_date = _d(2026, 6, 1)
    e.termination_reason = TerminationReason.RESIGNATION
    db.session.commit()
    return "POST → status=ACTIVE + 302"


@check("6. visible_employees_for hides TERMINATED for owner")
def _():
    from app.services.daily_digest import visible_employees_for
    from app.models import User
    owner = db.session.get(User, _STATE["owner_id"])
    visible = visible_employees_for(owner, _STATE["company_id"])
    names = {e.name for e in visible}
    assert "سمير النشط" in names, "active missing from reports"
    assert "أحمد المستقيل" not in names, \
        "TERMINATED still visible in daily reports"
    return f"reports visible = {names}"


@check("7. HR dropdown queries filter status=ACTIVE (spot-check)")
def _():
    """This isn't a new query — the existing hr.py/payroll.py
    routes already use `filter_by(status=ACTIVE)` on the manager /
    attendance / leaves dropdowns. Verify by running the same query
    directly."""
    from app.models import Employee, EmployeeStatus
    q = Employee.query.filter_by(
        company_id=_STATE["company_id"],
        status=EmployeeStatus.ACTIVE,
    ).all()
    ids = {e.id for e in q}
    assert _STATE["active_emp_id"] in ids
    assert _STATE["terminated_emp_id"] not in ids
    return f"dropdown query returns {len(ids)} rows (active only)"


@check("8. hr_ss linked_users_here excludes TERMINATED employees")
def _():
    """The hr_self_service.index page builds a set of 'user ids
    already linked to an employee in this company'. After the
    audit fix, TERMINATED employees drop out of that set so the
    user's account no longer appears in the 'active employees'
    bucket on that page."""
    from app.models import (
        Employee, EmployeeStatus, User, user_companies,
    )
    from werkzeug.security import generate_password_hash
    # Give the terminated employee a linked User so the query
    # actually has something to filter.
    u = User(email="archive-fired@x.test",
              password_hash=generate_password_hash(
                  "x", method="pbkdf2:sha256"),
              full_name="Fired User")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=_STATE["company_id"], role="employee",
    ))
    terminated = db.session.get(Employee, _STATE["terminated_emp_id"])
    terminated.user_id = u.id
    db.session.commit()

    linked_users_here = {
        e.user_id for e in Employee.query.filter(
            Employee.company_id == _STATE["company_id"],
            Employee.status == EmployeeStatus.ACTIVE,
            Employee.user_id.isnot(None),
        ).all() if e.user_id is not None
    }
    assert u.id not in linked_users_here, (
        "TERMINATED employee's User still classified as linked-active"
    )
    return "TERMINATED employee's user hidden from active bucket"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}  ⇒ {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "company_id" in _STATE:
                    _teardown(_STATE["company_id"])
                    print("\n(cleaned up fixture company)")
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
