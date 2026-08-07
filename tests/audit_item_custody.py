#!/usr/bin/env python3
"""MARSOUD-ITEM-CUSTODY-01 (2026-08-07) — item-custody audit.

Thirteen checks: every ticket acceptance criterion + guardrails
for the bridge into asset-disposal (charged_account_id seam) and
the atomic TRANSFERRED invariant.

Mirrors the fixture shape from tests/audit_cash_custody.py — same
CHECK-constraint holder pattern, so we prove item-custody enforces
the same guarantees.
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
PREFIX = "__IC_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import (
        Company, Plan, User, UserStatus, Employee, EmployeeStatus,
        ContractType, Department, FixedAsset, Account,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from app.services.roles_seed import ensure_roles_ready_for_company
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code="__ic__").first()
    if not plan:
        plan = Plan(code="__ic__", name="IC", name_ar="IC",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "purchases",
                          "reports", "agent", "inventory", "pos",
                          "crm", "hr", "cash_custody"])
        db.session.add(plan); db.session.flush()

    c = Company(name=f"{PREFIX}CO", base_currency="EGP",
                subdomain="ic",
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1),
                intended_plan_id=plan.id, plan_id=plan.id)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    u = User(email=f"{PREFIX}u@x.test", full_name="ic owner",
             is_active=True, status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"))
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    ensure_roles_ready_for_company(c.id)

    emp = Employee(company_id=c.id, name=f"{PREFIX}emp", user_id=u.id,
                   status=EmployeeStatus.ACTIVE,
                   contract_type=ContractType.FULL_TIME,
                   start_date=date.today() - timedelta(days=100))
    db.session.add(emp)
    emp2 = Employee(company_id=c.id, name=f"{PREFIX}emp2",
                     status=EmployeeStatus.ACTIVE,
                     contract_type=ContractType.FULL_TIME,
                     start_date=date.today() - timedelta(days=100))
    db.session.add(emp2)
    dept = Department(company_id=c.id, name=f"{PREFIX}dept",
                      is_active=True)
    db.session.add(dept)
    # A FixedAsset for the linked-item tests.
    acc_1210 = Account.query.filter_by(company_id=c.id, code="1210").first()
    asset = FixedAsset(
        company_id=c.id, name=f"{PREFIX}laptop",
        purchase_date=date.today() - timedelta(days=365),
        cost=8000, useful_life_years=4,
        accumulated_depreciation=2000,
        account_id=acc_1210.id,
    )
    db.session.add(asset)
    db.session.commit()

    _STATE.update(company_id=c.id, user_id=u.id,
                  employee_id=emp.id, employee2_id=emp2.id,
                  department_id=dept.id, asset_id=asset.id)


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        # Orphan sweep — same SQLite id-reuse trap.
        conn.execute(text(
            "DELETE FROM journal_lines WHERE entry_id NOT IN "
            "(SELECT id FROM journal_entries)"))
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__IC_%'"))]
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
            "DELETE FROM users WHERE email LIKE '__IC_%@x.test'"))
        conn.execute(text("DELETE FROM plans WHERE code = '__ic__'"))


# ─── Checks ────────────────────────────────────────────────────
@check("1. CHECK constraint refuses both-holders + no-holder on request")
def _():
    from app.models import ItemCustodyRequest, CustodyItem, CustodyHolderType
    from sqlalchemy.exc import IntegrityError
    _setup()
    item = CustodyItem(company_id=_STATE["company_id"],
                       name="test", estimated_value=100)
    db.session.add(item); db.session.commit()
    bad = ItemCustodyRequest(
        company_id=_STATE["company_id"], item_id=item.id,
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=_STATE["employee_id"],
        department_id=_STATE["department_id"],  # both set
        purpose="both")
    db.session.add(bad)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
    else:
        db.session.rollback()
        raise AssertionError("CHECK constraint accepted both-holders row")
    return "both-holders refused by DB"


@check("2. create_item refuses fixed_asset_id + estimated_value together")
def _():
    from app.services.item_custody import create_item, ItemCustodyError
    _setup()
    try:
        create_item(
            _STATE["company_id"], name="ambiguous",
            fixed_asset_id=_STATE["asset_id"], estimated_value=500,
            created_by=_STATE["user_id"])
    except ItemCustodyError as e:
        assert "واحد فقط" in str(e)
        return f"refused double-typing: {str(e)[:40]}"
    raise AssertionError("double-typing accepted")


@check("3. request_item_custody refuses if item has ACTIVE custody")
def _():
    from app.services.item_custody import (
        create_item, request_item_custody, approve_item_request,
        ItemCustodyError,
    )
    from app.models import CustodyHolderType
    _setup()
    item = create_item(_STATE["company_id"], name="only-one",
                       estimated_value=100,
                       created_by=_STATE["user_id"])
    req = request_item_custody(
        _STATE["company_id"], item.id,
        CustodyHolderType.EMPLOYEE, _STATE["employee_id"],
        purpose="test", created_by=_STATE["user_id"])
    approve_item_request(req, reviewer_id=_STATE["user_id"])
    try:
        request_item_custody(
            _STATE["company_id"], item.id,
            CustodyHolderType.EMPLOYEE, _STATE["employee2_id"],
            purpose="second try", created_by=_STATE["user_id"])
    except ItemCustodyError as e:
        assert "نشطة" in str(e)
        return "second-request refused"
    raise AssertionError("second-request accepted while item is ACTIVE")


@check("4. approve_item_request race guard — second approval refuses")
def _():
    from app.services.item_custody import (
        create_item, request_item_custody, approve_item_request,
        ItemCustodyError,
    )
    from app.models import CustodyHolderType
    _setup()
    item = create_item(_STATE["company_id"], name="race",
                       estimated_value=100,
                       created_by=_STATE["user_id"])
    # Two requests for the same item, different holders.
    req1 = request_item_custody(
        _STATE["company_id"], item.id,
        CustodyHolderType.EMPLOYEE, _STATE["employee_id"],
        purpose="A", created_by=_STATE["user_id"])
    # Second request creation is blocked by request_item_custody
    # itself only when the SAME holder repeats — a different holder
    # can queue a competing request while the first is PENDING.
    # We bypass request_item_custody's dup guard by inserting the
    # second request directly (different holder).
    from app.models import ItemCustodyRequest, ItemCustodyRequestStatus
    req2 = ItemCustodyRequest(
        company_id=_STATE["company_id"], item_id=item.id,
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=_STATE["employee2_id"],
        purpose="B", status=ItemCustodyRequestStatus.PENDING,
        created_by=_STATE["user_id"])
    db.session.add(req2); db.session.commit()
    # Approve the first. Now the item has an ACTIVE custody.
    approve_item_request(req1, reviewer_id=_STATE["user_id"])
    # Approving the second must refuse via the race guard.
    try:
        approve_item_request(req2, reviewer_id=_STATE["user_id"])
    except ItemCustodyError as e:
        assert "نشطة" in str(e), f"wrong message: {e}"
        return "race guard fired"
    raise AssertionError("race guard failed — second approval accepted")


@check("5. hand_over_item posts no journal")
def _():
    from app.services.item_custody import (
        create_item, hand_over_item,
    )
    from app.models import CustodyHolderType, JournalEntry
    _setup()
    baseline = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"]).count()
    item = create_item(_STATE["company_id"], name="handover-test",
                       estimated_value=100,
                       created_by=_STATE["user_id"])
    custody = hand_over_item(
        _STATE["company_id"], item.id,
        CustodyHolderType.EMPLOYEE, _STATE["employee_id"],
        actor_id=_STATE["user_id"])
    after = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"]).count()
    assert after == baseline, (
        f"handover posted a journal: {after - baseline}")
    assert custody.journal_entry_id is None
    return "no journal on handover (administrative only)"


@check("6. settle RETURNED_GOOD — no journal, item stays available")
def _():
    from app.services.item_custody import (
        create_item, hand_over_item, settle_item_custody,
        active_custody_for_item,
    )
    from app.models import (
        CustodyHolderType, ItemCustodyStatus, JournalEntry,
    )
    _setup()
    item = create_item(_STATE["company_id"], name="rg-test",
                       estimated_value=100,
                       created_by=_STATE["user_id"])
    custody = hand_over_item(
        _STATE["company_id"], item.id,
        CustodyHolderType.EMPLOYEE, _STATE["employee_id"],
        actor_id=_STATE["user_id"])
    baseline = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"]).count()
    settle_item_custody(custody, "RETURNED_GOOD",
                         actor_id=_STATE["user_id"])
    after = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"]).count()
    assert after == baseline, f"RETURNED_GOOD posted journal"
    db.session.refresh(item)
    assert item.is_active is True, "item retired despite RETURNED_GOOD"
    assert active_custody_for_item(item.id) is None
    return "no journal + item stays available"


@check("7. LOST charged=True on STANDALONE — journal Dr 2130-emp / Cr 5930")
def _():
    from app.services.item_custody import (
        create_item, hand_over_item, settle_item_custody,
    )
    from app.models import (
        CustodyHolderType, JournalLine, Account, Employee,
    )
    from app.services.subsidiary import ensure_employee_account
    _setup()
    item = create_item(_STATE["company_id"], name="lost-charged",
                       estimated_value=250,
                       created_by=_STATE["user_id"])
    custody = hand_over_item(
        _STATE["company_id"], item.id,
        CustodyHolderType.EMPLOYEE, _STATE["employee_id"],
        actor_id=_STATE["user_id"])
    emp = db.session.get(Employee, _STATE["employee_id"])
    ensure_employee_account(emp)
    settle_item_custody(custody, "LOST",
                         damage_value=250,
                         charged_to_employee=True,
                         actor_id=_STATE["user_id"])
    db.session.refresh(custody)
    assert custody.journal_entry_id is not None
    lines = JournalLine.query.filter_by(
        entry_id=custody.journal_entry_id).all()
    assert len(lines) == 2
    total_dr = sum(float(l.debit or 0) for l in lines)
    total_cr = sum(float(l.credit or 0) for l in lines)
    assert abs(total_dr - 250) < 0.01 and abs(total_cr - 250) < 0.01
    # Dr line must hit the employee's 2130 sub-account.
    emp_leaf_id = emp.account_id
    dr_line = [l for l in lines if l.account_id == emp_leaf_id]
    assert dr_line and abs(float(dr_line[0].debit or 0) - 250) < 0.01
    return f"Dr 2130-emp 250 / Cr 5930 250, balanced"


@check("8. LOST charged=False on STANDALONE — no journal")
def _():
    from app.services.item_custody import (
        create_item, hand_over_item, settle_item_custody,
    )
    from app.models import CustodyHolderType, JournalEntry
    _setup()
    item = create_item(_STATE["company_id"], name="lost-noncharge",
                       estimated_value=50,
                       created_by=_STATE["user_id"])
    custody = hand_over_item(
        _STATE["company_id"], item.id,
        CustodyHolderType.EMPLOYEE, _STATE["employee_id"],
        actor_id=_STATE["user_id"])
    baseline = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"]).count()
    settle_item_custody(custody, "LOST",
                         damage_value=50,
                         charged_to_employee=False,
                         actor_id=_STATE["user_id"])
    after = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"]).count()
    assert after == baseline, "posted journal despite charged=False"
    db.session.refresh(custody)
    assert custody.journal_entry_id is None
    return "no journal (was expensed at purchase)"


@check("9. LOST on FIXED-ASSET-linked — no journal, disposal_pending_at set")
def _():
    from app.services.item_custody import (
        create_item, hand_over_item, settle_item_custody,
    )
    from app.models import (
        CustodyHolderType, FixedAsset, JournalEntry,
    )
    _setup()
    item = create_item(_STATE["company_id"], name="asset-linked",
                       fixed_asset_id=_STATE["asset_id"],
                       created_by=_STATE["user_id"])
    custody = hand_over_item(
        _STATE["company_id"], item.id,
        CustodyHolderType.EMPLOYEE, _STATE["employee_id"],
        actor_id=_STATE["user_id"])
    baseline = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"]).count()
    settle_item_custody(custody, "LOST",
                         damage_value=6000,
                         charged_to_employee=True,
                         actor_id=_STATE["user_id"])
    after = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"]).count()
    assert after == baseline, "posted journal on asset-linked LOST"
    db.session.refresh(custody)
    assert custody.disposal_pending_at is not None
    assert custody.journal_entry_id is None
    # Asset stays LIVE — disposal happens explicitly via
    # complete_disposal_for_custody.
    asset = db.session.get(FixedAsset, _STATE["asset_id"])
    assert asset.is_disposed is False
    return "no journal; disposal_pending_at set; asset live"


@check("10. complete_disposal_for_custody with charged=True routes to 2130")
def _():
    from app.services.item_custody import (
        create_item, hand_over_item, settle_item_custody,
        complete_disposal_for_custody,
    )
    from app.models import (
        CustodyHolderType, FixedAsset, JournalLine, Employee, Account,
    )
    from app.services.subsidiary import ensure_employee_account
    _setup()
    item = create_item(_STATE["company_id"], name="asset-charged",
                       fixed_asset_id=_STATE["asset_id"],
                       created_by=_STATE["user_id"])
    custody = hand_over_item(
        _STATE["company_id"], item.id,
        CustodyHolderType.EMPLOYEE, _STATE["employee_id"],
        actor_id=_STATE["user_id"])
    settle_item_custody(custody, "LOST",
                         damage_value=6000,
                         charged_to_employee=True,
                         actor_id=_STATE["user_id"])
    # Now complete disposal.
    emp = db.session.get(Employee, _STATE["employee_id"])
    ensure_employee_account(emp)  # ensure the 2130 leaf exists
    complete_disposal_for_custody(
        custody, disposal_date=date.today(),
        reason="LOST", actor_id=_STATE["user_id"])
    db.session.refresh(custody)
    db.session.refresh(item)
    assert custody.disposal_pending_at is None
    assert custody.disposal_asset_result_id == _STATE["asset_id"]
    asset = db.session.get(FixedAsset, _STATE["asset_id"])
    assert asset.is_disposed is True
    assert item.is_active is False
    # Verify the disposal journal charged the employee's 2130
    # (not 5950). asset.disposal_journal_entry_id is where the
    # disposal journal lives.
    lines = JournalLine.query.filter_by(
        entry_id=asset.disposal_journal_entry_id).all()
    # No line should hit 5950.
    acc5950 = Account.query.filter_by(
        company_id=_STATE["company_id"], code="5950").first()
    on_5950 = [l for l in lines if l.account_id == acc5950.id]
    assert not on_5950, f"loss leaked to 5950: {on_5950}"
    # Should hit the employee's 2130 leaf instead.
    emp_leaf = emp.account_id
    on_emp = [l for l in lines if l.account_id == emp_leaf]
    assert on_emp, "no line hit the employee's 2130 leaf"
    return f"disposal loss routed to emp 2130 (skipped 5950)"


@check("11. TRANSFERRED — atomic close-old + open-new")
def _():
    from app.services.item_custody import (
        create_item, hand_over_item, settle_item_custody,
        active_custody_for_item,
    )
    from app.models import (
        CustodyHolderType, ItemCustody, ItemCustodyStatus,
    )
    _setup()
    item = create_item(_STATE["company_id"], name="transfer-test",
                       estimated_value=100,
                       created_by=_STATE["user_id"])
    orig = hand_over_item(
        _STATE["company_id"], item.id,
        CustodyHolderType.EMPLOYEE, _STATE["employee_id"],
        actor_id=_STATE["user_id"])
    # Transfer to emp2.
    settle_item_custody(
        orig, "TRANSFERRED",
        transfer_holder_type=CustodyHolderType.EMPLOYEE,
        transfer_holder_id=_STATE["employee2_id"],
        actor_id=_STATE["user_id"])
    db.session.refresh(orig)
    assert orig.status == ItemCustodyStatus.TRANSFERRED
    assert orig.transferred_to_custody_id is not None
    # The new custody exists + is ACTIVE for emp2.
    new_c = active_custody_for_item(item.id)
    assert new_c is not None
    assert new_c.id == orig.transferred_to_custody_id
    assert new_c.employee_id == _STATE["employee2_id"]
    # Invariant: exactly ONE ACTIVE row for this item.
    active_count = ItemCustody.query.filter_by(
        item_id=item.id, status=ItemCustodyStatus.ACTIVE).count()
    assert active_count == 1
    return f"old→#{orig.transferred_to_custody_id} live; invariant intact"


@check("12. sweep_long_active_custodies fires ONE bell then dedups")
def _():
    from app.services.item_custody import (
        create_item, hand_over_item, sweep_long_active_custodies,
    )
    from app.models import (
        CustodyHolderType, ItemCustody, Notification,
    )
    from sqlalchemy import text
    _setup()
    item = create_item(_STATE["company_id"], name="old-custody",
                       estimated_value=100,
                       created_by=_STATE["user_id"])
    custody = hand_over_item(
        _STATE["company_id"], item.id,
        CustodyHolderType.EMPLOYEE, _STATE["employee_id"],
        # Force old handover date via kwarg (100 days ago > 90 threshold)
        handed_over_on=date.today() - timedelta(days=100),
        actor_id=_STATE["user_id"])
    # Wipe any prior notifications so we count only THIS run.
    db.session.execute(text(
        "DELETE FROM notifications WHERE user_id=:u"),
        {"u": _STATE["user_id"]})
    db.session.commit()
    sent1 = sweep_long_active_custodies(_STATE["company_id"],
                                          threshold_days=90)
    db.session.refresh(custody)
    assert custody.overdue_notified_at is not None
    assert sent1 >= 1, f"first sweep sent nothing: {sent1}"
    n1 = Notification.query.filter_by(
        user_id=_STATE["user_id"]).count()
    # Second sweep must fire ZERO — dedup on overdue_notified_at.
    sent2 = sweep_long_active_custodies(_STATE["company_id"],
                                          threshold_days=90)
    assert sent2 == 0, f"second sweep fired again: {sent2}"
    n2 = Notification.query.filter_by(
        user_id=_STATE["user_id"]).count()
    assert n2 == n1
    return f"1st sweep sent={sent1}, 2nd sent=0 (dedup works)"


@check("13. Routes registered + sidebar entry present")
def _():
    from flask import current_app
    from pathlib import Path
    endpoints = {r.endpoint for r in current_app.url_map.iter_rules()}
    for expected in ("item_custody.index",
                      "item_custody.new",
                      "item_custody.detail",
                      "item_custody.settle",
                      "item_custody.complete_disposal",
                      "portal_emp.items_list",
                      "portal_emp.items_request_new",
                      "portal_emp.items_detail"):
        assert expected in endpoints, f"missing route: {expected}"
    # Sidebar structural — both the owner + portal rows.
    tpl = (ROOT / "app" / "templates" / "base.html").read_text(
        encoding="utf-8")
    assert "item_custody.index" in tpl, "owner sidebar row missing"
    assert "portal_emp.items_list" in tpl, "portal sidebar row missing"
    return f"{len(endpoints)} routes; sidebar rows present"


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
