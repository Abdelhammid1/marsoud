#!/usr/bin/env python3
"""MARSOUD-CUSTODY-BUGS-01 (2026-08-08) — custody creation +
employee-portal-home tiles.

Locks two fixes:

1. issue_custody() no longer requires account 1110 specifically —
   it walks the cash-account tree (same helper cash_flow uses) so a
   tenant whose seed pre-dated 1110, or whose super-admin renamed /
   re-parented cash accounts, can still issue custody.
2. The employee portal home (portal_emp/index.html) carries the
   four cross-portal tiles (custody, items, daily reports, files)
   so employees have an obvious entry point on landing.

Three checks:
  1. Delete 1110 + default PaymentMethod → issue_custody still
     posts, journal credits a surviving bank leaf
  2. Delete EVERY cash+bank leaf → issue_custody raises the
     widened error message
  3. portal_emp/index.html contains url_for() for all four tiles
"""
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__CB_"
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

    plan = Plan.query.filter_by(code="__cb__").first()
    if not plan:
        plan = Plan(code="__cb__", name="CB", name_ar="CB",
                    allowed_subitems=None)
        plan.set_modules([
            "accounting", "sales", "purchases", "reports", "agent",
            "inventory", "pos", "hr", "cash_custody",
        ])
        db.session.add(plan); db.session.flush()

    c = Company(name=f"{PREFIX}CO", base_currency="EGP",
                subdomain="cb",
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1),
                intended_plan_id=plan.id, plan_id=plan.id)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    u = User(email=f"{PREFIX}u@x.test", full_name="cb owner",
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
        conn.execute(text("DELETE FROM plans WHERE code = '__cb__'"))


def _delete_account_by_code(company_id, code):
    """Delete an account row cleanly — including any PaymentMethod
    that points at it (FK). Mirrors what a super-admin doing a COA
    reorganization would touch."""
    from app.models import Account, PaymentMethod
    acc = Account.query.filter_by(
        company_id=company_id, code=code).first()
    if not acc:
        return False
    # Null-out any PaymentMethod pointing at this account, or delete
    # the PM if it's the only account it references (nullable).
    PaymentMethod.query.filter_by(
        company_id=company_id, account_id=acc.id).delete()
    db.session.delete(acc)
    db.session.commit()
    return True


# ─── Checks ──────────────────────────────────────────────────────

@check("1. issue_custody works when 1110 is missing — fallback picks a bank leaf")
def _():
    from app.services.cash_custody import issue_custody
    from app.models import (CustodyHolderType, JournalEntry,
                            JournalLine, Account, PaymentMethod)
    _setup()
    cid = _STATE["company_id"]

    # Nuke 1110 and any PaymentMethod pointing at it. cash_accounts()
    # will now yield the bank leaves 1121-1125 only.
    PaymentMethod.query.filter_by(company_id=cid).delete()
    db.session.commit()
    assert _delete_account_by_code(cid, "1110"), \
        "seed_default_coa should have created 1110"

    custody = issue_custody(
        cid,
        holder_type=CustodyHolderType.EMPLOYEE,
        holder_id=_STATE["employee_id"],
        amount=1234, purpose="fallback test",
        actor_id=_STATE["user_id"])
    assert custody.journal_entry_id, "no journal id — fallback didn't fire"

    entry = db.session.get(JournalEntry, custody.journal_entry_id)
    lines = JournalLine.query.filter_by(entry_id=entry.id).all()
    # Two lines: Dr 1180-NNNNNN (holder sub), Cr <first surviving cash leaf>
    assert len(lines) == 2
    total_dr = sum(float(l.debit or 0) for l in lines)
    total_cr = sum(float(l.credit or 0) for l in lines)
    assert abs(total_dr - total_cr) < 0.01, "unbalanced"
    assert abs(total_dr - 1234) < 0.01, f"amount mismatch {total_dr}"

    # The credit leg should be against a bank leaf (1121-1125), not 1110.
    credit_line = next(l for l in lines if float(l.credit or 0) > 0)
    credit_account = db.session.get(Account, credit_line.account_id)
    assert credit_account.code.startswith("112"), (
        f"fallback picked {credit_account.code!r}; expected a bank leaf "
        f"(1121-1125) after 1110 was deleted")
    return f"posted vs {credit_account.code} «{credit_account.name_ar}»"


@check("2. issue_custody errors cleanly when no cash/bank account exists at all")
def _():
    from app.services.cash_custody import issue_custody, CustodyError
    from app.models import CustodyHolderType, PaymentMethod
    _setup()
    cid = _STATE["company_id"]

    # Nuke every payment method + every postable descendant of 1110/1120.
    PaymentMethod.query.filter_by(company_id=cid).delete()
    db.session.commit()
    from app.services.ledger import cash_accounts
    for acc in cash_accounts(cid, active_only=False):
        _delete_account_by_code(cid, acc.code)

    from app.services.ledger import cash_accounts as _re
    assert not _re(cid, active_only=False), \
        "test setup incomplete — cash accounts still present"

    try:
        issue_custody(
            cid,
            holder_type=CustodyHolderType.EMPLOYEE,
            holder_id=_STATE["employee_id"],
            amount=500, purpose="should fail",
            actor_id=_STATE["user_id"])
    except CustodyError as e:
        msg = str(e)
        assert "نقدية" in msg or "بنك" in msg, (
            f"error message should mention نقدية أو بنك; got: {msg}")
        assert "1110" in msg or "1120" in msg, (
            f"error message should reference the account roots so the "
            f"accountant knows where to seed; got: {msg}")
        return f"raised cleanly: {msg[:60]}…"
    raise AssertionError(
        "issue_custody did not raise CustodyError when no cash account "
        "existed at all")


@check("3. portal_emp/index.html carries all four cross-portal tiles")
def _():
    tmpl = (ROOT / "app" / "templates" / "portal_emp" / "index.html")\
        .read_text(encoding="utf-8")
    required_endpoints = [
        "portal_emp.custody_list",
        "portal_emp.items_list",
        "portal_emp.daily_reports_list",
        "user_files.index",
    ]
    missing = []
    for ep in required_endpoints:
        pat = re.compile(
            r"""url_for\(\s*['"]""" + re.escape(ep) + r"""['"]""")
        if not pat.search(tmpl):
            missing.append(ep)
    assert not missing, (
        f"portal_emp/index.html missing tile(s) for: {missing}\n"
        f"Every employee-portal top-level route needs a tile so "
        f"employees have an obvious entry point on landing.")
    # And the section header itself, so someone can't slip the
    # url_fors into an obscure corner without a visible section.
    assert "روابط سريعة" in tmpl, (
        "the 'روابط سريعة' section header is missing — the tiles "
        "should be inside a titled block so employees see them")
    return f"all {len(required_endpoints)} tiles + section header present"


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
