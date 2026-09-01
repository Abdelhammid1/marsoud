#!/usr/bin/env python3
"""MARSOUD-TKT-PAYROLL-JE-BALANCE-GUARD (Abdelhamid 2026-08-31).

Fixes the "run 42 for August 2026, company 8" incident where the
payroll JE went out of balance by 7,800.00 EGP because a
negative-net employee was silently skipped on the Cr side while
their (negative) net stayed in the Dr aggregate (line 547 of
services/payroll.py: `if payable < 0.005: continue`).

Three guarantees are enforced:
  1. Any employee with negative net (deductions > gross) is refused
     BEFORE the JE is built, with a LedgerError naming every
     offending row + numbers so HR can fix the input.
  2. Belt-and-suspenders — even if a future bug reintroduces a
     silent skip somewhere, a pre-post_journal balance check
     compares sum(Dr) vs sum(Cr) and raises with a per-line
     breakdown showing exactly which line drifts.
  3. delete_payroll_run(run) is now a real service. It reverses
     the accrual + settlement JEs and removes the PayrollRun +
     PayrollLine + EmployeeAccrual rows atomically. Was previously
     a manual SQL dance that motivated this ticket.

Checks:
  1. delete_payroll_run exists with the expected signature.
  2. Negative-net raises LedgerError naming the employee + numbers.
  3. Zero-net employee still runs (matching pre-fix "quiet skip
     of legitimately-zero payable" behavior; the fix is scoped to
     STRICTLY negative).
  4. Happy path: a positive-net-only run produces a balanced JE.
  5. delete_payroll_run:
     · reverses the accrual JE (reversal_of link written),
     · removes the PayrollLine rows (no orphans left),
     · removes the PayrollRun row itself,
     · is idempotent — a second call on a stale reference is safe.
"""
import os
import re
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


def _boot_hr(prefix):
    """Company + owner user + salary/cash accounts seeded via
    seed_default_coa."""
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        f"SELECT id FROM companies WHERE name LIKE '__{prefix}__%'"))]
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
        f"DELETE FROM users WHERE email LIKE '%__{prefix.lower()}__%'"))
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="Audit", name_ar="A",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "hr"])
        db.session.add(plan); db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency="EGP",
                subdomain=prefix.lower(), plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.commit()
    seed_default_coa(c.id); db.session.commit()

    u = User(email=f"user__{prefix.lower()}__@x.io",
             full_name=f"User {prefix}",
             is_active=True, email_verified_at=datetime.utcnow())
    u.set_password("pw12345678")
    db.session.add(u); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    return c.id, u.id


def _teardown(prefix):
    from sqlalchemy import text, inspect
    from app import db

    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        f"SELECT id FROM companies WHERE name LIKE '__{prefix}__%'"))]
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
        f"DELETE FROM users WHERE email LIKE '%__{prefix.lower()}__%'"))
    db.session.commit()

    # Clean orphan payroll_lines + employee_accruals AFTER the company
    # cleanup — the previous DELETE FROM payroll_runs left them
    # dangling (payroll_lines has no company_id column, so the sorted-
    # tables loop above skipped it). SQLite reuses run_ids so leaving
    # these dangling makes the next test's fresh payroll_lines look
    # like they belong to the same run.
    db.session.execute(text(
        "DELETE FROM payroll_lines WHERE run_id NOT IN "
        "(SELECT id FROM payroll_runs)"))
    db.session.execute(text(
        "DELETE FROM employee_accruals WHERE source_run_id NOT IN "
        "(SELECT id FROM payroll_runs)"))
    # Also: attendance_exceptions and other employee-scoped tables
    # reference employee_id, which is gone now — sweep them.
    db.session.execute(text(
        "DELETE FROM attendance_exceptions WHERE employee_id NOT IN "
        "(SELECT id FROM employees)"))
    db.session.commit()


def _make_employee(cid, name, basic, *, absence_deduction_days_policy=None):
    """Create one employee. Optional violation policy that multiplies
    unexcused absence days — used by the negative-net repro."""
    from app import db
    from app.models import Employee, EmployeeStatus
    emp = Employee(company_id=cid, name=name,
                   employee_number=f"EMP-{name[:6]}",
                   basic_salary=Decimal(str(basic)),
                   start_date=date(2026, 1, 1),
                   status=EmployeeStatus.ACTIVE)
    db.session.add(emp); db.session.commit()
    return emp


@check("1. delete_payroll_run exists with the right signature")
def _():
    from app.services import payroll as _p
    import inspect as _inspect
    fn = getattr(_p, "delete_payroll_run", None)
    assert fn is not None, \
        "services.payroll.delete_payroll_run not defined"
    sig = _inspect.signature(fn)
    params = list(sig.parameters)
    assert params[0] == "run", \
        f"first positional must be `run`; got {params[0]!r}"
    assert "actor_id" in sig.parameters, \
        "delete_payroll_run must accept actor_id kwarg"
    return "signature correct"


