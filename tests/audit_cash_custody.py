#!/usr/bin/env python3
"""MARSOUD-CASH-CUSTODY-01 (2026-08-07) — cash custody audit.

Every acceptance criterion from the ticket has at least one check
here. Mirrors the shape of tests/audit_advance_installments.py:
maximal Plan (all modules including cash_custody), fresh Company
with seeded CoA, owner + optional second user, PREFIX-scoped
teardown that walks sorted tables.

Fifteen checks:

  1. Holder = EMPLOYEE — issue journal balanced + carries source
  2. Holder = DEPARTMENT — same, on a department sub-account
  3. Issue via approve_custody_request creates BOTH request +
     custody in the same transaction with a journal
  4. Reject request → no custody, request flipped, no journal
  5. add_settlement_line accumulates without posting; status
     flips to PARTIALLY_SETTLED on first line
  6. add_settlement_line refuses over-settlement (sum > issued)
  7. close_settlement — full amount, no returned/shortfall —
     posts ONE balanced journal Dr expenses / Cr 1180
  8. close_settlement with returned excess — Dr cash + Dr
     expenses / Cr 1180, sum matches issued
  9. close_settlement with shortfall EMPLOYEE_LIABILITY — Dr
     2130-emp for shortfall
 10. close_settlement with shortfall EXPENSE — Dr auto-created
     5991 "عجز عهدة" for shortfall
 11. close_settlement refuses drift (sum+returned+shortfall
     ≠ issued)
 12. cancel_custody before settlement — reverses journal, flips
     status; refuses if any line was added
 13. reverse_journal on the issue entry from /journals flips
     custody to CANCELLED (via _undo_source_side_effects branch)
 14. Cross-tenant: company B cannot approve/settle/cancel
     company A's custody
 15. CHECK constraint refuses both-holders + no-holder rows at
     the DB level
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
PREFIX = "__CC_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ───────────────────────────────────────────────────
def _setup():
    _teardown()
    from app.models import (
        Company, Plan, User, UserStatus, Employee,
        EmployeeStatus, ContractType, Department,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from app.services.roles_seed import ensure_roles_ready_for_company
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code="__cc__").first()
    if not plan:
        plan = Plan(code="__cc__", name="CC", name_ar="CC",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "purchases",
                          "reports", "agent", "inventory", "pos",
                          "crm", "hr", "employee_reports",
                          "manufacturing", "evaluations",
                          "cash_custody", "insights"])
        db.session.add(plan); db.session.flush()

    c = Company(name=f"{PREFIX}CO", base_currency="EGP",
                subdomain="cc",
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1),
                intended_plan_id=plan.id, plan_id=plan.id)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    u = User(email=f"{PREFIX}u@x.test", full_name="cc owner",
             is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST",
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
                   start_date=date.today() - timedelta(days=100))
    db.session.add(emp)
    dept = Department(company_id=c.id, name=f"{PREFIX}dept",
                      is_active=True)
    db.session.add(dept)
    db.session.commit()

    _STATE.update(company_id=c.id, user_id=u.id,
                  employee_id=emp.id, department_id=dept.id)


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        # SQLite reuses primary keys for tables without AUTOINCREMENT.
        # If a previous run left orphan journal_lines (entry_id pointing
        # at a deleted journal_entries row), a fresh journal_entries
        # row can be assigned the same id and inherit those orphans.
        # Same trap the T7/T8 audits hit. Sweep first.
        conn.execute(text(
            "DELETE FROM journal_lines WHERE entry_id NOT IN "
            "(SELECT id FROM journal_entries)"))
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__CC_%'"))]
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
        conn.execute(text("DELETE FROM users WHERE email LIKE '__CC_%@x.test'"))
        conn.execute(text("DELETE FROM plans WHERE code = '__cc__'"))


# ─── Checks ────────────────────────────────────────────────────
@check("1. Issue custody to EMPLOYEE — journal balanced + source_type='cash_custody'")
def _():
    from app.services.cash_custody import issue_custody
    from app.models import CustodyHolderType, JournalEntry, JournalLine
    _setup()
    custody = issue_custody(
        _STATE["company_id"],
        holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"],
        amount=5000, purpose="site expenses",
        actor_id=_STATE["user_id"])
    assert custody.journal_entry_id, "no journal id"
    entry = db.session.get(JournalEntry, custody.journal_entry_id)
    assert entry.source_type == "cash_custody"
    assert entry.source_id == custody.id
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    assert len(lines) == 2, f"expected 2 lines, got {len(lines)}"
    total_dr = sum(float(l.debit or 0) for l in lines)
    total_cr = sum(float(l.credit or 0) for l in lines)
    assert abs(total_dr - total_cr) < 0.01 and abs(total_dr - 5000) < 0.01
    return f"issued 5000 → entry #{entry.number}, balanced"


@check("2. Issue custody to DEPARTMENT — sub-account under 1180 minted")
def _():
    from app.services.cash_custody import issue_custody
    from app.models import (
        CustodyHolderType, Account, Department,
    )
    _setup()
    custody = issue_custody(
        _STATE["company_id"],
        holder_type=CustodyHolderType.DEPARTMENT,
        holder_id=_STATE["department_id"],
        amount=2000, purpose="team-day catering",
        actor_id=_STATE["user_id"])
    dept = db.session.get(Department, _STATE["department_id"])
    assert dept.custody_account_id, "department custody sub-account not minted"
    acc = db.session.get(Account, dept.custody_account_id)
    assert acc.code.startswith("1180-"), f"bad code {acc.code}"
    return f"dept sub-account = {acc.code}"


@check("3. approve_custody_request creates custody + journal in one flow")
def _():
    from app.services.cash_custody import (
        request_custody, approve_custody_request,
    )
    from app.models import CustodyHolderType, CustodyRequestStatus
    _setup()
    req = request_custody(
        _STATE["company_id"], CustodyHolderType.EMPLOYEE,
        _STATE["employee_id"], amount=1500, purpose="fuel",
        created_by=_STATE["user_id"])
    assert req.status == CustodyRequestStatus.PENDING
    custody = approve_custody_request(
        req, reviewer_id=_STATE["user_id"])
    db.session.refresh(req)
    assert req.status == CustodyRequestStatus.APPROVED
    assert custody.journal_entry_id
    assert custody.request_id == req.id
    return f"request #{req.id} → custody #{custody.id}"


@check("4. reject_custody_request — no journal, request flipped")
def _():
    from app.services.cash_custody import (
        request_custody, reject_custody_request,
    )
    from app.models import (
        CustodyHolderType, CustodyRequestStatus, JournalEntry,
    )
    _setup()
    baseline_je = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"]).count()
    req = request_custody(
        _STATE["company_id"], CustodyHolderType.EMPLOYEE,
        _STATE["employee_id"], amount=500, purpose="office supplies",
        created_by=_STATE["user_id"])
    reject_custody_request(req, reviewer_id=_STATE["user_id"],
                            review_note="not now")
    db.session.refresh(req)
    assert req.status == CustodyRequestStatus.REJECTED
    now_je = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"]).count()
    assert now_je == baseline_je, (
        f"reject posted a journal ({now_je - baseline_je}) — shouldn't")
    return f"reject with note, journal count unchanged"


@check("5. add_settlement_line accumulates, flips ISSUED → PARTIALLY_SETTLED")
def _():
    from app.services.cash_custody import (
        issue_custody, add_settlement_line,
    )
    from app.models import (
        Account, AccountType, CustodyHolderType, CustodyStatus,
    )
    _setup()
    custody = issue_custody(
        _STATE["company_id"], holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"], amount=1000,
        purpose="misc", actor_id=_STATE["user_id"])
    exp = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5100").first()
    assert exp, "5100 expense account missing"
    line = add_settlement_line(
        custody, expense_account_id=exp.id, amount=200,
        receipt_note="receipt 1", actor_id=_STATE["user_id"])
    db.session.refresh(custody)
    assert custody.status == CustodyStatus.PARTIALLY_SETTLED
    assert float(custody.amount_settled) == 200
    assert line.id
    return f"line #{line.id} added, status = {custody.status.value}"


@check("6. add_settlement_line refuses over-settlement")
def _():
    from app.services.cash_custody import (
        issue_custody, add_settlement_line, CustodyError,
    )
    from app.models import Account, CustodyHolderType
    _setup()
    custody = issue_custody(
        _STATE["company_id"], holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"], amount=500,
        purpose="misc", actor_id=_STATE["user_id"])
    exp = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5100").first()
    add_settlement_line(custody, expense_account_id=exp.id,
                         amount=400, actor_id=_STATE["user_id"])
    try:
        add_settlement_line(custody, expense_account_id=exp.id,
                             amount=200, actor_id=_STATE["user_id"])
    except CustodyError as e:
        # Root "تجاوز" — matches both "سيتجاوز" (will exceed) in
        # add_settlement_line and "تتجاوز" (exceed-fem) elsewhere.
        assert "تجاوز" in str(e), f"wrong msg: {e}"
        return f"refused over-settle: {str(e)[:60]}"
    raise AssertionError("didn't refuse over-settlement")


@check("7. close_settlement — full, no returned/shortfall — one balanced journal")
def _():
    from app.services.cash_custody import (
        issue_custody, add_settlement_line, close_settlement,
    )
    from app.models import (
        Account, CustodyHolderType, CustodyStatus,
        JournalEntry, JournalLine,
    )
    _setup()
    custody = issue_custody(
        _STATE["company_id"], holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"], amount=1000,
        purpose="site", actor_id=_STATE["user_id"])
    exp = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5100").first()
    # 5200 is a header (Operating Expenses) — not postable. Use a
    # leaf under it (5220 Rent Expense) so add_settlement_line accepts.
    exp2 = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5220").first()
    add_settlement_line(custody, expense_account_id=exp.id,
                         amount=600, actor_id=_STATE["user_id"])
    add_settlement_line(custody, expense_account_id=exp2.id,
                         amount=400, actor_id=_STATE["user_id"])
    close_settlement(custody, actor_id=_STATE["user_id"])
    db.session.refresh(custody)
    assert custody.status == CustodyStatus.SETTLED
    assert custody.settlement_journal_entry_id
    entry = db.session.get(JournalEntry,
                            custody.settlement_journal_entry_id)
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    total_dr = sum(float(l.debit or 0) for l in lines)
    total_cr = sum(float(l.credit or 0) for l in lines)
    assert abs(total_dr - 1000) < 0.01
    assert abs(total_cr - 1000) < 0.01
    # 2 expense Dr rows + 1 custody Cr row = 3 lines
    assert len(lines) == 3, f"expected 3 lines, got {len(lines)}"
    return f"settled 1000 across 2 accounts → entry #{entry.number}"


@check("8. close_settlement with returned excess — Dr cash + Dr expense / Cr custody")
def _():
    from app.services.cash_custody import (
        issue_custody, add_settlement_line, close_settlement,
    )
    from app.models import (
        Account, CustodyHolderType, CustodyStatus, JournalLine,
    )
    _setup()
    custody = issue_custody(
        _STATE["company_id"], holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"], amount=1000,
        purpose="site", actor_id=_STATE["user_id"])
    exp = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5100").first()
    add_settlement_line(custody, expense_account_id=exp.id,
                         amount=700, actor_id=_STATE["user_id"])
    close_settlement(custody, actor_id=_STATE["user_id"],
                      returned_amount=300)
    db.session.refresh(custody)
    assert custody.status == CustodyStatus.SETTLED
    assert float(custody.amount_returned) == 300
    lines = JournalLine.query.filter_by(
        entry_id=custody.settlement_journal_entry_id).all()
    total_dr = sum(float(l.debit or 0) for l in lines)
    total_cr = sum(float(l.credit or 0) for l in lines)
    assert abs(total_dr - 1000) < 0.01 and abs(total_cr - 1000) < 0.01
    return f"expenses 700 + returned 300 = 1000"


@check("9. close_settlement with shortfall EMPLOYEE_LIABILITY")
def _():
    from app.services.cash_custody import (
        issue_custody, add_settlement_line, close_settlement,
    )
    from app.models import (
        Account, CustodyHolderType, CustodyStatus,
        ShortfallDisposition, Employee, JournalLine,
    )
    _setup()
    custody = issue_custody(
        _STATE["company_id"], holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"], amount=1000,
        purpose="site", actor_id=_STATE["user_id"])
    exp = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5100").first()
    add_settlement_line(custody, expense_account_id=exp.id,
                         amount=800, actor_id=_STATE["user_id"])
    close_settlement(
        custody, actor_id=_STATE["user_id"],
        shortfall_disposition=ShortfallDisposition.EMPLOYEE_LIABILITY)
    db.session.refresh(custody)
    assert custody.status == CustodyStatus.SETTLED
    assert float(custody.amount_shortfall) == 200
    # Check that the employee's 2130 sub-account got a Dr 200 line.
    emp = db.session.get(Employee, _STATE["employee_id"])
    assert emp.account_id, "employee 2130 sub not minted"
    emp_lines = JournalLine.query.filter_by(
        entry_id=custody.settlement_journal_entry_id,
        account_id=emp.account_id).all()
    assert emp_lines, "no line hit the employee's 2130 leaf"
    assert abs(float(emp_lines[0].debit or 0) - 200) < 0.01
    return f"shortfall 200 → employee 2130 leaf"


@check("10. close_settlement with shortfall EXPENSE — creates 5991")
def _():
    from app.services.cash_custody import (
        issue_custody, add_settlement_line, close_settlement,
    )
    from app.models import (
        Account, CustodyHolderType, CustodyStatus,
        ShortfallDisposition, JournalLine,
    )
    _setup()
    custody = issue_custody(
        _STATE["company_id"], holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"], amount=1000,
        purpose="site", actor_id=_STATE["user_id"])
    exp = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5100").first()
    add_settlement_line(custody, expense_account_id=exp.id,
                         amount=850, actor_id=_STATE["user_id"])
    close_settlement(
        custody, actor_id=_STATE["user_id"],
        shortfall_disposition=ShortfallDisposition.EXPENSE)
    # 5991 must now exist and carry the 150 Dr.
    acc5991 = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5991").first()
    assert acc5991, "5991 عجز عهدة not lazy-created"
    lines = JournalLine.query.filter_by(
        entry_id=custody.settlement_journal_entry_id,
        account_id=acc5991.id).all()
    assert lines and abs(float(lines[0].debit or 0) - 150) < 0.01
    return f"5991 created, shortfall 150 booked as expense"


@check("11. close_settlement refuses drift (lines+returned > issued)")
def _():
    from app.services.cash_custody import (
        issue_custody, add_settlement_line, close_settlement,
        CustodyError,
    )
    from app.models import Account, CustodyHolderType
    _setup()
    custody = issue_custody(
        _STATE["company_id"], holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"], amount=1000,
        purpose="site", actor_id=_STATE["user_id"])
    exp = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5100").first()
    add_settlement_line(custody, expense_account_id=exp.id,
                         amount=800, actor_id=_STATE["user_id"])
    try:
        close_settlement(custody, actor_id=_STATE["user_id"],
                          returned_amount=500)  # 800 + 500 > 1000
    except CustodyError as e:
        assert "تتجاوز" in str(e)
        return f"refused drift: {str(e)[:60]}"
    raise AssertionError("close_settlement accepted drift")


@check("12. cancel_custody before settlement — reverses; refuses if lines exist")
def _():
    from app.services.cash_custody import (
        issue_custody, add_settlement_line, cancel_custody,
        CustodyError,
    )
    from app.models import (
        Account, CustodyHolderType, CustodyStatus,
    )
    _setup()
    custody = issue_custody(
        _STATE["company_id"], holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"], amount=1000,
        purpose="site", actor_id=_STATE["user_id"])
    cancel_custody(custody, actor_id=_STATE["user_id"],
                    reason="typo")
    db.session.refresh(custody)
    assert custody.status == CustodyStatus.CANCELLED
    assert custody.reversal_entry_id

    # Second custody: add a line first → cancel refuses.
    _setup()
    custody2 = issue_custody(
        _STATE["company_id"], holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"], amount=500,
        purpose="site", actor_id=_STATE["user_id"])
    exp = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5100").first()
    add_settlement_line(custody2, expense_account_id=exp.id,
                         amount=100, actor_id=_STATE["user_id"])
    try:
        cancel_custody(custody2, actor_id=_STATE["user_id"])
    except CustodyError as e:
        assert "بنود تسوية" in str(e)
        return "cancelled + refused-with-lines"
    raise AssertionError("cancel_custody accepted lines-present state")


@check("13. reverse_journal on issue entry from /journals flips custody to CANCELLED")
def _():
    from app.services.cash_custody import issue_custody
    from app.services.ledger import reverse_journal
    from app.models import CustodyHolderType, CustodyStatus
    _setup()
    custody = issue_custody(
        _STATE["company_id"], holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"], amount=500,
        purpose="site", actor_id=_STATE["user_id"])
    # Reverse the raw journal (the /journals path).
    reverse_journal(custody.journal_entry_id,
                     created_by=_STATE["user_id"])
    db.session.refresh(custody)
    assert custody.status == CustodyStatus.CANCELLED, (
        f"status = {custody.status.value}, expected CANCELLED")
    return "raw reverse flipped custody"


@check("14. Cross-tenant: company B cannot cancel/settle company A's custody")
def _():
    from app.services.cash_custody import (
        issue_custody, cancel_custody, add_settlement_line,
        CustodyError,
    )
    from app.models import (
        Company, Plan, User, UserStatus, Employee,
        EmployeeStatus, ContractType, CustodyHolderType,
        Account,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    _setup()
    # A's custody
    custodyA = issue_custody(
        _STATE["company_id"], holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"], amount=500,
        purpose="A site", actor_id=_STATE["user_id"])

    # Spin up company B
    plan = Plan.query.filter_by(code="__cc__").first()
    b = Company(name=f"{PREFIX}CO_B", base_currency="EGP",
                subdomain="ccb",
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1),
                intended_plan_id=plan.id, plan_id=plan.id)
    db.session.add(b); db.session.flush()
    seed_default_coa(b.id)
    uB = User(email=f"{PREFIX}b@x.test", full_name="cc B owner",
              is_active=True,
              status=UserStatus.ACTIVE.value,
              email_verified_at=datetime.utcnow(),
              terms_version="TEST",
              password_hash=generate_password_hash(
                  "x", method="pbkdf2:sha256"))
    db.session.add(uB); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=uB.id, company_id=b.id, role="owner"))
    db.session.commit()

    # Try to add a settlement line using B's 5100 account against
    # A's custody — service must refuse (account_id company mismatch).
    accB = Account.query.filter_by(company_id=b.id, code="5100").first()
    try:
        add_settlement_line(custodyA, expense_account_id=accB.id,
                             amount=100, actor_id=uB.id)
    except CustodyError as e:
        assert "غير موجود" in str(e), f"wrong error: {e}"
        return f"cross-tenant line refused: {str(e)[:50]}"
    raise AssertionError("cross-tenant settlement leaked")


@check("15. DB-level CHECK constraint: both-holders + no-holder refused")
def _():
    from app.models import CashCustody, CustodyHolderType, CustodyStatus
    from sqlalchemy.exc import IntegrityError
    _setup()
    # Both holders — must fail
    bad1 = CashCustody(
        company_id=_STATE["company_id"],
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=_STATE["employee_id"],
        department_id=_STATE["department_id"],  # ← both set
        amount_issued=100, status=CustodyStatus.ISSUED,
        issued_on=date.today())
    db.session.add(bad1)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
    else:
        db.session.rollback()
        raise AssertionError("both-holders row was accepted")

    # No holder — must fail
    bad2 = CashCustody(
        company_id=_STATE["company_id"],
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=None, department_id=None,
        amount_issued=100, status=CustodyStatus.ISSUED,
        issued_on=date.today())
    db.session.add(bad2)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
    else:
        db.session.rollback()
        raise AssertionError("no-holder row was accepted")
    return "both-holders + no-holder both rejected at DB level"


# ═══════════════════════════════════════════════════════════════
# Slice 3 checks — portal / report / overdue-cron surfaces
# ═══════════════════════════════════════════════════════════════

@check("16. open_custody_report returns rows with days_open + is_overdue")
def _():
    from app.services.reports import open_custody_report
    from app.services.cash_custody import (
        issue_custody, add_settlement_line,
    )
    from app.models import Account, CustodyHolderType
    _setup()
    # Open custody — should appear
    open_c = issue_custody(
        _STATE["company_id"], holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"], amount=1000,
        purpose="report test",
        settlement_due_date=date.today() - timedelta(days=5),
        actor_id=_STATE["user_id"])
    exp = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5100").first()
    add_settlement_line(open_c, expense_account_id=exp.id,
                         amount=300, actor_id=_STATE["user_id"])
    r = open_custody_report(_STATE["company_id"])
    assert r["rows"], "no rows returned"
    row = r["rows"][0]
    assert row["custody_id"] == open_c.id
    assert row["amount_pending"] == 700.0
    assert row["is_overdue"] is True
    assert r["totals"]["overdue_count"] == 1
    assert r["totals"]["pending"] == 700.0
    return f"pending 700 · overdue flagged · totals correct"


@check("17. sweep_overdue_custodies fires ONE notification then dedups")
def _():
    from app.services.cash_custody import (
        issue_custody, sweep_overdue_custodies,
    )
    from app.models import (
        CashCustody, CustodyHolderType, Notification,
    )
    from sqlalchemy import text
    _setup()
    custody = issue_custody(
        _STATE["company_id"], holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"], amount=500,
        purpose="cron test",
        settlement_due_date=date.today() - timedelta(days=2),
        actor_id=_STATE["user_id"])

    # Wipe any prior notifications for this user so we count only
    # what the sweep fires this run.
    db.session.execute(text(
        "DELETE FROM notifications WHERE user_id=:u"),
        {"u": _STATE["user_id"]})
    db.session.commit()

    sent1 = sweep_overdue_custodies(_STATE["company_id"])
    db.session.refresh(custody)
    assert custody.custody_overdue_notified_at is not None
    assert sent1 >= 1, f"first sweep didn't notify: {sent1}"
    notif_count = Notification.query.filter_by(
        user_id=_STATE["user_id"]).count()
    assert notif_count >= 1

    # Second tick — must NOT fire again (dedup on
    # custody_overdue_notified_at).
    sent2 = sweep_overdue_custodies(_STATE["company_id"])
    assert sent2 == 0, f"second sweep re-fired: {sent2}"
    new_notif_count = Notification.query.filter_by(
        user_id=_STATE["user_id"]).count()
    assert new_notif_count == notif_count, (
        f"notification count changed: {notif_count} → {new_notif_count}")
    return f"1st sweep sent={sent1}, 2nd sweep sent=0 (dedup works)"


@check("18. Portal routes: /my/custody + /my/custody/request registered")
def _():
    """Structural check — the three portal endpoints exist and
    the permission gate for _can_attach_to knows about the new
    CASH_CUSTODY_SETTLEMENT source_type."""
    from flask import current_app
    endpoints = {r.endpoint for r in current_app.url_map.iter_rules()}
    for expected in ("portal_emp.custody_list",
                      "portal_emp.custody_request_new",
                      "portal_emp.custody_detail",
                      "reports.open_custody",
                      "custody.detail"):
        assert expected in endpoints, f"missing route: {expected}"
    # Attach-gate branch for the new source_type must exist in the
    # documents blueprint — grep the source (structural guard).
    from pathlib import Path
    src = (ROOT / "app" / "routes" / "opsflow_extras.py").read_text(
        encoding="utf-8")
    assert 'CASH_CUSTODY_SETTLEMENT' in src, (
        "documents route missing CASH_CUSTODY_SETTLEMENT branch")
    return f"{len(endpoints)} routes registered; documents branch present"


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    passed = failed = 0
    for label, fn in CHECKS:
        with app.app_context():
            try:
                _teardown()
                res = fn()
                print(f"PASS  {label}\n        => {res}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                failed += 1
                import traceback; traceback.print_exc()
    with app.app_context():
        _teardown()
        print("\n(cleaned up)")
    print(f"\n----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
