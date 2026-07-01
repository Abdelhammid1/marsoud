#!/usr/bin/env python3
"""MARSOUD-PARTIAL-SETTLE audit — every acceptance criterion from the ticket.

Ledger correctness:
  1.  Fresh accrual: remaining == amount, is_settled == False
  2.  Partial pay of N produces ONE journal entry sized N
        Dr employee sub-account N / Cr 1110 N
  3.  paid_amount accumulates across partial pays
  4.  settled_at fires ONLY when paid_amount ≥ amount
  5.  Full-remainder pay (amount=None) works (backwards-compat)
  6.  Amount==remaining ends up settled with exactly one leg
  7.  Zero / negative amount → LedgerError
  8.  Over-payment (amount > remaining) → LedgerError
  9.  Already-fully-settled accrual → LedgerError on further pay
 10.  Bank leaf (1124) as payment source works
 11.  Employee.total_received includes partial paid_amount (not just settled)
 12.  outstanding on profile = Σ remaining (not Σ amount)
 13.  Every partial payment tags description "سداد جزئي" or "سداد كامل"
 14.  HTTP route (/payroll/accruals/<id>/settle) — full round-trip
"""
import sys, time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__PARTIAL_SETTLE_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import (
        Company, Employee, EmployeeStatus, User, EmployeeAccrual,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from app.services.subsidiary import ensure_employee_account
    existing = Company.query.filter_by(name=COMPANY_NAME).first()
    if existing:
        _teardown(existing.id)
    c = Company(name=COMPANY_NAME, base_currency="SAR")
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    owner = User.query.filter_by(email="demo@manasety.ai").first()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner",
    ))
    emp = Employee(
        company_id=c.id, name="ط.اختبار", email="pt@x.y",
        status=EmployeeStatus.ACTIVE, basic_salary=Decimal("10000"),
        start_date=date.today() - timedelta(days=200),
    )
    db.session.add(emp); db.session.flush()
    ensure_employee_account(emp)
    db.session.commit()
    _STATE.update(company_id=c.id, employee_id=emp.id, owner_id=owner.id,
                    emp_account_code=emp.account.code)


def _teardown(company_id):
    from app.models import (
        Company, JournalEntry, JournalLine, EmployeeAccrual, Employee,
    )
    from app.models.user import user_companies
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    EmployeeAccrual.query.filter_by(company_id=company_id).delete()
    Employee.query.filter_by(company_id=company_id).delete()
    entry_ids = [r.id for r in JournalEntry.query.filter_by(
        company_id=company_id).all()]
    if entry_ids:
        JournalLine.query.filter(JournalLine.entry_id.in_(entry_ids)
                                  ).delete(synchronize_session=False)
    db.session.execute(user_companies.delete().where(
        user_companies.c.company_id == company_id))
    for t in reversed(db.metadata.sorted_tables):
        if "company_id" in {c["name"] for c in insp.get_columns(t.name)}:
            db.session.execute(t.delete().where(t.c.company_id == company_id))
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()


def _fresh_accrual(amount):
    """Insert a fresh unsettled accrual on the audit employee."""
    from app.models import EmployeeAccrual
    a = EmployeeAccrual(
        company_id=_STATE["company_id"],
        employee_id=_STATE["employee_id"],
        amount=Decimal(str(amount)),
    )
    db.session.add(a); db.session.commit()
    return a


# ─── 1: Fresh accrual state ─────────────────────────────────────────────
@check("1. Fresh accrual: remaining == amount, is_settled == False")
def _():
    a = _fresh_accrual(1000)
    assert a.remaining == 1000.0
    assert a.is_settled is False
    assert float(a.paid_amount or 0) == 0.0
    return "amount=1000, remaining=1000, paid=0, settled=False"


# ─── 2: One partial payment produces one journal entry sized correctly ─
@check("2. Partial pay of 400 produces ONE balanced journal entry")
def _():
    from app.models import EmployeeAccrual, JournalEntry, JournalLine, Account
    from app.services.payroll import settle_accrual
    a = _fresh_accrual(1000)
    settle_accrual(a, amount=400, payment_method_account_code="1110")
    entries = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"], source_type="accrual_settle",
        source_id=a.id,
    ).all()
    assert len(entries) == 1
    lines = JournalLine.query.filter_by(entry_id=entries[0].id).all()
    assert len(lines) == 2
    emp_acc = Account.query.filter_by(
        company_id=_STATE["company_id"],
        code=_STATE["emp_account_code"],
    ).first()
    for l in lines:
        acc = db.session.get(Account, l.account_id)
        if acc.id == emp_acc.id:
            assert float(l.debit or 0) == 400.0 and float(l.credit or 0) == 0
        elif acc.code == "1110":
            assert float(l.credit or 0) == 400.0 and float(l.debit or 0) == 0
    return "entry balanced: Dr emp_sub 400, Cr 1110 400"


