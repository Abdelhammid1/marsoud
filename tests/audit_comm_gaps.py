#!/usr/bin/env python3
"""MARSOUD-COMM-01 — gap closure audit.

Covers the two acceptance criteria that the original audit didn't verify:

  #9  Multi-month rollover: a negative carry-forward balance that exceeds
      next month's earnings rolls over again into the month after that,
      and keeps rolling until earned commissions absorb it.

  #12 Per-rep cross-check: sum(line.commissions for rep across ALL runs)
      MUST equal sum(amount of PAID rows for the same rep). If those two
      numbers ever disagree, the payslip is lying or the report is.

This audit drives the service layer directly (no HTTP), since both gaps
are about the in-memory math, not the UI. It uses a unique test year
(2031) to avoid colliding with real or prior-test data.
"""
import sys
from datetime import date
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
TEST_YEAR = 2031   # far enough out that no real or prior-audit data lives here


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ────────────────────────────────────────────────────────────
def _fixture():
    """Returns (company, sales_rep_user, employee, customer, invoice)
    cleaned for the test year.

    The employee is linked to a User (sales_rep) via User.employee_id —
    that's the link settle_commissions_for_employee follows.
    """
    from app.models import (
        Company, User, Employee, EmployeeStatus, ContractType,
        Customer, Invoice, InvoiceStatus, SalesCommission,
        PayrollRun, PayrollLine,
    )
    company = Company.query.order_by(Company.id).first()
    cid = company.id

    # Pick or create a dedicated test user (acts as the sales rep)
    rep_email = "comm_gap_rep@example.com"
    rep = User.query.filter_by(email=rep_email).first()
    if not rep:
        rep = User(
            email=rep_email, full_name="مندوب اختبار العمولات",
            password_hash="x",
            is_active=True, is_superadmin=False,
        )
        db.session.add(rep)
        db.session.flush()

    # Dedicated test employee, linked to the rep user
    emp = Employee.query.filter_by(
        company_id=cid, email=rep_email,
    ).first()
    if not emp:
        emp = Employee(
            company_id=cid,
            name="موظف اختبار العمولات",
            employee_number="COMM-GAP-EMP",
            email=rep_email,
            phone="000",
            job_title="مندوب",
            start_date=date(2020, 1, 1),
            contract_type=ContractType.FULL_TIME,
            status=EmployeeStatus.ACTIVE,
            basic_salary=2000, allowances=0, deductions=0,
            is_active=True,
        )
        db.session.add(emp)
        db.session.flush()
    # Link the rep User to the Employee (required by settle_commissions)
    rep.employee_id = emp.id

    # Dedicated test customer w/ this rep + 10% rate
    cust = Customer.query.filter_by(
        company_id=cid, name="عميل اختبار العمولات",
    ).first()
    if not cust:
        cust = Customer(
            company_id=cid,
            name="عميل اختبار العمولات",
            phone="111", email="comm_gap_cust@example.com",
        )
        db.session.add(cust)
        db.session.flush()
    cust.sales_rep_id = rep.id
    cust.commission_rate = 10  # 10%

    # A throwaway invoice — needed only because SalesCommission.invoice_id
    # is NOT NULL. The amounts don't matter for the settlement logic.
    inv = Invoice.query.filter_by(
        company_id=cid, number="COMM-GAP-INV",
    ).first()
    if not inv:
        inv = Invoice(
            company_id=cid,
            number="COMM-GAP-INV",
            customer_id=cust.id,
            issue_date=date(TEST_YEAR, 1, 1),
            due_date=date(TEST_YEAR, 2, 1),
            subtotal=1000, tax_amount=0, total=1000,
            status=InvoiceStatus.SENT, currency="EGP",
        )
        db.session.add(inv)
        db.session.flush()

    # Clean any prior test rows for this rep + test year
    SalesCommission.query.filter(
        SalesCommission.sales_rep_id == rep.id,
        SalesCommission.period_year == TEST_YEAR,
    ).delete(synchronize_session=False)
    # And clean prior PayrollRuns + their lines for the test year
    runs = PayrollRun.query.filter(
        PayrollRun.company_id == cid,
        PayrollRun.period_year == TEST_YEAR,
    ).all()
    for r in runs:
        PayrollLine.query.filter_by(run_id=r.id).delete()
        db.session.delete(r)
    # Also nuke orphan PayrollLine rows (test cleanup paranoia — see
    # earlier session notes about SQLite rowid reuse).
    valid_run_ids = {r.id for r in PayrollRun.query.all()}
    orphans = [L for L in PayrollLine.query.filter_by(employee_id=emp.id).all()
                if L.run_id not in valid_run_ids]
    for o in orphans:
        db.session.delete(o)
    db.session.commit()
    return company, rep, emp, cust, inv


