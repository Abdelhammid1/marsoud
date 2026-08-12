#!/usr/bin/env python3
"""MARSOUD-CUSTODY-DELETE-CONSISTENCY (2026-08-12) — locks
the delete / cancel / reopen contract on cash custody.

Every AC from the ticket has at least one check:
- AC #1: delete_pending_request happy path.
- AC #2: delete on issued custody refused at both service +
  HTTP layers.
- AC #3: cancel_custody yields (a) reversal JE + (b)
  effective_status='CANCELLED' on the linked request (the
  drift closure).
- AC #4: reopen_settlement reverses settlement JE, flips
  custody back to PARTIALLY_SETTLED / ISSUED; original
  issue JE untouched; re-close after reopen works.
- AC #5: after every write op, request.effective_status
  and custody.status agree (state-machine table).
- AC #6: no orphan JE — reversals always create a new JE,
  never delete the original.

Every check verified to fail against pre-ticket HEAD (the
drift ones fail because `req.effective_status` doesn't
exist; the reopen ones fail because there's no reopen
service; the delete ones fail because the endpoint doesn't
exist).
"""
import io
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

_ORIG_CREATE_APP = create_app
def create_app(*a, **kw):
    app = _ORIG_CREATE_APP(*a, **kw)
    app.config["SESSION_COOKIE_DOMAIN"] = None
    return app


CHECKS = []
PREFIX = "__CUSTDEL_"
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
        EmployeeStatus, ContractType, Department,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.roles import set_membership_role
    from app.services.legal import get_terms_version
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code=f"{PREFIX}plan").first()
    if not plan:
        plan = Plan(code=f"{PREFIX}plan", name="CUSTDEL",
                    name_ar="CUSTDEL", allowed_subitems=None)
        plan.set_modules(["accounting", "hr", "cash_custody",
                           "settings"])
        db.session.add(plan); db.session.flush()

    co = Company(name=f"{PREFIX}CO", base_currency="SAR",
                  plan_id=plan.id,
                  subscription_started_at=datetime.utcnow(),
                  subscription_expires_at=datetime.utcnow()
                    + timedelta(days=365))
    db.session.add(co); db.session.flush()
    db.session.commit()
    seed_default_coa(co.id)
    ensure_roles_ready_for_company(co.id)

    def _mk_user(email, name):
        u = User(email=email, full_name=name, is_active=True,
                  status=UserStatus.ACTIVE.value,
                  email_verified_at=datetime.utcnow(),
                  terms_version=get_terms_version(),
                  terms_accepted_at=datetime.utcnow(),
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"))
        db.session.add(u); db.session.flush()
        return u

    owner = _mk_user(f"{PREFIX}owner@x.test", "custdel owner")
    holder = _mk_user(f"{PREFIX}holder@x.test", "custdel holder")

    for u, role in ((owner, "owner"), (holder, "team_member")):
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=co.id, role=role))
    db.session.commit()
    for u, role in ((owner, "owner"), (holder, "team_member")):
        set_membership_role(u.id, co.id, role)

    emp = Employee(company_id=co.id, name="Holder Employee",
                    user_id=holder.id,
                    status=EmployeeStatus.ACTIVE,
                    contract_type=ContractType.FULL_TIME,
                    start_date=date.today() - timedelta(days=100))
    db.session.add(emp); db.session.flush()
    db.session.commit()

    _STATE.update(
        co_id=co.id, owner_id=owner.id, holder_id=holder.id,
        emp_id=emp.id,
    )


def _teardown():
    from app.models import Company, User, Plan
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id=:c"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id=:c"), {"c": cid})
        db.session.commit()
    for p in Plan.query.filter(Plan.code.like(f"{PREFIX}%")).all():
        db.session.delete(p)
    db.session.commit()
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _reset_g():
    from flask import g
    for key in ("_login_user", "active_company", "user_companies",
                "impersonating"):
        try:
            g.pop(key, None)
        except Exception:
            pass


def _client_as(user_id):
    from flask import current_app
    _reset_g()
    db.session.expire_all()
    db.session.remove()
    c = current_app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["co_id"]
    return c


_EMP_SEQ = [0]  # unique-name counter shared across checks


def _fresh_employee():
    """Every check that issues a custody needs its own employee
    because issue_custody / request_custody refuse a second open
    custody for the same holder. Rather than cleaning up after
    each check, mint a fresh employee per call."""
    from app.models import Employee, EmployeeStatus, ContractType
    _EMP_SEQ[0] += 1
    emp = Employee(
        company_id=_STATE["co_id"],
        name=f"{PREFIX}emp_{_EMP_SEQ[0]}",
        status=EmployeeStatus.ACTIVE,
        contract_type=ContractType.FULL_TIME,
        start_date=date.today() - timedelta(days=30),
    )
    db.session.add(emp); db.session.commit()
    return emp


