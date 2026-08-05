#!/usr/bin/env python3
"""MARSOUD-ADVANCE-INSTALMENTS (2026-08-05).

The advance accounting was already right. Two gaps sat above it.

(a) THE DEDUCTION CAME FROM THE FORM, NOT THE BALANCE.
    run_payroll read `float(inputs.get("advance", 0) or 0)`. The payroll
    FORM prefilled that box from the open balance, so the automation
    lived in the browser: any other caller — a script, the agent, a
    future importer — deducted 0 and the advance stayed open forever.
    That `or 0` was the whole bug: a missing field became a zero
    deduction, indistinguishable from a deliberate one.

(b) NO RECORD OF WHICH INSTALMENT WAS TAKEN WHEN.
    apply_advance_deduction did `adv.remaining -= applied` and stopped.
    Nothing linked an instalment to the run that took it, so "how much
    have I paid so far?" had no answer but subtraction, and nothing
    stopped the same period being recovered twice.

The reference pattern was already in the same function: sales
commissions are settled INSIDE run_payroll with no form input and linked
back via SalesCommission.payroll_run_id. Advances now match.

One acceptance criterion needs a caveat, stated rather than hidden:
"re-running a payslip for the same month" is NOT reachable through the
app. run_payroll refuses a second run for a period, and there is no
delete path for a run — no route, no service. What IS reachable, and
what check 4 exercises, is the service being called twice for the same
period, which is the same hole that let the form own the automation.

Checks
  1. a run with NO advance input deducts the instalment automatically
  2. a typed number is respected exactly
  3. a typed 0 is a deliberate skip, and is recorded as such
  4. the same period cannot be recovered twice
  5. every instalment has a row linked to its run, line and period
  6. the payslip shows what was ACTUALLY applied, not what was asked
  7. the last instalment closes the advance and stops at the balance
  8. an employee with no advance is unaffected
  9. both the employee's page and the HR profile show the history
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__ADVINST_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ────────────────────────────────────────────────────────────
def _setup():
    _teardown()
    from app.models import Company, User, Employee, Plan
    from app.services.seed_coa import seed_default_coa
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.plan_gating import plan_allows
    from datetime import datetime

    # A maximal plan, the same trick audit_portal_403 uses. Picking the
    # first seeded plan that allows employees.view lands on `starter`,
    # which does not enable the HR module — and the module gate then 403s
    # /payroll/*, so check 9 fails for a reason that has nothing to do
    # with advances.
    plan = Plan.query.filter_by(code="__advinst__").first()
    if not plan:
        plan = Plan(code="__advinst__", name="Audit", name_ar="تدقيق",
                    allowed_subitems=None)
        plan.set_modules([
            "accounting", "sales", "inventory", "purchases", "pos", "crm",
            "hr", "reports", "agent", "employee_reports", "manufacturing",
            "evaluations", "insights", "settings",
        ])
        db.session.add(plan)
        db.session.flush()

    co = Company(name=f"{PREFIX}CO__", base_currency="EGP", vat_rate=0,
                 plan_id=plan.id)
    db.session.add(co)
    db.session.flush()
    seed_default_coa(co.id)
    co.intended_plan_id = plan.id
    db.session.commit()
    assert plan_allows("employees.view", co), "the audit plan blocks payroll"
    ensure_roles_ready_for_company(co.id)

    u = User(email=f"{PREFIX}owner@audit.local", full_name="AdvInst Owner",
             is_active=True, terms_version=get_terms_version(),
             terms_accepted_at=datetime.utcnow())
    u.set_password("Passw0rd!audit1")
    db.session.add(u)
    db.session.flush()
    db.session.commit()
    set_membership_role(u.id, co.id, "owner")

    emp = Employee(company_id=co.id, name="موظف السلفة", basic_salary=6000,
                   status="ACTIVE", start_date=date(2025, 1, 1), user_id=u.id)
    plain = Employee(company_id=co.id, name="موظف بلا سلفة",
                     basic_salary=4000, status="ACTIVE",
                     start_date=date(2025, 1, 1))
    db.session.add_all([emp, plain])
    db.session.commit()

    _STATE.update(cid=co.id, uid=u.id, emp_id=emp.id, plain_id=plain.id,
                  month=1)


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
    db.session.execute(text("DELETE FROM plans WHERE code='__advinst__'"))
    db.session.commit()
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text("DELETE FROM user_companies WHERE user_id=:u"),
                           {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _emp():
    from app.models import Employee
    return db.session.get(Employee, _STATE["emp_id"])


def _fresh_advance(amount=3000, months=3):
    """A brand-new ACTIVE advance, replacing any existing one."""
    from app.models import EmployeeAdvance, AdvanceSource, AdvanceStatus
    from app.services.advances import approve_advance
    for a in EmployeeAdvance.query.filter_by(
            employee_id=_STATE["emp_id"]).all():
        a.status = AdvanceStatus.CANCELLED
    db.session.flush()
    adv = approve_advance(_STATE["cid"], _STATE["emp_id"], amount, months,
                          date(2026, 1, 5), source=AdvanceSource.DIRECT,
                          actor_id=_STATE["uid"])
    db.session.commit()
    return adv


def _next_month():
    """Each run needs its own period — run_payroll refuses duplicates."""
    _STATE["month"] += 1
    return 2026, _STATE["month"]


def _run(advance_input="__omit__", employee_inputs=None):
    from app.services.payroll import run_payroll
    year, month = _next_month()
    inputs = employee_inputs
    if inputs is None:
        inputs = {} if advance_input == "__omit__" else {
            _STATE["emp_id"]: {"advance": advance_input}}
    run = run_payroll(_STATE["cid"], year, month, line_inputs=inputs,
                      created_by=_STATE["uid"], send_emails=False)
    db.session.commit()
    return run, year, month


def _line_for(run, employee_id=None):
    from app.models import PayrollLine
    return PayrollLine.query.filter_by(
        run_id=run.id, employee_id=employee_id or _STATE["emp_id"]).first()


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. a run with NO advance input deducts the instalment anyway")
def _():
    """(a) — the bug. Before this, an absent field meant a zero deduction
    and the advance stayed open forever on every non-form path."""
    from app.services.advances import repayments_for
    adv = _fresh_advance(3000, 3)
    # Recorded before the assertions so that when this check fails, the
    # ones after it fail on their own behaviour instead of a KeyError.
    _STATE["adv_id"] = adv.id
    assert float(adv.remaining) == 3000.0
    run, y, m = _run()                      # nothing passed at all
    db.session.refresh(adv)
    assert float(adv.remaining) == 2000.0, (
        f"remaining is {adv.remaining} after a run with no advance input — "
        "the deduction is still coming from the form, not the balance")
    rows = repayments_for(adv.id)
    assert len(rows) == 1 and float(rows[0].amount) == 1000.0
    assert rows[0].manual is False, "an automatic instalment is marked manual"
    _STATE["adv_id"] = adv.id
    return f"3000 -> {float(adv.remaining)} with no input, row {y}-{m:02d} = 1000.00"


@check("2. a typed number is respected exactly")
def _():
    from app.models import EmployeeAdvance
    from app.services.advances import repayments_for
    adv = db.session.get(EmployeeAdvance, _STATE["adv_id"])
    before = float(adv.remaining)
    run, y, m = _run("400")
    db.session.refresh(adv)
    assert float(adv.remaining) == before - 400, (
        f"typed 400 but remaining moved by {before - float(adv.remaining)}")
    rows = [r for r in repayments_for(adv.id)
            if (r.period_year, r.period_month) == (y, m)]
    assert rows, f"no instalment row was written for {y}-{m:02d}"
    row = rows[0]
    assert float(row.amount) == 400.0
    assert row.manual is True, "a typed number is not marked manual"
    return f"{before} -> {float(adv.remaining)}, row marked manual"


@check("3. a typed 0 is a deliberate skip, and is recorded as one")
def _():
    """Silence means «work it out»; an explicit 0 means «not this month».
    The row still gets written — "skipped" and "never run" are different
    facts and only a row can tell them apart."""
    from app.models import EmployeeAdvance
    from app.services.advances import repayments_for
    adv = db.session.get(EmployeeAdvance, _STATE["adv_id"])
    before = float(adv.remaining)
    run, y, m = _run("0")
    db.session.refresh(adv)
    assert float(adv.remaining) == before, (
        f"a typed 0 still deducted {before - float(adv.remaining)}")
    row = [r for r in repayments_for(adv.id)
           if (r.period_year, r.period_month) == (y, m)]
    assert row, "the skipped month left no row — indistinguishable from "\
                "a month that was never run"
    assert float(row[0].amount) == 0.0
    return f"remaining held at {before}, zero row recorded for {y}-{m:02d}"


@check("4. the same period cannot be recovered twice")
def _():
    """CAVEAT, stated rather than hidden: a literal payroll re-run is not
    reachable — run_payroll refuses a duplicate period and no delete path
    for a run exists anywhere in the app. What IS reachable is a second
    SERVICE call for a period already recovered, which is the same hole
    that let the form own the automation."""
    from app.models import EmployeeAdvance, PayrollRun, AdvanceRepayment
    from app.services.advances import apply_advance_deduction
    from app.services.payroll import run_payroll
    from app.services.ledger import LedgerError
    adv = db.session.get(EmployeeAdvance, _STATE["adv_id"])

    run, y, m = _run()                      # a real auto instalment
    db.session.refresh(adv)
    after_first = float(adv.remaining)
    rows_before = AdvanceRepayment.query.filter_by(advance_id=adv.id).count()

    for _attempt in range(3):
        got = apply_advance_deduction(_emp(), None, run=run,
                                      period_year=y, period_month=m)
        assert got == 1000.0, (
            f"a repeat call returned {got}; it should report the instalment "
            "already taken, not take another")
    db.session.commit()
    db.session.refresh(adv)
    assert float(adv.remaining) == after_first, (
        f"3 repeat calls moved the balance {after_first} -> "
        f"{float(adv.remaining)}")
    assert AdvanceRepayment.query.filter_by(advance_id=adv.id).count() \
        == rows_before, "a repeat call wrote a second row for the period"

    # and the run-level guard that makes the UI path unreachable
    try:
        run_payroll(_STATE["cid"], y, m, created_by=_STATE["uid"],
                    send_emails=False)
        raise AssertionError("a second run was created for the same period")
    except LedgerError as e:
        refused = str(e)
    return f"3 repeat calls: balance held at {after_first} · run refused: {refused}"


@check("5. every instalment is linked to its run, line and period")
def _():
    from app.models import EmployeeAdvance, PayrollLine
    from app.services.advances import repayments_for
    adv = db.session.get(EmployeeAdvance, _STATE["adv_id"])
    rows = repayments_for(adv.id)
    assert rows, "no instalment rows at all"
    for r in rows:
        assert r.payroll_run_id, f"{r} has no payroll run"
        assert r.period_year and r.period_month, f"{r} has no period"
        assert r.company_id == _STATE["cid"], "row leaked another tenant"
        if float(r.amount) > 0:
            assert r.payroll_line_id, f"{r} is not linked to a payslip line"
            line = db.session.get(PayrollLine, r.payroll_line_id)
            assert line.employee_id == _STATE["emp_id"]
    total = round(sum(float(r.amount) for r in rows), 2)
    assert abs(total - adv.paid_amount) < 0.005, (
        f"the rows total {total} but paid_amount says {adv.paid_amount} — "
        "the history and the balance disagree")
    return f"{len(rows)} rows, totalling {total} = paid_amount"


@check("6. the payslip shows what was applied, not what was asked")
def _():
    """The instalment is capped by the remaining balance. If the line kept
    the requested figure, the payslip would claim a deduction the
    employee never had, and net pay would be wrong by the difference."""
    from app.models import EmployeeAdvance
    adv = _fresh_advance(250, 3)            # instalment 83.33, remaining 250
    db.session.refresh(adv)
    run, y, m = _run()
    line = _line_for(run)
    db.session.refresh(adv)
    applied = round(250.0 - float(adv.remaining), 2)
    assert abs(float(line.advance_deduction) - applied) < 0.005, (
        f"payslip says {line.advance_deduction}, balance moved {applied}")
    _STATE["small_adv"] = adv.id
    return f"deducted {applied}, payslip shows {float(line.advance_deduction)}"


@check("7. the final instalment closes the advance and never overdraws")
def _():
    from app.models import EmployeeAdvance
    from app.services.advances import repayments_for
    adv = db.session.get(EmployeeAdvance, _STATE["small_adv"])
    guard = 0
    while adv.status.value == "ACTIVE" and guard < 8:
        _run()
        db.session.refresh(adv)
        guard += 1
    assert adv.status.value == "SETTLED", (
        f"advance is {adv.status.value} after {guard} runs")
    assert float(adv.remaining) == 0.0, f"remaining {adv.remaining}"
    total = round(sum(float(r.amount) for r in repayments_for(adv.id)), 2)
    assert abs(total - 250.0) < 0.005, (
        f"instalments total {total} against an advance of 250 — overdrawn")
    return f"settled in {guard} runs, instalments total {total}"


@check("8. an employee with no advance is unaffected")
def _():
    from app.models import AdvanceRepayment
    run, y, m = _run()
    line = _line_for(run, employee_id=_STATE["plain_id"])
    assert line is not None, "the second employee got no payslip line"
    assert float(line.advance_deduction) == 0.0, (
        f"an employee with no advance was deducted {line.advance_deduction}")
    rows = (AdvanceRepayment.query
            .filter_by(payroll_run_id=run.id).all())
    for r in rows:
        assert r.advance.employee_id != _STATE["plain_id"], (
            "a repayment row was written for an employee with no advance")
    return "no deduction, no row"


@check("9. both pages show the instalment history")
def _():
    """Acceptance criterion 5 — «الموظف يشوف في حسابه ... الأقساط
    المخصومة بتواريخها». The number alone was already there; what was
    missing is what it is made of."""
    from app.models import EmployeeAdvance
    from app.services.advances import repayments_for
    adv = _fresh_advance(3000, 3)
    _run()
    db.session.refresh(adv)
    rows = repayments_for(adv.id)
    assert rows, "fixture produced no instalment to display"

    app = _STATE["app"]
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(_STATE["uid"])
        s["_fresh"] = True
        s["active_company_id"] = _STATE["cid"]

    body = client.get("/my/account").get_data(as_text=True)
    assert "الأقساط المخصومة" in body, (
        "the employee's own page does not show the instalment history")
    assert f"{rows[0].period_year}-{rows[0].period_month:02d}" in body, (
        "the instalment's month is missing from the employee's page")

    prof = client.get(
        f"/payroll/employees/{_STATE['emp_id']}").get_data(as_text=True)
    assert "الأقساط المخصومة" in prof, (
        "the HR profile does not show the instalment history")
    return "instalments listed on /my/account and on the HR profile"


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