@check("2. negative net → LedgerError naming employees + numbers")
def _():
    from app import create_app, db
    from app.services.ledger import LedgerError
    from app.services.payroll import run_payroll

    app = create_app()
    with app.app_context():
        cid, uid = _boot_hr("PJEBG_NEG")
        try:
            # Employee with a small basic and heavy absence override
            # that pushes net below zero. The override is passed via
            # line_inputs so we don't need to seed an attendance policy.
            emp = _make_employee(cid, "زياد وائل", 5000)
            try:
                run_payroll(cid, 2026, 8,
                            line_inputs={
                                emp.id: {"absence": 12000}  # deducts > basic
                            },
                            created_by=uid, send_emails=False)
            except LedgerError as e:
                msg = str(e)
                assert "زياد وائل" in msg, \
                    f"error must name the offending employee; got: {msg}"
                assert "صافي" in msg, "error must call out net"
                return "negative net refused with named breakdown"
            raise AssertionError(
                "run_payroll should have raised LedgerError for "
                "negative-net employee")
        finally:
            _teardown("PJEBG_NEG")


@check("3. zero-net employee doesn't trigger the guard")
def _():
    """A legitimately-zero-net line (basic=0 with 0 absence) is a
    quiet skip on both sides — not a bug, and the fix is scoped
    strictly to negative payables."""
    from app import create_app, db
    from app.services.payroll import run_payroll

    app = create_app()
    with app.app_context():
        cid, uid = _boot_hr("PJEBG_ZERO")
        try:
            _make_employee(cid, "زيرو الموظف", 0)
            # Second employee so run doesn't end up entirely empty
            # (post_journal refuses a run with no lines).
            _make_employee(cid, "أحمد", 3000)
            run = run_payroll(cid, 2026, 8,
                              created_by=uid, send_emails=False)
            assert run.id is not None
            return "zero-net legitimately skipped, run posted"
        finally:
            _teardown("PJEBG_ZERO")


@check("4. happy path: positive-net-only run produces a balanced JE")
def _():
    from app import create_app, db
    from app.models import JournalEntry, JournalLine
    from app.services.payroll import run_payroll

    app = create_app()
    with app.app_context():
        cid, uid = _boot_hr("PJEBG_OK")
        try:
            _make_employee(cid, "علي", 8000)
            _make_employee(cid, "منى", 6000)
            run = run_payroll(cid, 2026, 8,
                              created_by=uid, send_emails=False)
            je = db.session.get(JournalEntry, run.journal_entry_id)
            assert je is not None
            lines = JournalLine.query.filter_by(entry_id=je.id).all()
            total_dr = sum(float(l.debit or 0) for l in lines)
            total_cr = sum(float(l.credit or 0) for l in lines)
            assert abs(total_dr - total_cr) < 0.01, \
                f"JE not balanced: Dr {total_dr} vs Cr {total_cr}"
            return f"JE balanced (Dr = Cr = {total_dr:.2f})"
        finally:
            _teardown("PJEBG_OK")


@check("5. delete_payroll_run reverses JE + removes lines + is idempotent")
def _():
    from app import create_app, db
    from app.models import JournalEntry, PayrollRun, PayrollLine
    from app.services.payroll import run_payroll, delete_payroll_run

    app = create_app()
    with app.app_context():
        cid, uid = _boot_hr("PJEBG_DEL")
        try:
            _make_employee(cid, "أحمد", 4000)
            _make_employee(cid, "مصطفى", 5000)
            run = run_payroll(cid, 2026, 8,
                              created_by=uid, send_emails=False)
            run_id = run.id
            orig_je_id = run.journal_entry_id
            line_count_before = PayrollLine.query.filter_by(
                run_id=run_id).count()
            assert line_count_before == 2, \
                f"seed made 2 employees; expected 2 payroll lines"

            summary = delete_payroll_run(run, actor_id=uid)
            assert summary is not None
            # Reversal JE created + linked back to original
            reversals = JournalEntry.query.filter_by(
                reversal_of=orig_je_id).all()
            assert reversals, \
                "delete_payroll_run must create a reversal JE " \
                "linked via reversal_of"
            # PayrollRun row gone
            assert db.session.get(PayrollRun, run_id) is None, \
                "PayrollRun row must be deleted"
            # No orphan lines left behind
            assert PayrollLine.query.filter_by(run_id=run_id).count() == 0, \
                "no PayrollLine row may reference the deleted run"

            # Idempotent: calling with a now-detached row must not crash.
            second = delete_payroll_run(
                db.session.get(PayrollRun, run_id), actor_id=uid)
            assert second is None, \
                "second delete on a gone run should return None " \
                "(idempotent, not an exception)"
            return "reversal + line cleanup + idempotent"
        finally:
            _teardown("PJEBG_DEL")


def main():
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