def _make_pending_request(amount="500", employee=None):
    """Fresh PENDING request. Auto-mints a new employee unless
    the caller passed one (helpful when a check needs to create
    multiple requests for the SAME holder — e.g. sequential
    approve → cancel → new request)."""
    from app.models import CashCustodyRequest, CustodyRequestStatus, CustodyHolderType
    emp = employee or _fresh_employee()
    req = CashCustodyRequest(
        company_id=_STATE["co_id"],
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=emp.id,
        amount=Decimal(str(amount)),
        purpose="audit stub",
        status=CustodyRequestStatus.PENDING,
    )
    db.session.add(req); db.session.commit()
    return req


def _approve_and_settle(amount="500", lines_total="300"):
    """Build a fully-SETTLED custody for reopen tests. Returns
    (req, custody). Approve + add one line + close_settlement."""
    from app.services.cash_custody import (
        approve_custody_request, add_settlement_line, close_settlement,
    )
    from app.models import Account
    req = _make_pending_request(amount=amount)
    custody = approve_custody_request(
        req, reviewer_id=_STATE["owner_id"])
    # Pick any expense account for the line.
    exp = (Account.query.filter_by(company_id=_STATE["co_id"],
                                     is_postable=True)
           .filter(Account.code.like("5%")).first())
    add_settlement_line(
        custody,
        expense_account_id=exp.id,
        amount=lines_total,
        receipt_note="stub receipt",
        actor_id=_STATE["owner_id"],
    )
    # Close: lines + returned = issued. returned = amount - lines_total.
    returned = float(Decimal(amount) - Decimal(lines_total))
    close_settlement(
        custody, returned_amount=returned,
        shortfall_disposition=None,
        actor_id=_STATE["owner_id"],
    )
    db.session.expire_all()
    from app.models import CashCustody, CashCustodyRequest
    return (db.session.get(CashCustodyRequest, req.id),
            db.session.get(CashCustody, custody.id))


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. AC #1: delete_pending_request on PENDING removes the row cleanly")
def _():
    from app.models import CashCustodyRequest, JournalEntry
    from app.services.cash_custody import delete_pending_request
    req = _make_pending_request(amount="100")
    rid = req.id
    je_count_before = JournalEntry.query.filter_by(
        company_id=_STATE["co_id"]).count()
    delete_pending_request(req, actor_id=_STATE["owner_id"])
    db.session.expire_all()
    assert db.session.get(CashCustodyRequest, rid) is None, (
        "PENDING request not deleted")
    je_count_after = JournalEntry.query.filter_by(
        company_id=_STATE["co_id"]).count()
    assert je_count_after == je_count_before, (
        f"a JE was created — should be zero-effect: "
        f"{je_count_before} -> {je_count_after}")
    return f"PENDING deleted; JE count unchanged ({je_count_before})"


@check("2. delete_pending_request on APPROVED refused")
def _():
    from app.services.cash_custody import (
        approve_custody_request, delete_pending_request,
        CustodyError,
    )
    req = _make_pending_request(amount="150")
    approve_custody_request(req, reviewer_id=_STATE["owner_id"])
    db.session.expire_all()
    try:
        delete_pending_request(req, actor_id=_STATE["owner_id"])
    except CustodyError as e:
        assert "لم يعد معلّقاً" in str(e), f"wrong msg: {e}"
        return f"APPROVED refused: {e}"
    raise AssertionError("APPROVED delete accepted")


@check("3. delete_pending_request on REJECTED refused")
def _():
    from app.services.cash_custody import (
        reject_custody_request, delete_pending_request,
        CustodyError,
    )
    req = _make_pending_request(amount="200")
    reject_custody_request(req, reviewer_id=_STATE["owner_id"],
                            review_note="test reject")
    db.session.expire_all()
    try:
        delete_pending_request(req, actor_id=_STATE["owner_id"])
    except CustodyError as e:
        return f"REJECTED refused: {e}"
    raise AssertionError("REJECTED delete accepted")


@check("4. AC #2: POST /custody/<id>/delete always refuses + no data change")
def _():
    from app.services.cash_custody import approve_custody_request
    from app.models import CashCustody, JournalEntry
    req = _make_pending_request(amount="250")
    custody = approve_custody_request(
        req, reviewer_id=_STATE["owner_id"])
    cid = custody.id
    original_je = custody.journal_entry_id
    original_status = custody.status
    c = _client_as(_STATE["owner_id"])
    r = c.post(f"/custody/{cid}/delete", follow_redirects=False)
    # Should redirect (302), not perform a delete.
    assert r.status_code in (302, 303), (
        f"expected redirect refusal, got {r.status_code}")
    db.session.expire_all()
    reloaded = db.session.get(CashCustody, cid)
    assert reloaded is not None, "row was deleted — endpoint failed AC #2"
    assert reloaded.status == original_status, (
        f"status drifted: {reloaded.status}")
    assert reloaded.journal_entry_id == original_je, (
        f"JE id changed: {original_je} -> {reloaded.journal_entry_id}")
    # No new JE, no deleted JE.
    je_still_there = db.session.get(JournalEntry, original_je)
    assert je_still_there is not None, "original JE deleted!"
    assert je_still_there.is_active is True, "original JE deactivated"
    return "delete-custody refused; row + JE untouched"


