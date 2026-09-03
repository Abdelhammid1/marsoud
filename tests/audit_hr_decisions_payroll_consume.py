#!/usr/bin/env python3
"""MARSOUD-TKT-HR-DECISIONS-02-PAYROLL-CONSUME (2026-09-03).

Phase 2 of HR decisions: PENDING_PAYROLL decisions get folded into
the next payroll run automatically, flipped to EXECUTED, and back-
linked to that run — one atomic transaction so a rollback reverts
everything together.

Checks:
  1. BONUS NEXT_PAYROLL → PayrollLine.bonus += amount; decision
     EXECUTED + payroll_run_id set.
  2. PENALTY NEXT_PAYROLL → PayrollLine.deductions += amount;
     decision EXECUTED + payroll_run_id set.
  3. Two PENALTY decisions for the same employee accumulate
     (150 + 100 = 250), both back-linked.
  4. Manual line_inputs override composes with the decision
     (100 + 500 = 600) — both sources are legitimate.
  5. Idempotency — after one consumption, a second run doesn't
     re-consume the same decision (status is EXECUTED, not
     PENDING_PAYROLL).
  6. CANCELLED decision is invisible to the consume path.
"""
import os
import sys
from pathlib import Path
from datetime import datetime, date, timedelta
from decimal import Decimal

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
    """Company + owner + seeded CoA. Duplicated from
    tests/audit_hr_decisions.py:_boot — self-contained per audit."""
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

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
    db.session.execute(text(
        "DELETE FROM journal_entries WHERE company_id NOT IN (SELECT id FROM companies)"))
    db.session.execute(text(
        "DELETE FROM journal_lines WHERE entry_id NOT IN (SELECT id FROM journal_entries)"))
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C",
                    allowed_subitems=None)
        db.session.add(plan)
    plan.set_modules(["accounting", "sales", "hr", "reports"])
    db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency="EGP",
                subdomain=prefix.lower(), plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.commit()
    seed_default_coa(c.id); db.session.commit()

    try:
        from app.services.legal import get_terms_version
        tv = get_terms_version() or "audit"
    except Exception:
        tv = "audit"
    owner = User(email=f"owner__{prefix.lower()}__@x.io",
                 full_name=f"Owner {prefix}", is_active=True,
                 email_verified_at=datetime.utcnow(),
                 terms_version=tv, terms_accepted_at=datetime.utcnow())
    owner.set_password("pw12345678")
    db.session.add(owner); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner"))
    db.session.commit()
    return owner.email, c.id, owner.id


def _make_employee(cid, name="موظف اختبار", basic_salary=3000):
    from app import db
    from app.models import Employee, EmployeeStatus
    from app.services.subsidiary import ensure_employee_account
    emp = Employee(company_id=cid, name=name, job_title="محاسب",
                    basic_salary=Decimal(str(basic_salary)),
                    status=EmployeeStatus.ACTIVE, is_active=True,
                    start_date=date.today() - timedelta(days=365))
    db.session.add(emp); db.session.flush()
    ensure_employee_account(emp)
    db.session.commit()
    return emp


def _queue_bonus(cid, emp, amount, title="مكافأة", actor_id=None):
    """create → PENDING_PAYROLL via execute_decision."""
    from app.services.hr_decisions import (
        create_decision, execute_decision,
    )
    dec = create_decision(
        cid, employee_id=emp.id, kind="BONUS",
        effective_date=date.today(),
        title=title, timing="NEXT_PAYROLL",
        amount=amount, actor_id=actor_id)
    execute_decision(dec, actor_id=actor_id)
    return dec


def _queue_penalty(cid, emp, amount, title="جزاء",
                    body="سبب الجزاء موثّق", actor_id=None):
    from app.services.hr_decisions import (
        create_decision, execute_decision,
    )
    dec = create_decision(
        cid, employee_id=emp.id, kind="PENALTY",
        effective_date=date.today(),
        title=title, body=body,
        timing="NEXT_PAYROLL",
        amount=amount, actor_id=actor_id)
    execute_decision(dec, actor_id=actor_id)
    return dec


@check("1. BONUS NEXT_PAYROLL → PayrollLine.bonus += amount, "
        "decision EXECUTED + linked to run")
def _():
    from app import create_app, db
    from app.models import PayrollLine
    from app.services.payroll import run_payroll
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HRCON1")
        emp = _make_employee(cid, "أحمد", 3000)
        dec = _queue_bonus(cid, emp, 300, title="إنجاز يناير",
                            actor_id=oid)
        assert dec.status == "PENDING_PAYROLL"
        # Pick a month far enough in the past that Employee.start_date
        # doesn't clip billable days.
        run = run_payroll(cid, 2026, 6, created_by=oid,
                          send_emails=False)
        line = PayrollLine.query.filter_by(
            run_id=run.id, employee_id=emp.id).first()
        assert line is not None, "PayrollLine missing"
        assert float(line.bonus) == 300.0, \
            f"bonus={line.bonus}, want 300.00"
        db.session.refresh(dec)
        assert dec.status == "EXECUTED", \
            f"status={dec.status}, want EXECUTED"
        assert dec.payroll_run_id == run.id, \
            f"payroll_run_id={dec.payroll_run_id}, want {run.id}"
        assert dec.executed_at is not None
        return f"bonus=300 folded; decision #{dec.id} → run #{run.id}"


