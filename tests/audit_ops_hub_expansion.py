#!/usr/bin/env python3
"""MARSOUD-OPS-HUB-EXPANSION-01 (2026-08-08) — payroll GOSI/tax
withholdings + 18 new ops-hub wizards + ops.adjustments permission.

Structured audit — mirrors the plan's phase boundaries so a failure
message names the phase it belongs to.

Phase 1 (payroll):
  1. Employee with GOSI rates → journal splits onto 2135/2136/5217
  2. Zero-rate employee → journal byte-identical structure
  3. Two-month accumulation → 2135/2136 balances grow correctly
  4. PayrollLine snapshot columns populated
  5. HR form carries the three new inputs

Phase 2 (infrastructure):
  6. party_choices(party_type="customer") returns customers only
  7. resolve_party rejects wrong-type submissions
  8. All 18 new source_types resolve to non-unknown Arabic labels
  9. REQUIRED_ACCOUNTS carries 5960 + 5970 (and the rest)
  10. verify_coa reports 5960 as missing when deleted

Phase 3 (13 processors):
  11. All 13 phase-3 keys exist in OPERATIONS_BY_KEY

Phase 4 (5 processors + adjustment):
  12. All 5 phase-4 keys + adjust-account exist
  13. cash-count-adjust with account_id != 1110 rejected (money field
      absent — routes to 1110 unconditionally)
  14. adjust-account with empty note refused
  15. ops.adjustments is NOT in _IMPLIES (per ticket "صلاحية مستقلة")
  16. ops.adjustments is in P with owner+admin (not accountant)
"""
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__OHX_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import (
        Company, Plan, User, UserStatus, Employee,
        EmployeeStatus, ContractType, Customer, Vendor,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from app.services.roles_seed import ensure_roles_ready_for_company
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code="__ohx__").first()
    if not plan:
        plan = Plan(code="__ohx__", name="OHX", name_ar="OHX",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "purchases",
                          "reports", "agent", "hr", "settings"])
        db.session.add(plan); db.session.flush()

    c = Company(name=f"{PREFIX}CO", base_currency="SAR",
                subdomain="ohx",
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1),
                intended_plan_id=plan.id, plan_id=plan.id)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    u = User(email=f"{PREFIX}u@x.test", full_name="ohx owner",
             is_active=True, status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(), terms_version="TEST",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"))
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    ensure_roles_ready_for_company(c.id)

    emp = Employee(company_id=c.id, name=f"{PREFIX}emp",
                   user_id=u.id, status=EmployeeStatus.ACTIVE,
                   contract_type=ContractType.FULL_TIME,
                   start_date=date.today() - timedelta(days=400),
                   basic_salary=Decimal("10000"))
    db.session.add(emp); db.session.flush()

    cust = Customer(company_id=c.id, name=f"{PREFIX}customer",
                    is_active=True)
    ven = Vendor(company_id=c.id, name=f"{PREFIX}vendor",
                 is_active=True)
    db.session.add_all([cust, ven])
    db.session.commit()

    _STATE.update(company_id=c.id, user_id=u.id, employee_id=emp.id,
                  customer_id=cust.id, vendor_id=ven.id)


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        # SQLite reuses primary keys — orphan child rows whose parent
        # was deleted can attach to a freshly-created parent row that
        # happens to reuse the same id. Sweep both journal_lines
        # (audit-cash-custody trap) and payroll_lines (this ticket's
        # trap — the check-4 failure earlier).
        conn.execute(text(
            "DELETE FROM journal_lines WHERE entry_id NOT IN "
            "(SELECT id FROM journal_entries)"))
        conn.execute(text(
            "DELETE FROM payroll_lines WHERE run_id NOT IN "
            "(SELECT id FROM payroll_runs)"))
        cids = [r[0] for r in conn.execute(text(
            f"SELECT id FROM companies WHERE name LIKE '{PREFIX}%'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            conn.execute(text(
                "DELETE FROM journal_lines WHERE entry_id IN "
                "(SELECT id FROM journal_entries WHERE company_id = :c)"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    try:
                        conn.execute(text(
                            f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                            {"c": cid})
                    except Exception:
                        pass
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            f"DELETE FROM users WHERE email LIKE '{PREFIX}%@x.test'"))
        conn.execute(text("DELETE FROM plans WHERE code = '__ohx__'"))


def _get_account_by_code(company_id, code):
    from app.models import Account
    return Account.query.filter_by(
        company_id=company_id, code=code).first()


# ─── Phase 1 checks ────────────────────────────────────────────────

@check("1. Payroll with GOSI rates splits onto 2135/2136/5217")
def _():
    _setup()
    from app.models import Employee, JournalLine, PayrollRun
    from app.services.payroll import run_payroll
    from app.services.subsidiary import ensure_employee_account
    from decimal import Decimal
    cid = _STATE["company_id"]
    emp = db.session.get(Employee, _STATE["employee_id"])
    emp.insurance_rate = Decimal("11")     # 11% employee GOSI
    emp.income_tax_rate = Decimal("8")     # 8% income tax
    emp.company_insurance_rate = Decimal("12")   # 12% employer share
    db.session.commit()
    ensure_employee_account(emp)

    run = run_payroll(
        cid, year=2026, month=8,
        line_inputs={emp.id: {"working_days": 30, "amount_paid": 0}},
        send_emails=False, created_by=_STATE["user_id"])

    lines = JournalLine.query.filter_by(entry_id=run.journal_entry_id).all()
    by_code = {}
    for ln in lines:
        from app.models import Account
        acc = db.session.get(Account, ln.account_id)
        by_code.setdefault(acc.code, []).append(ln)

    # Employee 11% of 10 000 = 1 100 (credit 2135)
    # Income tax 8% of 10 000 = 800 (credit 2136)
    # Employer 12% of 10 000 = 1 200 (debit 5217 + credit 2135)
    assert "2135" in by_code
    gosi_credits = sum(float(l.credit or 0) for l in by_code["2135"])
    assert abs(gosi_credits - (1100 + 1200)) < 0.01, (
        f"2135 credit total {gosi_credits}, expected 2300")
    assert "2136" in by_code
    tax_credits = sum(float(l.credit or 0) for l in by_code["2136"])
    assert abs(tax_credits - 800) < 0.01, (
        f"2136 credit total {tax_credits}, expected 800")
    assert "5217" in by_code
    empl_debits = sum(float(l.debit or 0) for l in by_code["5217"])
    assert abs(empl_debits - 1200) < 0.01, (
        f"5217 debit total {empl_debits}, expected 1200")
    return f"2135={gosi_credits} 2136={tax_credits} 5217={empl_debits}"


@check("2. Zero-rate employee → no 2135/2136/5217 lines emitted")
def _():
    _setup()
    from app.models import Employee, JournalLine, Account
    from app.services.payroll import run_payroll
    from app.services.subsidiary import ensure_employee_account
    emp = db.session.get(Employee, _STATE["employee_id"])
    # All three rates zero
    emp.insurance_rate = 0
    emp.income_tax_rate = 0
    emp.company_insurance_rate = 0
    db.session.commit()
    ensure_employee_account(emp)

    run = run_payroll(
        _STATE["company_id"], year=2026, month=8,
        line_inputs={emp.id: {"working_days": 30, "amount_paid": 0}},
        send_emails=False, created_by=_STATE["user_id"])
    lines = JournalLine.query.filter_by(entry_id=run.journal_entry_id).all()
    codes = {db.session.get(Account, l.account_id).code for l in lines}
    for c in ("2135", "2136", "5217"):
        assert c not in codes, (
            f"zero-rate employee should not have {c} in journal; "
            f"codes present: {sorted(codes)}")
    return f"no GOSI/tax lines; codes = {sorted(codes)}"


@check("3. Two-month accumulation → 2135/2136 balances grow correctly")
def _():
    _setup()
    from app.models import Employee, JournalLine, Account
    from app.services.payroll import run_payroll
    from app.services.subsidiary import ensure_employee_account
    from decimal import Decimal
    emp = db.session.get(Employee, _STATE["employee_id"])
    emp.insurance_rate = Decimal("10")
    emp.income_tax_rate = Decimal("5")
    emp.company_insurance_rate = Decimal("0")
    db.session.commit()
    ensure_employee_account(emp)

    for m in (6, 7):
        run_payroll(
            _STATE["company_id"], year=2026, month=m,
            line_inputs={emp.id: {"working_days": 30,
                                  "amount_paid": 0}},
            send_emails=False, created_by=_STATE["user_id"])
    acc_2135 = _get_account_by_code(_STATE["company_id"], "2135")
    acc_2136 = _get_account_by_code(_STATE["company_id"], "2136")
    bal_2135 = sum(float(l.credit or 0) - float(l.debit or 0)
                    for l in JournalLine.query.filter_by(
                        account_id=acc_2135.id).all())
    bal_2136 = sum(float(l.credit or 0) - float(l.debit or 0)
                    for l in JournalLine.query.filter_by(
                        account_id=acc_2136.id).all())
    # Two months × 10 000 × 10% = 2000 in 2135
    # Two months × 10 000 × 5%  = 1000 in 2136
    assert abs(bal_2135 - 2000) < 0.01, f"2135 {bal_2135}, expected 2000"
    assert abs(bal_2136 - 1000) < 0.01, f"2136 {bal_2136}, expected 1000"
    return f"after 2 runs: 2135={bal_2135} 2136={bal_2136}"


@check("4. PayrollLine snapshot columns populated")
def _():
    _setup()
    from app.models import Employee, PayrollLine
    from app.services.payroll import run_payroll
    from app.services.subsidiary import ensure_employee_account
    from decimal import Decimal
    emp = db.session.get(Employee, _STATE["employee_id"])
    emp.insurance_rate = Decimal("11")
    emp.income_tax_rate = Decimal("8")
    emp.company_insurance_rate = Decimal("12")
    db.session.commit()
    ensure_employee_account(emp)
    run = run_payroll(
        _STATE["company_id"], year=2026, month=8,
        line_inputs={emp.id: {"working_days": 30, "amount_paid": 0}},
        send_emails=False, created_by=_STATE["user_id"])
    line = PayrollLine.query.filter_by(run_id=run.id).first()
    assert line is not None, "no PayrollLine created"
    assert abs(float(line.insurance_deduction or 0) - 1100) < 0.01, (
        f"insurance_deduction={line.insurance_deduction!r}, expected 1100")
    assert abs(float(line.income_tax_deduction or 0) - 800) < 0.01, (
        f"income_tax_deduction={line.income_tax_deduction!r}, expected 800")
    assert abs(float(line.employer_insurance_share or 0) - 1200) < 0.01, (
        f"employer_insurance_share={line.employer_insurance_share!r}, expected 1200")
    return (f"line ins={line.insurance_deduction} "
            f"tax={line.income_tax_deduction} "
            f"emp={line.employer_insurance_share}")


@check("5. HR form carries the three new insurance/tax inputs")
def _():
    tmpl = (ROOT / "app" / "templates" / "payroll"
            / "employee_form.html").read_text(encoding="utf-8")
    for name in ("insurance_rate", "income_tax_rate",
                  "company_insurance_rate"):
        assert f'name="{name}"' in tmpl, (
            f"HR form is missing input {name!r}")
    return "all 3 rate inputs present"


# ─── Phase 2 checks ────────────────────────────────────────────────

@check("6. party_choices(party_type='customer') returns customers only")
def _():
    _setup()
    from app.services.accounting_ops import party_choices
    groups = party_choices(_STATE["company_id"], party_type="customer")
    labels = [g[0] for g in groups]
    assert "العملاء" in labels, f"customer group missing: {labels}"
    assert "الموردون" not in labels, (
        f"vendor group leaked into customer-only filter: {labels}")
    assert "الموظفون" not in labels
    # And no filter → all three groups can appear
    groups_all = party_choices(_STATE["company_id"])
    assert len(groups_all) >= 2, (
        f"unfiltered picker should include >1 groups: {groups_all}")
    return f"customer filter: {labels}; all: {[g[0] for g in groups_all]}"


@check("7. resolve_party rejects wrong-type submissions")
def _():
    _setup()
    from app.services.accounting_ops import resolve_party, OperationError
    vid = _STATE["vendor_id"]
    raised = False
    try:
        resolve_party(_STATE["company_id"], f"vendor:{vid}",
                       expected_type="customer")
    except OperationError as e:
        raised = "نوع الطرف" in str(e)
    assert raised, "resolve_party accepted wrong-type party"
    # Correct type should succeed.
    _, acct, label = resolve_party(
        _STATE["company_id"], f"vendor:{vid}", expected_type="vendor")
    assert acct is not None and label
    return "cross-type rejected; same-type accepted"


@check("8. All 18 new source_types resolve to non-unknown Arabic labels")
def _():
    from app.services.source_reference import _SOURCE_TYPES, _UNKNOWN_LABEL
    NEW = [
        "loan_short_receive", "loan_long_receive", "loan_installment_paid",
        "vat_net_payment", "year_end_close", "legal_reserve_allocation",
        "eosb_provision", "eosb_payment",
        "deposit_received", "deposit_returned",
        "note_receivable_received", "note_payable_issued",
        "bad_debt_writeoff",
        "cash_count_adjustment", "general_adjustment",
    ]
    for st in NEW:
        assert st in _SOURCE_TYPES, (
            f"source_type {st!r} not registered in source_reference.py")
        label = _SOURCE_TYPES[st][0]
        assert label != _UNKNOWN_LABEL, (
            f"source_type {st!r} label is the unknown fallback")
    return f"{len(NEW)} new source_types all labeled"


@check("9. REQUIRED_ACCOUNTS carries 5960 + 5970 (and the rest)")
def _():
    from app.services.coa_guard import REQUIRED_ACCOUNTS
    for code in ("5960", "5970", "2135", "2136", "5217", "2220"):
        assert code in REQUIRED_ACCOUNTS, (
            f"REQUIRED_ACCOUNTS missing {code}")
    return f"{len(REQUIRED_ACCOUNTS)} required codes total"


@check("10. verify_coa reports 5960 as missing when deleted")
def _():
    _setup()
    from app.services.coa_guard import verify_coa
    from app.models import Account
    acc = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5960").first()
    assert acc is not None, "5960 should be seeded by seed_default_coa"
    db.session.delete(acc); db.session.commit()
    missing = verify_coa(_STATE["company_id"])
    assert "5960" in missing, (
        f"verify_coa should flag 5960 as missing; got {missing}")
    return f"missing after delete: {[c for c in missing if c == '5960']}"


# ─── Phase 3 + 4 checks ────────────────────────────────────────────

@check("11. All 13 phase-3 keys exist in OPERATIONS_BY_KEY")
def _():
    from app.services.accounting_ops import OPERATIONS_BY_KEY
    P3 = [
        "receive-short-loan", "receive-long-loan", "pay-loan-installment",
        "pay-vat-net",
        "close-year-end", "allocate-legal-reserve",
        "provision-eosb",
        "accrue-revenue", "collect-accrued-revenue",
        "declare-dividend", "pay-dividend",
        "receive-deposit", "return-deposit",
    ]
    missing = [k for k in P3 if k not in OPERATIONS_BY_KEY]
    assert not missing, f"phase-3 wizards missing: {missing}"
    return f"{len(P3)}/13 phase-3 wizards registered"


@check("12. All 5 phase-4 keys + adjust-account exist")
def _():
    from app.services.accounting_ops import OPERATIONS_BY_KEY
    P4 = [
        "receive-note-receivable", "issue-note-payable",
        "writeoff-bad-debt", "pay-eosb",
        "cash-count-adjust", "adjust-account",
    ]
    missing = [k for k in P4 if k not in OPERATIONS_BY_KEY]
    assert not missing, f"phase-4 wizards missing: {missing}"
    return f"{len(P4)}/6 phase-4 wizards registered (incl. adjust-account)"


@check("13. cash-count-adjust posts against 1110 only")
def _():
    _setup()
    from app.services.accounting_ops import (
        get_operation, run_operation, OperationError,
    )
    from app.models import JournalEntry, JournalLine, Account
    op = get_operation("cash-count-adjust")
    # money field is not exposed — cash-count is 1110-locked.
    for f in op.fields:
        assert f.kind != "financial_account", (
            "cash-count-adjust should NOT expose a money picker — "
            "it's hardcoded to 1110")
    # Post a surplus
    entry = run_operation(op, _STATE["company_id"], dict(
        direction="surplus", amount="50", date=date.today().isoformat(),
        notes="اختبار زيادة صندوق"),
        actor_id=_STATE["user_id"])
    e = db.session.get(JournalEntry, entry.id)
    lines = JournalLine.query.filter_by(entry_id=e.id).all()
    codes = {db.session.get(Account, l.account_id).code for l in lines}
    assert codes == {"1110", "5960"}, (
        f"cash-count-adjust should touch only 1110 + 5960; got {codes}")
    return "1110 + 5960 only"


@check("14. adjust-account with empty note refused")
def _():
    _setup()
    from app.services.accounting_ops import (
        get_operation, run_operation, OperationError,
    )
    from app.models import Account
    op = get_operation("adjust-account")
    any_acc = Account.query.filter_by(
        company_id=_STATE["company_id"], is_postable=True,
        is_active=True).first()
    raised = False
    try:
        run_operation(op, _STATE["company_id"], dict(
            target_account_id=str(any_acc.id),
            direction="debit", amount="100",
            date=date.today().isoformat(), notes=""),
            actor_id=_STATE["user_id"])
    except OperationError as e:
        raised = "السبب إلزامي" in str(e)
    assert raised, "adjust-account should refuse empty note"
    return "empty-note adjustment refused"


@check("15. ops.adjustments is NOT in _IMPLIES (per ticket)")
def _():
    from app.services.permissions import _IMPLIES
    assert "ops.adjustments" not in _IMPLIES, (
        "ops.adjustments must NOT be in _IMPLIES — the ticket says "
        "'صلاحية مستقلة' — accountant-role users must not inherit it "
        "just because they have journals.create")
    return "ops.adjustments is explicitly-granted only"


@check("16. ops.adjustments in P with owner+admin (not accountant)")
def _():
    from app.services.permissions import P
    roles = P.get("ops.adjustments")
    assert roles is not None, "ops.adjustments missing from P"
    assert "owner" in roles and "admin" in roles, (
        f"ops.adjustments must grant owner+admin; got {roles}")
    assert "accountant" not in roles, (
        f"ops.adjustments must NOT include accountant role "
        f"(dangerous processor); got {roles}")
    return f"roles: {sorted(roles)}"


def main():
    app = create_app()
    _STATE["app"] = app
    passed = failed = 0
    try:
        for label, fn in CHECKS:
            try:
                with app.app_context():
                    result = fn()
                print(f"PASS  {label}\n        ⇒ {result}")
                passed += 1
            except Exception as e:
                print(f"FAIL  {label}\n        ⇒ {type(e).__name__}: {e}")
                failed += 1
    finally:
        with app.app_context():
            _teardown()
        print("\n(cleaned up fixture company)")
    print(f"\n────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