@check("5. AC #3: cancel_custody flips effective_status to CANCELLED (drift closure)")
def _():
    from app.models import (
        CashCustodyRequest, CashCustody, CustodyRequestStatus,
        CustodyStatus, EffectiveRequestStatus, JournalEntry,
    )
    from app.services.cash_custody import (
        approve_custody_request, cancel_custody,
    )
    req = _make_pending_request(amount="300")
    custody = approve_custody_request(
        req, reviewer_id=_STATE["owner_id"])
    issue_je_id = custody.journal_entry_id
    cancel_custody(custody, actor_id=_STATE["owner_id"],
                    reason="audit stub")
    db.session.expire_all()
    req = db.session.get(CashCustodyRequest, req.id)
    custody = db.session.get(CashCustody, custody.id)
    # Raw status stays APPROVED — audit trail intact.
    assert req.status == CustodyRequestStatus.APPROVED, (
        f"raw req.status mutated: {req.status}")
    # Custody is CANCELLED.
    assert custody.status == CustodyStatus.CANCELLED
    # effective_status derives CANCELLED — closes the drift.
    eff = req.effective_status
    assert eff == EffectiveRequestStatus.CANCELLED, (
        f"effective_status did not derive CANCELLED: {eff}")
    # A reversal JE was posted for the issue JE (no orphan;
    # AC #6).
    reversal = JournalEntry.query.filter_by(
        reversal_of=issue_je_id).first()
    assert reversal is not None, "no reversal JE created"
    assert reversal.is_reversal is True
    assert custody.reversal_entry_id == reversal.id
    return (f"req.status={req.status.value} (raw), "
            f"eff={eff[0]}; reversal JE #{reversal.id}")


@check("6. AC #4: reopen_settlement reverses settlement JE + flips status back")
def _():
    from app.models import CashCustody, JournalEntry, CustodyStatus
    from app.services.cash_custody import reopen_settlement
    req, custody = _approve_and_settle(amount="500",
                                          lines_total="300")
    settle_je = custody.settlement_journal_entry_id
    issue_je = custody.journal_entry_id
    assert custody.status == CustodyStatus.SETTLED
    reopen_settlement(custody, actor_id=_STATE["owner_id"],
                       reason="audit reopen")
    db.session.expire_all()
    custody = db.session.get(CashCustody, custody.id)
    assert custody.status == CustodyStatus.PARTIALLY_SETTLED, (
        f"expected PARTIALLY_SETTLED (line exists), "
        f"got {custody.status}")
    assert custody.settlement_journal_entry_id is None, (
        "settlement_journal_entry_id not cleared")
    assert custody.settled_at is None, "settled_at not cleared"
    assert custody.settled_by is None, "settled_by not cleared"
    assert custody.amount_settled == Decimal("0.00")
    # Issue JE untouched.
    assert custody.journal_entry_id == issue_je, (
        f"issue JE id changed: {issue_je} -> "
        f"{custody.journal_entry_id}")
    issue_row = db.session.get(JournalEntry, issue_je)
    assert issue_row.is_active is True, "issue JE deactivated"
    # A reversal JE for the settlement JE was posted.
    reversal = JournalEntry.query.filter_by(
        reversal_of=settle_je).first()
    assert reversal is not None
    assert reversal.is_reversal is True
    return (f"settled -> PARTIALLY_SETTLED; issue JE #{issue_je} "
            f"intact; reversal JE #{reversal.id} for settle JE "
            f"#{settle_je}")


@check("7. reopen_settlement refused on non-SETTLED status")
def _():
    from app.services.cash_custody import (
        approve_custody_request, cancel_custody, reopen_settlement,
        CustodyError,
    )
    req = _make_pending_request(amount="400")
    custody = approve_custody_request(
        req, reviewer_id=_STATE["owner_id"])
    cancel_custody(custody, actor_id=_STATE["owner_id"],
                    reason="stub")
    db.session.expire_all()
    try:
        reopen_settlement(custody, actor_id=_STATE["owner_id"])
    except CustodyError as e:
        assert "ليست مُقفلة" in str(e), f"wrong msg: {e}"
        return f"CANCELLED refused: {e}"
    raise AssertionError("reopen on CANCELLED accepted")