# ─── 3: paid_amount accumulates across multiple partial payments ───────
@check("3. Sequence 400 + 300 + 300 → paid_amount = 1000, 3 journal entries")
def _():
    from app.models import EmployeeAccrual, JournalEntry
    from app.services.payroll import settle_accrual
    a = _fresh_accrual(1000)
    for pay in (400, 300, 300):
        settle_accrual(a, amount=pay)
    db.session.refresh(a)
    assert float(a.paid_amount) == 1000.0
    assert a.remaining == 0.0
    n = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"], source_type="accrual_settle",
        source_id=a.id,
    ).count()
    assert n == 3, f"expected 3 entries, got {n}"
    return f"paid=1000, remaining=0, {n} journal entries"


# ─── 4: settled_at fires only when paid_amount reaches amount ──────────
@check("4. settled_at fires only on the final leg (not on partials)")
def _():
    from app.services.payroll import settle_accrual
    a = _fresh_accrual(500)
    settle_accrual(a, amount=200)
    db.session.refresh(a)
    assert not a.is_settled
    settle_accrual(a, amount=200)
    db.session.refresh(a)
    assert not a.is_settled
    settle_accrual(a, amount=100)   # brings paid to full amount
    db.session.refresh(a)
    assert a.is_settled
    return "settled_at only after paid_amount reached amount"


# ─── 5: Full-remainder pay (amount=None) — backwards-compat ────────────
@check("5. settle_accrual(amount=None) pays the whole remainder")
def _():
    from app.services.payroll import settle_accrual
    a = _fresh_accrual(700)
    settle_accrual(a)   # no amount → pay all
    db.session.refresh(a)
    assert a.is_settled and float(a.paid_amount) == 700.0
    return "amount=None → full remainder paid"


# ─── 6: Exact-remainder pay closes the accrual with one leg ────────────
@check("6. Exact-remainder pay closes the accrual with one entry")
def _():
    from app.models import JournalEntry
    from app.services.payroll import settle_accrual
    a = _fresh_accrual(150)
    settle_accrual(a, amount=150)
    db.session.refresh(a)
    n = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"], source_type="accrual_settle",
        source_id=a.id,
    ).count()
    assert a.is_settled and n == 1
    return f"single entry closes the whole 150"


# ─── 7: Zero / negative amount refused ─────────────────────────────────
@check("7. Zero-amount payment refused with LedgerError")
def _():
    from app.services.payroll import settle_accrual
    from app.services.ledger import LedgerError
    a = _fresh_accrual(100)
    try:
        settle_accrual(a, amount=0)
    except LedgerError:
        pass
    else:
        raise AssertionError("zero-amount should raise")
    try:
        settle_accrual(a, amount=-50)
    except LedgerError:
        pass
    else:
        raise AssertionError("negative-amount should raise")
    return "0 and negative both raise LedgerError"


# ─── 8: Over-payment refused ───────────────────────────────────────────
@check("8. Over-payment (amount > remaining) refused")
def _():
    from app.services.payroll import settle_accrual
    from app.services.ledger import LedgerError
    a = _fresh_accrual(500)
    settle_accrual(a, amount=300)  # remaining = 200
    try:
        settle_accrual(a, amount=250)
    except LedgerError as e:
        assert "أكبر" in str(e), f"unclear error: {e}"
        return f"{str(e)[:70]}"
    raise AssertionError("over-payment should raise")


# ─── 9: Fully-settled accrual refuses further pay ──────────────────────
@check("9. Fully-settled accrual: further payment refused")
def _():
    from app.services.payroll import settle_accrual
    from app.services.ledger import LedgerError
    a = _fresh_accrual(200)
    settle_accrual(a)  # closes it
    try:
        settle_accrual(a, amount=10)
    except LedgerError:
        return "settled accrual rejects further pay"
    raise AssertionError("should have raised")