@check("2. PENALTY NEXT_PAYROLL → PayrollLine.deductions += amount")
def _():
    from app import create_app, db
    from app.models import PayrollLine
    from app.services.payroll import run_payroll
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HRCON2")
        emp = _make_employee(cid, "منى", 3000)
        dec = _queue_penalty(cid, emp, 200, title="تأخير متكرر",
                              actor_id=oid)
        run = run_payroll(cid, 2026, 6, created_by=oid,
                          send_emails=False)
        line = PayrollLine.query.filter_by(
            run_id=run.id, employee_id=emp.id).first()
        assert float(line.deductions) == 200.0, \
            f"deductions={line.deductions}, want 200.00"
        db.session.refresh(dec)
        assert dec.status == "EXECUTED"
        assert dec.payroll_run_id == run.id
        return f"deductions=200 folded"


@check("3. Two PENALTY decisions for the same employee accumulate "
        "(150 + 100 = 250)")
def _():
    from app import create_app, db
    from app.models import PayrollLine, HrDecision
    from app.services.payroll import run_payroll
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HRCON3")
        emp = _make_employee(cid, "سالم", 4000)
        d1 = _queue_penalty(cid, emp, 150,
                             title="تأخير", actor_id=oid)
        d2 = _queue_penalty(cid, emp, 100,
                             title="غياب", actor_id=oid)
        run = run_payroll(cid, 2026, 6, created_by=oid,
                          send_emails=False)
        line = PayrollLine.query.filter_by(
            run_id=run.id, employee_id=emp.id).first()
        assert float(line.deductions) == 250.0, \
            f"deductions={line.deductions}, want 250.00"
        linked = HrDecision.query.filter_by(
            payroll_run_id=run.id).all()
        assert len(linked) == 2, f"linked count={len(linked)}"
        assert {d.id for d in linked} == {d1.id, d2.id}
        for d in linked:
            assert d.status == "EXECUTED"
        return "150 + 100 = 250 folded; both decisions linked"


@check("4. Manual line_inputs override + PENDING BONUS compose "
        "(100 + 500 = 600)")
def _():
    from app import create_app, db
    from app.models import PayrollLine
    from app.services.payroll import run_payroll
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HRCON4")
        emp = _make_employee(cid, "ياسمين", 5000)
        _queue_bonus(cid, emp, 500,
                      title="مكافأة تنفيذ",
                      actor_id=oid)
        run = run_payroll(cid, 2026, 6,
                          line_inputs={emp.id: {"bonus": 100}},
                          created_by=oid, send_emails=False)
        line = PayrollLine.query.filter_by(
            run_id=run.id, employee_id=emp.id).first()
        assert float(line.bonus) == 600.0, \
            f"bonus={line.bonus}, want 600.00 (100 manual + 500 decision)"
        return "manual 100 + decision 500 = 600"


@check("5. Idempotency — a second payroll month doesn't re-consume "
        "an already EXECUTED decision")
def _():
    from app import create_app, db
    from app.models import PayrollLine, HrDecision
    from app.services.payroll import run_payroll
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HRCON5")
        emp = _make_employee(cid, "خالد", 3000)
        dec = _queue_bonus(cid, emp, 400,
                            title="مكافأة يونيو", actor_id=oid)
        # Month 1 — consumes.
        run1 = run_payroll(cid, 2026, 6, created_by=oid,
                           send_emails=False)
        db.session.refresh(dec)
        assert dec.status == "EXECUTED"
        assert dec.payroll_run_id == run1.id
        line1 = PayrollLine.query.filter_by(
            run_id=run1.id, employee_id=emp.id).first()
        assert float(line1.bonus) == 400.0
        # Month 2 — should NOT re-consume.
        run2 = run_payroll(cid, 2026, 7, created_by=oid,
                           send_emails=False)
        line2 = PayrollLine.query.filter_by(
            run_id=run2.id, employee_id=emp.id).first()
        assert float(line2.bonus) == 0.0, \
            f"month 2 bonus={line2.bonus}, want 0 — decision re-consumed!"
        # Decision back-link still points to run1.
        db.session.refresh(dec)
        assert dec.payroll_run_id == run1.id, \
            f"back-link swapped to {dec.payroll_run_id}"
        # And no fresh HrDecision rows landed on run2.
        n_run2 = HrDecision.query.filter_by(
            payroll_run_id=run2.id).count()
        assert n_run2 == 0, \
            f"run2 got {n_run2} decisions linked; want 0"
        return f"decision consumed once, run2 clean"


@check("6. CANCELLED decision is invisible to run_payroll")
def _():
    from app import create_app, db
    from app.models import PayrollLine
    from app.services.payroll import run_payroll
    from app.services.hr_decisions import cancel_decision
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HRCON6")
        emp = _make_employee(cid, "سلمى", 3000)
        dec = _queue_bonus(cid, emp, 500,
                            title="سيتم إلغاؤها", actor_id=oid)
        cancel_decision(dec, reason="عدل عن القرار", actor_id=oid)
        db.session.refresh(dec)
        assert dec.status == "CANCELLED"
        run = run_payroll(cid, 2026, 6, created_by=oid,
                          send_emails=False)
        line = PayrollLine.query.filter_by(
            run_id=run.id, employee_id=emp.id).first()
        assert float(line.bonus) == 0.0, \
            f"cancelled decision leaked into bonus={line.bonus}"
        db.session.refresh(dec)
        assert dec.payroll_run_id is None, \
            f"cancelled decision back-linked to run: {dec.payroll_run_id}"
        return "cancelled decision skipped; bonus stays 0"


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