def _insert_commission(*, company_id, rep_id, cust_id, inv_id,
                        amount, period_month, is_cf=False):
    """Insert one SalesCommission row, bypassing the full record_payment
    pipeline. We just need rows to feed settle_commissions_for_employee."""
    from app.models import SalesCommission
    base = abs(amount) * 10   # rate=10% → base = amount × 10
    row = SalesCommission(
        company_id=company_id,
        sales_rep_id=rep_id,
        customer_id=cust_id,
        invoice_id=inv_id,
        payment_id=None,
        taxable_base=base if amount > 0 else -base,
        amount=amount,
        commission_rate=10,
        period_year=TEST_YEAR,
        period_month=period_month,
        status="UNPAID",
        journal_entry_id=None,
        is_carry_forward=is_cf,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _run_simple_payroll(emp_id, year, month):
    """Call run_payroll for just this employee. Returns the line for the
    employee. amount_paid is set to net so no accruals are created."""
    from app.services.payroll import run_payroll
    from app.models import PayrollLine, Employee
    company_id = Employee.query.get(emp_id).company_id
    # Tell run_payroll to give every other employee a trivial 0 working_days
    # input (it processes every active employee) — easier: just process all
    # and pick our line.
    line_inputs = {
        emp_id: {"working_days": 30, "amount_paid": ""},
    }
    run = run_payroll(company_id, year, month,
                      line_inputs=line_inputs, send_emails=False)
    line = PayrollLine.query.filter_by(
        run_id=run.id, employee_id=emp_id,
    ).first()
    return run, line


# ─── Gap #9: multi-month rollover ──────────────────────────────────────
@check("#9a  Earned +100 then refunded → next month rollover is -100")
def _():
    """Month 1: +100 earned. Month 2: refund creates -100 carry-forward."""
    company, rep, emp, cust, inv = _fixture()
    # Month 1: +100 commission
    _insert_commission(company_id=company.id, rep_id=rep.id,
                        cust_id=cust.id, inv_id=inv.id,
                        amount=100, period_month=1)
    db.session.commit()
    run1, line1 = _run_simple_payroll(emp.id, TEST_YEAR, 1)
    assert abs(float(line1.commissions) - 100) < 0.01, \
        f"month 1 should pay +100, got {line1.commissions}"

    # Simulate refund AFTER run 1 settled → -100 carry-forward in month 2
    _insert_commission(company_id=company.id, rep_id=rep.id,
                        cust_id=cust.id, inv_id=inv.id,
                        amount=-100, period_month=2, is_cf=True)
    db.session.commit()
    return f"month 1 line.commissions = {line1.commissions} (+100 settled), -100 cf queued for month 2"


@check("#9b  Month 2 with no new earnings → line.commissions = 0, row stays UNPAID")
def _():
    """The -100 row is in scope for month 2. Net = -100. New behavior:
    don't reduce salary, leave the row UNPAID so month 3 picks it up."""
    from app.models import Employee, SalesCommission, User
    emp = Employee.query.filter_by(employee_number="COMM-GAP-EMP").first()
    rep = User.query.filter_by(email="comm_gap_rep@example.com").first()
    run2, line2 = _run_simple_payroll(emp.id, TEST_YEAR, 2)
    assert abs(float(line2.commissions or 0)) < 0.01, \
        f"month 2 should NOT touch salary (got {line2.commissions})"
    # The -100 carry-forward row must still be UNPAID
    cf_row = SalesCommission.query.filter(
        SalesCommission.sales_rep_id == rep.id,
        SalesCommission.period_year == TEST_YEAR,
        SalesCommission.period_month == 2,
    ).first()
    assert cf_row.status == "UNPAID", \
        f"carry-forward row should stay UNPAID, got {cf_row.status}"
    return f"month 2 line.commissions = 0 (rolled forward), cf row still UNPAID"


@check("#9c  Month 3 with +50 earnings → still net negative, no settlement")
def _():
    """+50 in month 3 is not enough to absorb -100. Net = -50. Same rule:
    don't touch salary, leave everything UNPAID for month 4."""
    from app.models import Employee, SalesCommission, User
    company_id = Employee.query.filter_by(employee_number="COMM-GAP-EMP").first().company_id
    emp = Employee.query.filter_by(employee_number="COMM-GAP-EMP").first()
    rep = User.query.filter_by(email="comm_gap_rep@example.com").first()
    cust_id = SalesCommission.query.filter_by(sales_rep_id=rep.id).first().customer_id
    inv_id = SalesCommission.query.filter_by(sales_rep_id=rep.id).first().invoice_id
    # Add +50 earnings in month 3
    _insert_commission(company_id=company_id, rep_id=rep.id,
                        cust_id=cust_id, inv_id=inv_id,
                        amount=50, period_month=3)
    db.session.commit()

    run3, line3 = _run_simple_payroll(emp.id, TEST_YEAR, 3)
    assert abs(float(line3.commissions or 0)) < 0.01, \
        f"month 3 should NOT settle (net=-50), got {line3.commissions}"
    # Both rows (-100 and +50) must still be UNPAID
    rows = SalesCommission.query.filter(
        SalesCommission.sales_rep_id == rep.id,
        SalesCommission.period_year == TEST_YEAR,
        SalesCommission.period_month.in_([2, 3]),
    ).all()
    for r in rows:
        assert r.status == "UNPAID", \
            f"row period={r.period_month} should be UNPAID, got {r.status}"
    return "month 3 line.commissions = 0, both -100 + +50 rows still UNPAID"


@check("#9d  Month 4 with +200 → net = +150, settles ALL backlog at once")
def _():
    """+200 in month 4. Total scope: -100 (cf) + 50 (m3) + 200 (m4) = +150.
    All rows settled in run 4. line.commissions = 150."""
    from app.models import Employee, SalesCommission, User
    emp = Employee.query.filter_by(employee_number="COMM-GAP-EMP").first()
    rep = User.query.filter_by(email="comm_gap_rep@example.com").first()
    cust_id = SalesCommission.query.filter_by(sales_rep_id=rep.id).first().customer_id
    inv_id = SalesCommission.query.filter_by(sales_rep_id=rep.id).first().invoice_id

    _insert_commission(company_id=emp.company_id, rep_id=rep.id,
                        cust_id=cust_id, inv_id=inv_id,
                        amount=200, period_month=4)
    db.session.commit()

    run4, line4 = _run_simple_payroll(emp.id, TEST_YEAR, 4)
    assert abs(float(line4.commissions) - 150) < 0.01, \
        f"month 4 should pay +150 (net of backlog), got {line4.commissions}"
    # All UNPAID rows from this rep in test year are now PAID
    leftover = SalesCommission.query.filter(
        SalesCommission.sales_rep_id == rep.id,
        SalesCommission.period_year == TEST_YEAR,
        SalesCommission.status == "UNPAID",
    ).count()
    assert leftover == 0, \
        f"expected 0 UNPAID after run 4, got {leftover}"
    return "month 4 line.commissions = 150 (settled -100 + 50 + 200)"


# ─── Gap #12: cross-check sum(line.commissions) == sum(paid rows) ──────
@check("#12 Cross-check: sum(line.commissions) == sum(PAID rows) per rep")
def _():
    """The invariant we promised in the original ticket. After the full
    4-month scenario:
      line.commissions across runs = +100, 0, 0, +150 → sum 250
      PAID rows: +100 (run 1) + -100 + 50 + 200 (all run 4) → sum 250 ✓
    """
    from app.models import (
        Employee, PayrollLine, SalesCommission, PayrollRun, User,
    )
    emp = Employee.query.filter_by(employee_number="COMM-GAP-EMP").first()
    rep = User.query.filter_by(email="comm_gap_rep@example.com").first()

    # Sum payroll-side commissions across the 4 test-year runs
    run_ids = [r.id for r in PayrollRun.query.filter(
        PayrollRun.company_id == emp.company_id,
        PayrollRun.period_year == TEST_YEAR,
    ).all()]
    lines = PayrollLine.query.filter(
        PayrollLine.employee_id == emp.id,
        PayrollLine.run_id.in_(run_ids),
    ).all()
    line_total = sum(float(L.commissions or 0) for L in lines)

    # Sum commission-side: every PAID row for this rep in test year
    paid_rows = SalesCommission.query.filter(
        SalesCommission.sales_rep_id == rep.id,
        SalesCommission.period_year == TEST_YEAR,
        SalesCommission.status == "PAID",
    ).all()
    row_total = sum(float(r.amount or 0) for r in paid_rows)

    assert abs(line_total - row_total) < 0.01, \
        f"cross-check FAILED: line_total={line_total}, row_total={row_total}"
    # And the commission_report's paid_total should match too
    from app.services.sales_commissions import commission_report
    rep_bucket = next((b for b in commission_report(emp.company_id)
                       if b["sales_rep_id"] == rep.id), None)
    assert rep_bucket, "rep not in commission_report output"
    assert abs(rep_bucket["paid_total"] - row_total) < 0.01, \
        f"report.paid_total={rep_bucket['paid_total']} mismatch row_total={row_total}"
    return f"line_total = row_total = report.paid_total = {line_total:.2f} ✓"


# ─── Cleanup ────────────────────────────────────────────────────────────
def _cleanup():
    """Wipe the test data so re-runs are clean. Keeps fixture rows so the
    second run reuses them."""
    from app.models import (
        SalesCommission, PayrollRun, PayrollLine, User, Employee,
    )
    rep = User.query.filter_by(email="comm_gap_rep@example.com").first()
    if not rep:
        return
    SalesCommission.query.filter(
        SalesCommission.sales_rep_id == rep.id,
        SalesCommission.period_year == TEST_YEAR,
    ).delete(synchronize_session=False)
    emp = Employee.query.filter_by(employee_number="COMM-GAP-EMP").first()
    if emp:
        runs = PayrollRun.query.filter(
            PayrollRun.company_id == emp.company_id,
            PayrollRun.period_year == TEST_YEAR,
        ).all()
        for r in runs:
            PayrollLine.query.filter_by(run_id=r.id).delete()
            db.session.delete(r)
    db.session.commit()


def main():
    app = create_app()
    with app.app_context():
        passed = failed = 0
        for label, fn in CHECKS:
            try:
                msg = fn()
                print(f"\033[92mPASS\033[0m  {label}")
                if msg:
                    print(f"        {msg}")
                passed += 1
            except Exception as e:
                print(f"\033[91mFAIL\033[0m  {label}")
                print(f"        {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                failed += 1
        _cleanup()
        print()
        print(f"  {passed}/{passed + failed} checks passed.")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
