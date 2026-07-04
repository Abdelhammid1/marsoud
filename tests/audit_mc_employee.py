#!/usr/bin/env python3
"""MARSOUD-MC-EMPLOYEE (Abdelhamid 2026-07-04) — audit.

Reproduces Abdelhamid's exact scenario: one User owns three Companies,
each with its own Employee row. Proves:

  1. Employee.user_id is set correctly per-company after /companies/new.
  2. Resolving the "my Employee" for the active company returns the
     right row in each of the three companies (not just the last one).
  3. Payslip access respects the active-company scope (a payslip in
     company A is 404 when active_company=B, even for the same User).
  4. /hr_ss buckets classify the user as "ACTIVE with employee" in
     every company they own, not "unlinked" in 2 of 3.
  5. UNIQUE (company_id, user_id) prevents duplicate linkage — trying
     to create a second Employee for the same user in the same company
     raises IntegrityError.
"""
import sys
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
    from app.models import Company, User, UserStatus, Employee, EmployeeStatus
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

    u_old = User.query.filter_by(email="mce_owner@t.co").first()
    if u_old:
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u_old.id))
        db.session.delete(u_old); db.session.commit()

    for name in ("__MCE_A__", "__MCE_B__", "__MCE_C__"):
        old = Company.query.filter_by(name=name).first()
        if old:
            _teardown_company(old.id)

    a = Company(name="__MCE_A__", base_currency="SAR")
    b = Company(name="__MCE_B__", base_currency="EGP")
    c = Company(name="__MCE_C__", base_currency="EGP")
    db.session.add_all([a, b, c]); db.session.flush()
    for co in (a, b, c):
        seed_default_coa(co.id)

    u = User(email="mce_owner@t.co", full_name="MC-EMP Owner",
              status=UserStatus.ACTIVE.value)
    u.set_password("x")
    db.session.add(u); db.session.flush()

    # Three memberships as owner.
    for co in (a, b, c):
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=co.id, role="owner",
        ))

    # Simulate what /companies/new + /register do — one Employee per
    # company with the new user_id linkage.
    emps = {}
    for co in (a, b, c):
        e = Employee(
            company_id=co.id, name=u.full_name, email=u.email,
            user_id=u.id, status=EmployeeStatus.ACTIVE,
        )
        db.session.add(e); db.session.flush()
        emps[co.id] = e.id
    db.session.commit()

    _STATE.update(
        user_id=u.id,
        a_id=a.id, b_id=b.id, c_id=c.id,
        emp_a=emps[a.id], emp_b=emps[b.id], emp_c=emps[c.id],
    )


def _teardown_company(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id = :c"
                ), {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                       {"c": company_id})


@check("1. Every Employee row has user_id set correctly")
def _():
    from app.models import Employee
    for cid_key, emp_key in (("a_id", "emp_a"),
                              ("b_id", "emp_b"),
                              ("c_id", "emp_c")):
        e = db.session.get(Employee, _STATE[emp_key])
        assert e.company_id == _STATE[cid_key], (
            f"{emp_key}.company_id mismatch: {e.company_id} != {_STATE[cid_key]}"
        )
        assert e.user_id == _STATE["user_id"], (
            f"{emp_key}.user_id = {e.user_id}, want {_STATE['user_id']}"
        )


@check("2. Per-company lookup returns the correct Employee in each company")
def _():
    from app.models import Employee
    for cid_key, emp_key in (("a_id", "emp_a"),
                              ("b_id", "emp_b"),
                              ("c_id", "emp_c")):
        e = Employee.query.filter_by(
            company_id=_STATE[cid_key], user_id=_STATE["user_id"],
        ).first()
        assert e is not None, f"no Employee found for {cid_key}"
        assert e.id == _STATE[emp_key], (
            f"per-company lookup for {cid_key} returned emp {e.id}, "
            f"want {_STATE[emp_key]}"
        )


@check("3. Payslip access is scoped to active company")
def _():
    # Simulates the guard in payslip_pdf: uses _my_employee() which is
    # scoped to (company_id, current_user.id).
    from app.models import Employee
    from app.routes.hr_self_service import _my_employee
    from flask import g

    app = _STATE["app"]
    from app.models import User, Company
    u = db.session.get(User, _STATE["user_id"])

    def sim(active_company_id):
        with app.test_request_context():
            from flask_login import login_user
            login_user(u)
            g.active_company = db.session.get(Company, active_company_id)
            emp = _my_employee()
            return emp.id if emp else None

    # In each of the 3 companies, _my_employee should return the
    # per-company Employee, not the wrong one.
    assert sim(_STATE["a_id"]) == _STATE["emp_a"], "wrong emp for A"
    assert sim(_STATE["b_id"]) == _STATE["emp_b"], "wrong emp for B"
    assert sim(_STATE["c_id"]) == _STATE["emp_c"], "wrong emp for C"


@check("4. HR-SS bucketing: user shows as linked in every company")
def _():
    # Reproduces the bucket logic in hr_self_service.py index() —
    # linked_users_here scopes by company.
    from app.models import Employee
    for cid_key in ("a_id", "b_id", "c_id"):
        cid = _STATE[cid_key]
        linked = {
            e.user_id for e in Employee.query.filter(
                Employee.company_id == cid,
                Employee.user_id.isnot(None),
            ).all()
        }
        assert _STATE["user_id"] in linked, (
            f"user missing from linked bucket in company {cid_key}"
        )


@check("5. UNIQUE(company_id, user_id) forbids duplicate linkage")
def _():
    from app.models import Employee, EmployeeStatus
    from sqlalchemy.exc import IntegrityError
    dup = Employee(
        company_id=_STATE["a_id"], name="dup",
        email="mce_owner@t.co", user_id=_STATE["user_id"],
        status=EmployeeStatus.ACTIVE,
    )
    db.session.add(dup)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return  # correct — constraint fired
    # If we got here the constraint didn't fire; fail.
    db.session.delete(dup); db.session.commit()
    raise AssertionError(
        "UNIQUE(company_id, user_id) did NOT fire — duplicate Employee "
        "for the same user in the same company was inserted."
    )


def main():
    app = create_app()
    with app.app_context():
        _STATE["app"] = app
        _setup()
        n_pass = 0
        for label, fn in CHECKS:
            try:
                fn()
                print(f"  ✓ {label}")
                n_pass += 1
            except AssertionError as e:
                print(f"  ✗ {label}\n      {e}")
        # Clean up.
        for cid in (_STATE["a_id"], _STATE["b_id"], _STATE["c_id"]):
            _teardown_company(cid)
        from app.models import User
        from app.models.user import user_companies
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == _STATE["user_id"]))
        db.session.delete(db.session.get(User, _STATE["user_id"]))
        db.session.commit()

        print(f"\n{n_pass}/{len(CHECKS)} passed.")
        sys.exit(0 if n_pass == len(CHECKS) else 1)


if __name__ == "__main__":
    main()