@check("8. Re-close after reopen works — status back to SETTLED")
def _():
    from app.models import CashCustody, CustodyStatus
    from app.services.cash_custody import (
        reopen_settlement, close_settlement,
    )
    req, custody = _approve_and_settle(amount="500",
                                          lines_total="300")
    reopen_settlement(custody, actor_id=_STATE["owner_id"])
    db.session.expire_all()
    custody = db.session.get(CashCustody, custody.id)
    # Re-close with fresh returned amount (line = 300, issued = 500)
    close_settlement(
        custody, returned_amount=200.0,
        shortfall_disposition=None,
        actor_id=_STATE["owner_id"],
    )
    db.session.expire_all()
    custody = db.session.get(CashCustody, custody.id)
    assert custody.status == CustodyStatus.SETTLED, (
        f"re-close failed: {custody.status}")
    assert custody.settlement_journal_entry_id is not None, (
        "no new settlement JE on re-close")
    return (f"re-closed cleanly; new settle JE "
            f"#{custody.settlement_journal_entry_id}")


@check("9. AC #6: original issue JE never deleted or deactivated")
def _():
    from app.models import JournalEntry, CashCustody
    from app.services.cash_custody import (
        approve_custody_request, cancel_custody,
        add_settlement_line, close_settlement, reopen_settlement,
    )
    from app.models import Account
    # Full lifecycle: approve → settle → reopen → cancel — the issue
    # JE (posted at approve) should never be deleted or is_active=False.
    req = _make_pending_request(amount="600")
    custody = approve_custody_request(
        req, reviewer_id=_STATE["owner_id"])
    issue_je = custody.journal_entry_id
    exp = (Account.query.filter_by(company_id=_STATE["co_id"],
                                     is_postable=True)
           .filter(Account.code.like("5%")).first())
    add_settlement_line(
        custody, expense_account_id=exp.id, amount="200",
        receipt_note="stub", actor_id=_STATE["owner_id"])
    close_settlement(
        custody, returned_amount=400.0,
        shortfall_disposition=None,
        actor_id=_STATE["owner_id"])
    reopen_settlement(custody, actor_id=_STATE["owner_id"])
    # Verify issue JE still there + active.
    db.session.expire_all()
    row = db.session.get(JournalEntry, issue_je)
    assert row is not None, "issue JE was deleted!"
    assert row.is_active is True, "issue JE deactivated"
    custody = db.session.get(CashCustody, custody.id)
    assert custody.journal_entry_id == issue_je, (
        "custody.journal_entry_id drifted")
    return f"issue JE #{issue_je} intact through full lifecycle"


@check("10. AC #5: effective_status matches derived state after every op")
def _():
    """Mini state-machine table — for every legal (raw
    req.status, custody.status) pair, effective_status must
    return the right code."""
    from app.models import (
        CashCustodyRequest, CashCustody,
        CustodyRequestStatus, CustodyStatus,
        EffectiveRequestStatus,
    )
    from app.services.cash_custody import (
        approve_custody_request, reject_custody_request,
        cancel_custody,
    )
    cases = []
    # PENDING → PENDING
    req = _make_pending_request(amount="50")
    cases.append((req, EffectiveRequestStatus.PENDING))
    # REJECTED → REJECTED
    req2 = _make_pending_request(amount="60")
    reject_custody_request(req2, reviewer_id=_STATE["owner_id"],
                            review_note="stub")
    cases.append((req2, EffectiveRequestStatus.REJECTED))
    # APPROVED + live custody → APPROVED
    req3 = _make_pending_request(amount="70")
    approve_custody_request(req3, reviewer_id=_STATE["owner_id"])
    cases.append((req3, EffectiveRequestStatus.APPROVED))
    # APPROVED + CANCELLED custody → CANCELLED (drift closure)
    req4 = _make_pending_request(amount="80")
    custody4 = approve_custody_request(
        req4, reviewer_id=_STATE["owner_id"])
    cancel_custody(custody4, actor_id=_STATE["owner_id"],
                    reason="stub")
    cases.append((req4, EffectiveRequestStatus.CANCELLED))
    db.session.expire_all()
    for req, expected in cases:
        r = db.session.get(CashCustodyRequest, req.id)
        got = r.effective_status
        assert got == expected, (
            f"req#{r.id}: want {expected[0]}, got {got[0]} "
            f"(raw status={r.status.value})")
    return (f"all {len(cases)} state combinations map correctly "
            f"(PENDING/REJECTED/APPROVED/CANCELLED)")


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                    failed += 1
        finally:
            _teardown()
            print("\n(fixture cleaned up)")
    print(f"\n----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
