#!/usr/bin/env python3
"""MARSOUD-CUSTODY-BUGS-02 (2026-08-08) — 1180 header backfill +
service-layer lazy-create for known party headers.

Sister audit to audit_custody_bugs.py (BUGS-01, already merged):
- BUGS-01 locked the LEAF-side fault-tolerance (cascading fallback
  when 1110 is missing) in cash_custody.py
- This one locks the HEADER-side fault-tolerance (lazy-create for
  the four documented party headers) in subsidiary.py

Four checks:
  1. create_party_subaccount('1180', …) on a company with 1180
     deleted → header re-minted + leaf created
  2. Full issue_custody end-to-end works when 1180 is missing
  3. The re-minted header is byte-identical to the seeded version
     (is_postable=False, parent 1100, ASSET, DEBIT normal side)
  4. Unknown parent codes still raise — lazy-create is scoped,
     not a blanket auto-heal
"""
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__CB2_"
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
        EmployeeStatus, ContractType,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from app.services.roles_seed import ensure_roles_ready_for_company
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code="__cb2__").first()
    if not plan:
        plan = Plan(code="__cb2__", name="CB2", name_ar="CB2",
                    allowed_subitems=None)
        plan.set_modules([
            "accounting", "sales", "purchases", "reports", "agent",
            "inventory", "pos", "hr", "cash_custody",
        ])
        db.session.add(plan); db.session.flush()

    c = Company(name=f"{PREFIX}CO", base_currency="EGP",
                subdomain="cb2",
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1),
                intended_plan_id=plan.id, plan_id=plan.id)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    u = User(email=f"{PREFIX}u@x.test", full_name="cb2 owner",
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
    db.session.add(emp); db.session.commit()
    _STATE.update(company_id=c.id, user_id=u.id, employee_id=emp.id)


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM journal_lines WHERE entry_id NOT IN "
            "(SELECT id FROM journal_entries)"))
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
        conn.execute(text("DELETE FROM plans WHERE code = '__cb2__'"))


def _delete_1180_and_its_leaves(company_id):
    """Simulate a legacy tenant whose seed pre-dated the cash-custody
    feature — no 1180 header and no children under it. Returns
    the count of rows we swept."""
    from app.models import Account
    swept = 0
    # Delete children first (any 1180-* leaves, though on a fresh
    # fixture there shouldn't be any yet).
    children = Account.query.filter(
        Account.company_id == company_id,
        Account.code.like("1180-%"),
    ).all()
    for c in children:
        db.session.delete(c); swept += 1
    header = Account.query.filter_by(
        company_id=company_id, code="1180").first()
    if header is not None:
        db.session.delete(header); swept += 1
    db.session.commit()
    return swept


# ─── Checks ──────────────────────────────────────────────────────

@check("1. create_party_subaccount lazy-mints 1180 when it's missing")
def _():
    from app.services.subsidiary import create_party_subaccount
    from app.models import Account
    _setup()
    cid = _STATE["company_id"]
    swept = _delete_1180_and_its_leaves(cid)
    assert swept >= 1, "test setup didn't delete anything"
    # No 1180 in DB — expect lazy-create to mint it.
    leaf = create_party_subaccount(cid, "1180", "test-employee")
    assert leaf is not None
    header = Account.query.filter_by(company_id=cid, code="1180").first()
    assert header is not None, "1180 header should have been lazy-created"
    assert leaf.parent_id == header.id
    assert leaf.code.startswith("1180-")
    assert leaf.is_postable is True
    return f"header re-minted + leaf {leaf.code}"


@check("2. Full issue_custody works end-to-end when 1180 is missing")
def _():
    from app.services.cash_custody import issue_custody
    from app.models import (CustodyHolderType, JournalEntry,
                            JournalLine, Account)
    _setup()
    cid = _STATE["company_id"]
    _delete_1180_and_its_leaves(cid)

    custody = issue_custody(
        cid,
        holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"],
        amount=1000, purpose="header lazy-create test",
        actor_id=_STATE["user_id"])
    assert custody.journal_entry_id, (
        "no journal id — lazy-create should have unblocked issue_custody")

    entry = db.session.get(JournalEntry, custody.journal_entry_id)
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    assert len(lines) == 2
    dr_line = next(l for l in lines if float(l.debit or 0) > 0)
    dr_acc = db.session.get(Account, dr_line.account_id)
    assert dr_acc.code.startswith("1180-"), (
        f"custody debit should hit a 1180-N leaf; got {dr_acc.code}")
    # Journal balanced
    total_dr = sum(float(l.debit or 0) for l in lines)
    total_cr = sum(float(l.credit or 0) for l in lines)
    assert abs(total_dr - total_cr) < 0.01
    return f"custody posted, debited {dr_acc.code}"


@check("3. Lazy-created 1180 is byte-identical to seeded 1180")
def _():
    from app.services.subsidiary import create_party_subaccount
    from app.models import Account, AccountType
    _setup()
    cid = _STATE["company_id"]
    _delete_1180_and_its_leaves(cid)
    create_party_subaccount(cid, "1180", "test-emp-2")
    header = Account.query.filter_by(company_id=cid, code="1180").first()
    assert header is not None
    # Match the seed_coa.py:52-53 tuple exactly.
    assert header.name == "Cash Custody in Settlement", (
        f"name != 'Cash Custody in Settlement': {header.name!r}")
    assert header.name_ar == "عهد نقدية تحت التسوية", (
        f"name_ar mismatch: {header.name_ar!r}")
    assert header.type == AccountType.ASSET
    assert header.is_postable is False, (
        "1180 must be a HEADER — leaves are minted under it")
    assert header.normal_side.value == "DEBIT"
    parent = db.session.get(Account, header.parent_id)
    assert parent is not None and parent.code == "1100", (
        f"parent should be 1100; got {parent.code if parent else None}")
    return "byte-identical to seeded header"


@check("4. Unknown parent code STILL raises — lazy-create is scoped")
def _():
    from app.services.subsidiary import create_party_subaccount
    _setup()
    cid = _STATE["company_id"]
    # 9999 is not in _KNOWN_PARTY_HEADERS; must NOT be auto-minted.
    raised = False
    try:
        create_party_subaccount(cid, "9999", "test-typo")
    except ValueError as e:
        msg = str(e)
        raised = ("9999" in msg and "غير موجود" in msg)
    assert raised, (
        "create_party_subaccount should still raise for unknown parent "
        "codes — the lazy-create must not mask real bugs")
    return "unknown code refused (typo protection preserved)"


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