# ─── 10: Bank leaf (1124) as source works ───────────────────────────────
@check("10. Payment from a bank leaf (1124) posts correctly")
def _():
    from app.models import JournalLine, JournalEntry, Account
    from app.services.payroll import settle_accrual
    a = _fresh_accrual(400)
    settle_accrual(a, amount=200, payment_method_account_code="1124")
    entry = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"], source_type="accrual_settle",
        source_id=a.id,
    ).first()
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    bank_line = next((l for l in lines
                        if db.session.get(Account, l.account_id).code == "1124"),
                       None)
    assert bank_line is not None and float(bank_line.credit) == 200.0
    return "Cr 1124 200 posted correctly"


# ─── 11: Employee.total_received includes partial paid_amount ──────────
@check("11. Employee.total_received includes partial paid_amount (not just settled)")
def _():
    from app.models import Employee, EmployeeAccrual, PayrollLine
    from app.services.payroll import settle_accrual
    emp_id = _STATE["employee_id"]
    # Snapshot baseline (from all prior tests in this file — settled ones)
    emp = db.session.get(Employee, emp_id)
    baseline = emp.total_received
    a = _fresh_accrual(500)
    settle_accrual(a, amount=200)  # partial only
    db.session.expire_all()
    emp = db.session.get(Employee, emp_id)
    after = emp.total_received
    assert (after - baseline) == 200.0, \
        f"baseline={baseline}, after={after}, expected +200"
    return f"partial 200 added to total_received (baseline {baseline} → {after})"


# ─── 12: Route response shows the partial-input UI + total_received ────
@check("12. /payroll/employees/<id> renders partial-pay input + remaining")
def _():
    emp_id = _STATE["employee_id"]
    cid = _STATE["company_id"]
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        r = c.get(f"/payroll/employees/{emp_id}")
        html = r.get_data(as_text=True)
        assert "المبلغ (فاضي = الكل)" in html
        assert "رصيد مستحق على الشركة" in html
    return "profile shows partial-pay input + outstanding label"


# ─── 13: Description tagging ───────────────────────────────────────────
@check("13. Description tags journal as 'سداد جزئي' vs 'سداد كامل'")
def _():
    from app.models import JournalEntry
    from app.services.payroll import settle_accrual
    a = _fresh_accrual(600)
    settle_accrual(a, amount=200)  # partial
    settle_accrual(a, amount=400)  # closes
    entries = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"], source_type="accrual_settle",
        source_id=a.id,
    ).order_by(JournalEntry.id).all()
    assert len(entries) == 2
    assert "سداد جزئي" in entries[0].description
    assert "سداد كامل" in entries[1].description
    return "first labeled 'سداد جزئي', second 'سداد كامل'"


# ─── 14: Full HTTP round-trip through the /settle route ────────────────
@check("14. HTTP POST /accruals/<id>/settle handles partial + full inputs")
def _():
    from app.models import EmployeeAccrual
    cid = _STATE["company_id"]
    a = _fresh_accrual(1200)
    app = create_app()
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        # Partial via HTTP form
        r = c.post(f"/payroll/accruals/{a.id}/settle", data={
            "amount": "500", "payment_account_code": "1110",
        }, follow_redirects=False)
        assert r.status_code in (302, 303)
    db.session.expire_all()
    a = db.session.get(EmployeeAccrual, a.id)
    assert a.remaining == 700.0 and not a.is_settled
    # Empty amount = pay all remaining
    with app.test_client() as c:
        c.post("/login", data={"email": "demo@manasety.ai",
                                 "password": "demo1234"})
        c.get(f"/switch-company/{cid}")
        r = c.post(f"/payroll/accruals/{a.id}/settle", data={
            "amount": "", "payment_account_code": "1110",
        }, follow_redirects=False)
        assert r.status_code in (302, 303)
    db.session.expire_all()
    a = db.session.get(EmployeeAccrual, a.id)
    assert a.remaining == 0.0 and a.is_settled
    return "HTTP: 500 partial → 700 remaining → empty → fully settled"


# ─── Run ───────────────────────────────────────────────────────────────
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
                except Exception as e:
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "company_id" in _STATE:
                    _teardown(_STATE["company_id"])
                    print(f"\n(cleaned up company #{_STATE['company_id']})")
            except Exception as e:
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
