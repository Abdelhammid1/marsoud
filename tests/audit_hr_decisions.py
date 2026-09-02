#!/usr/bin/env python3
"""MARSOUD-TKT-HR-DECISIONS-01 (2026-09-02) — HR Decisions Phase 1.

Persistent decision-document layer per employee (promotion / transfer /
warning / penalty / bonus / termination), with immediate-JE or
next-payroll timing for financial decisions, and delegate-to-existing
`terminate_employee` for terminations.

Checks:
  1. Blueprint registered with all expected endpoints.
  2. hr_decisions table exists (migration applied).
  3. ADMIN (PROMOTION) create + execute → EXECUTED, no JE.
  4. FINANCIAL BONUS IMMEDIATE → posts one JE tagged hr_decision,
     journal_entry_id set, status EXECUTED.
  5. FINANCIAL BONUS NEXT_PAYROLL → PENDING_PAYROLL, no JE.
  6. Cancel DRAFT with reason → CANCELLED.
  7. Cancel without reason → refused.
  8. Cancel EXECUTED → refused ("قرار عكسي" message).
  9. TERMINATION execute → Employee status=TERMINATED,
     termination_date set, is_active=False.
 10. Company isolation — B's employee not visible via A's session.
 11. Blank body for PENALTY → refused.
 12. Feature registry: hr_decisions_index under module=hr.
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
    db.session.execute(text(
        "DELETE FROM journal_lines WHERE account_id NOT IN (SELECT id FROM accounts)"))
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
    try:
        ensure_employee_account(emp)
    except Exception:
        pass
    db.session.commit()
    return emp


def _prime_cash(cid, amount=5000):
    from app import db
    from app.models import Account
    from app.services.ledger import post_journal
    cash = Account.query.filter_by(company_id=cid, code="1110").first()
    cap = (Account.query.filter_by(company_id=cid, code="3110").first()
            or Account.query.filter_by(company_id=cid, code="3100").first())
    post_journal(company_id=cid, description="opening",
                 lines=[{"account_id": cash.id, "debit": amount, "credit": 0},
                        {"account_id": cap.id, "debit": 0, "credit": amount}],
                 entry_date=date.today())


@check("1. blueprint registered")
def _():
    from app import create_app
    app = create_app()
    names = {r.endpoint for r in app.url_map.iter_rules()}
    for want in ("hr_decisions.index", "hr_decisions.new",
                 "hr_decisions.create", "hr_decisions.detail",
                 "hr_decisions.execute", "hr_decisions.cancel"):
        assert want in names, f"missing endpoint: {want}"
    return "6 endpoints registered"


@check("2. hr_decisions table exists")
def _():
    from app import create_app
    from sqlalchemy import inspect
    app = create_app()
    with app.app_context():
        from app import db
        insp = inspect(db.engine)
        assert "hr_decisions" in insp.get_table_names(), \
            "hr_decisions table not created — did the migration run?"
        cols = {c["name"] for c in insp.get_columns("hr_decisions")}
        for want in ("kind", "status", "timing", "amount",
                     "payment_account_id", "journal_entry_id",
                     "payroll_run_id", "cancel_reason"):
            assert want in cols, f"column missing: {want}"
        return f"{len(cols)} columns present"


@check("3. ADMIN (PROMOTION) create + execute → EXECUTED, no JE")
def _():
    from app import create_app, db
    from app.models.journal import JournalEntry
    from app.services.hr_decisions import create_decision, execute_decision

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HR3")
        try:
            emp = _make_employee(cid, "علي")
            before = JournalEntry.query.filter_by(company_id=cid).count()
            dec = create_decision(cid,
                employee_id=emp.id, kind="PROMOTION",
                effective_date=date.today(),
                title="ترقية إلى محاسب أول",
                body="أداء ممتاز",
                actor_id=oid)
            assert dec.status == "DRAFT"
            execute_decision(dec, actor_id=oid)
            assert dec.status == "EXECUTED"
            after = JournalEntry.query.filter_by(company_id=cid).count()
            assert after == before, \
                f"ADMIN decision must not post JE — before={before} after={after}"
            return "admin → EXECUTED with 0 JE"
        finally:
            pass


@check("4. BONUS IMMEDIATE → single JE posted, tagged hr_decision")
def _():
    from app import create_app, db
    from app.models import Account
    from app.models.journal import JournalEntry
    from app.services.hr_decisions import create_decision, execute_decision

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HR4")
        try:
            _prime_cash(cid, amount=5000)
            emp = _make_employee(cid, "أحمد")
            cash = Account.query.filter_by(company_id=cid, code="1110").first()
            before_cash = float(cash.balance or 0)
            dec = create_decision(cid,
                employee_id=emp.id, kind="BONUS",
                effective_date=date.today(),
                title="مكافأة إنجاز مشروع",
                body="مشروع الإطلاق",
                timing="IMMEDIATE",
                amount=500,
                payment_account_id=cash.id,
                actor_id=oid)
            execute_decision(dec, actor_id=oid)
            assert dec.status == "EXECUTED"
            assert dec.journal_entry_id is not None
            je = db.session.get(JournalEntry, dec.journal_entry_id)
            assert je.source_type == "hr_decision"
            assert je.source_id == dec.id
            db.session.refresh(cash)
            after_cash = float(cash.balance or 0)
            assert abs((before_cash - after_cash) - 500) < 0.01, \
                f"cash should decrease by 500 (was {before_cash}, now {after_cash})"
            return f"1 JE #{je.id}, cash 5000 → {after_cash}"
        finally:
            pass


@check("5. BONUS NEXT_PAYROLL → PENDING_PAYROLL, no JE")
def _():
    from app import create_app, db
    from app.models import Account
    from app.models.journal import JournalEntry
    from app.services.hr_decisions import create_decision, execute_decision

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HR5")
        try:
            emp = _make_employee(cid)
            cash = Account.query.filter_by(company_id=cid, code="1110").first()
            before = JournalEntry.query.filter_by(company_id=cid).count()
            dec = create_decision(cid,
                employee_id=emp.id, kind="BONUS",
                effective_date=date.today(),
                title="مكافأة شهرية",
                timing="NEXT_PAYROLL",
                amount=200,
                actor_id=oid)
            execute_decision(dec, actor_id=oid)
            assert dec.status == "PENDING_PAYROLL", \
                f"expected PENDING_PAYROLL, got {dec.status}"
            assert dec.journal_entry_id is None
            after = JournalEntry.query.filter_by(company_id=cid).count()
            assert after == before, \
                f"NEXT_PAYROLL must NOT post JE — before={before} after={after}"
            return "PENDING_PAYROLL, 0 JE (AC #3 satisfied)"
        finally:
            pass


@check("6. cancel DRAFT with reason → CANCELLED")
def _():
    from app import create_app
    from app.services.hr_decisions import (
        create_decision, cancel_decision)

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HR6")
        try:
            emp = _make_employee(cid)
            dec = create_decision(cid,
                employee_id=emp.id, kind="TRANSFER",
                effective_date=date.today(),
                title="نقل تجريبي", actor_id=oid)
            cancel_decision(dec, reason="اتلغى بأمر جديد",
                            actor_id=oid)
            assert dec.status == "CANCELLED"
            assert dec.cancel_reason == "اتلغى بأمر جديد"
            return "cancelled with reason"
        finally:
            pass


@check("7. cancel without reason → refused")
def _():
    from app import create_app
    from app.services.hr_decisions import (
        create_decision, cancel_decision, HrDecisionError)

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HR7")
        try:
            emp = _make_employee(cid)
            dec = create_decision(cid,
                employee_id=emp.id, kind="TRANSFER",
                effective_date=date.today(),
                title="نقل تجريبي", actor_id=oid)
            for bad in ("", "   ", None):
                try:
                    cancel_decision(dec, reason=bad, actor_id=oid)
                except HrDecisionError:
                    continue
                raise AssertionError(f"blank reason {bad!r} allowed")
            return "blank cancel reason correctly refused"
        finally:
            pass


@check("8. cancel EXECUTED → refused with 'قرار عكسي' message")
def _():
    from app import create_app
    from app.services.hr_decisions import (
        create_decision, execute_decision, cancel_decision,
        HrDecisionError)

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HR8")
        try:
            emp = _make_employee(cid)
            dec = create_decision(cid,
                employee_id=emp.id, kind="TRANSFER",
                effective_date=date.today(),
                title="نقل", actor_id=oid)
            execute_decision(dec, actor_id=oid)
            try:
                cancel_decision(dec, reason="تصحيح", actor_id=oid)
            except HrDecisionError as e:
                assert "عكسي" in str(e), \
                    f"expected 'عكسي' guidance, got: {e}"
                return "executed decision immutable"
            raise AssertionError("cancel of EXECUTED was allowed!")
        finally:
            pass


@check("9. TERMINATION execute → Employee TERMINATED via existing service")
def _():
    from app import create_app, db
    from app.models import Employee, EmployeeStatus
    from app.services.hr_decisions import (
        create_decision, execute_decision)

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HR9")
        try:
            emp = _make_employee(cid, "سالم")
            term_date = date.today() - timedelta(days=5)
            dec = create_decision(cid,
                employee_id=emp.id, kind="TERMINATION",
                effective_date=term_date,
                title="إنهاء خدمة",
                body="استقالة",
                actor_id=oid)
            execute_decision(dec, actor_id=oid)
            assert dec.status == "EXECUTED"
            db.session.refresh(emp)
            assert emp.status == EmployeeStatus.TERMINATED, \
                f"expected TERMINATED, got {emp.status}"
            assert emp.termination_date == term_date, \
                f"termination_date mismatch: {emp.termination_date}"
            assert emp.is_active is False
            return "employee TERMINATED via terminate_employee"
        finally:
            pass


@check("10. company isolation — B's employee not editable from A")
def _():
    from app import create_app
    from app.services.hr_decisions import (
        create_decision, HrDecisionError)

    app = create_app()
    with app.app_context():
        email_a, cid_a, oid_a = _boot("HR10A")
        try:
            email_b, cid_b, oid_b = _boot("HX10B")
            emp_b = _make_employee(cid_b, "مورَّط")
            # Attempt to create a decision on B's employee via A's cid
            try:
                create_decision(cid_a,
                    employee_id=emp_b.id, kind="PROMOTION",
                    effective_date=date.today(),
                    title="اختراق", actor_id=oid_a)
            except HrDecisionError as e:
                assert "غير موجود" in str(e), (
                    f"expected 'غير موجود' guard, got: {e}")
                return "cross-tenant attempt refused"
            raise AssertionError("cross-tenant create accepted!")
        finally:
            pass


@check("11. blank body for PENALTY → refused")
def _():
    from app import create_app
    from app.services.hr_decisions import (
        create_decision, HrDecisionError)

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HR11")
        try:
            emp = _make_employee(cid)
            from app.models import Account
            cash = Account.query.filter_by(company_id=cid, code="1110").first()
            try:
                create_decision(cid,
                    employee_id=emp.id, kind="PENALTY",
                    effective_date=date.today(),
                    title="جزاء بدون سبب",
                    timing="IMMEDIATE",
                    amount=100,
                    payment_account_id=cash.id,
                    body="",
                    actor_id=oid)
            except HrDecisionError as e:
                assert "سبب" in str(e), f"expected 'سبب' guard, got: {e}"
                return "blank penalty body correctly refused"
            raise AssertionError("empty penalty body accepted!")
        finally:
            pass


@check("12. feature registry — hr_decisions_index under module=hr")
def _():
    from app.services.feature_registry import all_features
    feats = {f.code: f for f in all_features()}
    assert "hr_decisions_index" in feats, "feature not registered"
    f = feats["hr_decisions_index"]
    assert f.module == "hr", f"wrong module: {f.module}"
    return "hr_decisions_index in registry, module=hr"


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
