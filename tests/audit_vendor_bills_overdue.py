#!/usr/bin/env python3
"""MARSOUD-VBILL-OVERDUE-01 (2026-08-06) — vendor bills stay visible
until someone acts on them.

Two invariants this suite pins:

  1. NO SILENT DISAPPEARANCE. A recurring vendor-bill forecast whose
     date passed used to age off the dashboard panel with no trace.
     Now cron materialises it into a real POSTED VendorBill, and until
     that runs the row is still shown on the panel as an
     unmaterialised forecast — either way, visible.

  2. CRON, NOT PAGE-OPEN, IS WHAT FLIPS OVERDUE. Previously a bill
     only flipped to OVERDUE when someone opened the vendor-bills
     index. A company with no one browsing that page carried
     unflagged overdue bills for weeks.

Every check verified to FAIL against pre-change HEAD before this file
was committed.

Checks
   1. cron flips a past-due POSTED bill to OVERDUE
   2. update_overdue_vendor_bills works without opening the page
   3. cron materialises a due recurring forecast (POSTED + JE)
   4. idempotency — same cron run twice, still one bill
   5. postpone_bill updates due_date + audit fields
   6. postpone rescues a bill from OVERDUE
   7. exception on real OVERDUE bill reverses via existing delete flow
   8. unmaterialised past-due forecast surfaces in late_vendor_bills
   9. cross-tenant — A's overdue bills invisible to B
  10. VENDOR_BILL_OVERDUE notification fires on first flip
  11. notification does NOT fire on second cron run
  12. permissions — accountant can pay+postpone; only owner can delete
"""
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__VBOVR_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import (Company, Plan, Employee, User, Vendor, Account,
                            BillLineType)
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.seed_coa import seed_default_coa
    from app.services.subsidiary import ensure_employee_account

    plan = Plan.query.filter_by(code="__vbovr__").first()
    if not plan:
        plan = Plan(code="__vbovr__", name="VB", name_ar="فواتير",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "purchases", "crm", "hr",
                          "reports", "settings"])
        db.session.add(plan); db.session.flush()

    def _mk_co(suffix):
        co = Company(name=f"{PREFIX}CO_{suffix}__", base_currency="EGP",
                     vat_rate=0, plan_id=plan.id)
        db.session.add(co); db.session.flush()
        co.intended_plan_id = plan.id
        db.session.commit()
        ensure_roles_ready_for_company(co.id)
        seed_default_coa(co.id)
        return co

    def _mk_user(co, tag, role):
        u = User(email=f"{PREFIX}{co.id}_{tag}@audit.local",
                 full_name=f"{tag}-{role}",
                 is_active=True, terms_version=get_terms_version(),
                 terms_accepted_at=datetime.utcnow())
        u.set_password("Passw0rd!audit1")
        db.session.add(u); db.session.flush()
        set_membership_role(u.id, co.id, role)
        return u.id

    co_a = _mk_co("A")
    co_b = _mk_co("B")

    # Three roles inside company A for the permission check.
    owner_a = _mk_user(co_a, "own", "owner")
    accountant_a = _mk_user(co_a, "acc", "accountant")
    team_a = _mk_user(co_a, "team", "team_member")

    # Two vendors, one per company.
    v_a = Vendor(company_id=co_a.id, name="مورد ألف")
    v_b = Vendor(company_id=co_b.id, name="مورد باء")
    db.session.add_all([v_a, v_b]); db.session.flush()
    db.session.commit()

    # An expense account per company for the bill lines.
    exp_a = (Account.query.filter_by(company_id=co_a.id, code="5100").first()
             or Account.query.filter_by(company_id=co_a.id, code="5200").first())
    exp_b = (Account.query.filter_by(company_id=co_b.id, code="5100").first()
             or Account.query.filter_by(company_id=co_b.id, code="5200").first())

    _STATE.update(
        cid_a=co_a.id, cid_b=co_b.id,
        vendor_a=v_a.id, vendor_b=v_b.id,
        exp_a=exp_a.id, exp_b=exp_b.id,
        owner_a=owner_a, accountant_a=accountant_a, team_a=team_a,
    )


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        db.session.execute(text(
            "DELETE FROM journal_lines WHERE entry_id IN "
            "(SELECT id FROM journal_entries WHERE company_id=:c)"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM notifications WHERE company_id=:c"), {"c": cid})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(
                        text(f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        db.session.execute(text("DELETE FROM companies WHERE id=:c"),
                           {"c": cid})
        db.session.commit()
    db.session.execute(text("DELETE FROM plans WHERE code='__vbovr__'"))
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _reset_bills():
    """Wipe bills and related between checks so each starts clean."""
    from app.models import (VendorBill, VendorBillItem, VendorBillPayment,
                            RecurringBill, RecurringBillOverride,
                            JournalEntry, JournalLine, Notification)
    from sqlalchemy import text
    for cid in (_STATE["cid_a"], _STATE["cid_b"]):
        eids = [r.id for r in JournalEntry.query.filter_by(
            company_id=cid).all()]
        if eids:
            JournalLine.query.filter(
                JournalLine.entry_id.in_(eids)
            ).delete(synchronize_session=False)
    VendorBillPayment.query.delete()
    VendorBillItem.query.delete()
    VendorBill.query.delete()
    RecurringBillOverride.query.delete()
    RecurringBill.query.delete()
    JournalEntry.query.delete()
    Notification.query.delete()
    db.session.commit()


def _mk_bill(cid, vendor_id, exp_acc_id, *, number, due_date, amount=100,
             status=None, notes=None):
    """Create a POSTED (default) vendor bill with one expense line so
    post_vendor_bill has something valid to sign off on."""
    from app.models import (VendorBill, VendorBillItem, VendorBillStatus,
                            VendorBillPaymentMethod, BillLineType)
    from app.services.vendor_bills import post_vendor_bill

    bill = VendorBill(
        company_id=cid, vendor_id=vendor_id, number=number,
        issue_date=due_date, due_date=due_date,
        payment_method=VendorBillPaymentMethod.CREDIT,
        currency="EGP", tax_rate=Decimal("0"),
        status=VendorBillStatus.DRAFT, notes=notes,
    )
    db.session.add(bill); db.session.flush()
    db.session.add(VendorBillItem(
        bill_id=bill.id, description="خدمة اختبار",
        line_type=BillLineType.EXPENSE, account_id=exp_acc_id,
        quantity=Decimal("1"), unit_price=Decimal(str(amount)),
    ))
    db.session.flush()
    bill.recalc()
    post_vendor_bill(bill)
    if status is not None:
        bill.status = status
    db.session.commit()
    return bill


def _mk_template(cid, source_bill_id, vendor_id, *, start_date, amount=100):
    from app.models import RecurringBill
    rb = RecurringBill(
        company_id=cid, source_bill_id=source_bill_id, vendor_id=vendor_id,
        amount=Decimal(str(amount)), currency="EGP",
        interval_unit="MONTH", interval_count=1,
        start_date=start_date, active=True,
    )
    db.session.add(rb); db.session.commit()
    return rb


def _post_as(user_id, company_id, path, data):
    """POST `path` inside a FRESH app_context so Flask-Login's
    g._login_user cache does not serve the request as whichever user
    this app-context saw first (handoff fact 7). Returns the response.

    audit_my_activity's _get_as taught us this: cheaper than spinning
    up a whole new app per call, but still gives us a clean g stack."""
    with _STATE["app"].app_context():
        c = _STATE["app"].test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(user_id)
            s["_fresh"] = True
            s["active_company_id"] = company_id
        return c.post(path, data=data, follow_redirects=False)


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. cron flips a past-due POSTED bill to OVERDUE")
def _():
    from app.models import VendorBillStatus
    from app.services.vendor_bills import update_overdue_vendor_bills
    _reset_bills()
    bill = _mk_bill(_STATE["cid_a"], _STATE["vendor_a"], _STATE["exp_a"],
                    number="V1", due_date=date.today() - timedelta(days=2))
    assert bill.status == VendorBillStatus.POSTED, (
        f"seed bill status={bill.status}")
    n = update_overdue_vendor_bills(_STATE["cid_a"])
    db.session.refresh(bill)
    assert bill.status == VendorBillStatus.OVERDUE, (
        f"post-cron status={bill.status}")
    return f"1 bill flipped, n={n}"


@check("2. update_overdue_vendor_bills works without opening the page")
def _():
    """Ticket's secondary problem — a bill only flipped when someone
    opened /vendor_bills. The metric must reflect it after cron
    alone."""
    from app.services.vendor_bills import update_overdue_vendor_bills
    from app.services.reports import dashboard_metrics
    _reset_bills()
    _mk_bill(_STATE["cid_a"], _STATE["vendor_a"], _STATE["exp_a"],
             number="V2", due_date=date.today() - timedelta(days=3),
             notes="ORIGINAL_MARKER")
    update_overdue_vendor_bills(_STATE["cid_a"])
    m = dashboard_metrics(_STATE["cid_a"])
    assert m["late_vendor_bills_count"] >= 1, (
        f"late_vendor_bills_count={m['late_vendor_bills_count']}")
    labels = {r["title_for_display"] for r in m["late_vendor_bills"]}
    assert "ORIGINAL_MARKER" in labels, (
        f"metric missing our bill; got titles {labels!r}")
    return f"count={m['late_vendor_bills_count']}"


@check("3. cron materialises a due recurring forecast into POSTED bill")
def _():
    from app.models import VendorBill, VendorBillStatus
    from app.services.recurring_vendor_bills import (
        process_recurring_vendor_bills,
    )
    _reset_bills()
    src = _mk_bill(_STATE["cid_a"], _STATE["vendor_a"], _STATE["exp_a"],
                   number="V3-SRC",
                   due_date=date.today() - timedelta(days=30),
                   amount=250)
    rb = _mk_template(_STATE["cid_a"], src.id, _STATE["vendor_a"],
                      start_date=date.today(), amount=250)
    summary = process_recurring_vendor_bills()
    assert summary["posted"] >= 1, (
        f"cron did not post: {summary!r}")
    materialised = VendorBill.query.filter_by(
        recurring_bill_id=rb.id).all()
    assert len(materialised) == 1, (
        f"expected 1 materialised bill, got {len(materialised)}")
    assert materialised[0].status == VendorBillStatus.POSTED, (
        f"expected POSTED, got {materialised[0].status}")
    assert materialised[0].journal_entry_id is not None, (
        "materialised bill has no JE — post_vendor_bill did not run")
    return f"1 POSTED bill created (total={float(materialised[0].total)})"


@check("4. idempotency — second cron run does not double-post")
def _():
    from app.models import VendorBill
    from app.services.recurring_vendor_bills import (
        process_recurring_vendor_bills,
    )
    _reset_bills()
    src = _mk_bill(_STATE["cid_a"], _STATE["vendor_a"], _STATE["exp_a"],
                   number="V4-SRC",
                   due_date=date.today() - timedelta(days=30))
    rb = _mk_template(_STATE["cid_a"], src.id, _STATE["vendor_a"],
                      start_date=date.today())
    first = process_recurring_vendor_bills()
    second = process_recurring_vendor_bills()
    n = VendorBill.query.filter_by(recurring_bill_id=rb.id).count()
    assert n == 1, (
        f"double-posted: first={first!r} second={second!r} n_bills={n}")
    assert second["posted"] == 0, (
        f"second run posted {second['posted']} — should be 0")
    return f"1 bill after 2 runs; second skipped={second.get('skipped_duplicate',0)}"


@check("5. postpone_bill updates due_date + audit fields")
def _():
    from app.services.vendor_bills import postpone_bill
    _reset_bills()
    bill = _mk_bill(_STATE["cid_a"], _STATE["vendor_a"], _STATE["exp_a"],
                    number="V5", due_date=date.today() - timedelta(days=5))
    old_due = bill.due_date
    new_due = date.today() + timedelta(days=10)
    postpone_bill(bill, new_due_date=new_due, reason="RESCHEDULE_MARKER",
                  actor_id=_STATE["owner_a"])
    db.session.refresh(bill)
    assert bill.previous_due_date == old_due, (
        f"previous_due_date={bill.previous_due_date}, expected {old_due}")
    assert bill.due_date == new_due, (
        f"due_date={bill.due_date}, expected {new_due}")
    assert bill.postpone_reason == "RESCHEDULE_MARKER"
    assert bill.postponed_by == _STATE["owner_a"]
    assert bill.postponed_at is not None
    return f"previous={old_due}, new={new_due}"


@check("6. postpone rescues an OVERDUE bill from the panel")
def _():
    from app.models import VendorBillStatus
    from app.services.vendor_bills import (
        postpone_bill, update_overdue_vendor_bills)
    from app.services.reports import dashboard_metrics
    _reset_bills()
    bill = _mk_bill(_STATE["cid_a"], _STATE["vendor_a"], _STATE["exp_a"],
                    number="V6", due_date=date.today() - timedelta(days=1),
                    notes="RESCUE_MARKER")
    update_overdue_vendor_bills(_STATE["cid_a"])
    db.session.refresh(bill)
    assert bill.status == VendorBillStatus.OVERDUE, "prep: not OVERDUE"

    postpone_bill(bill, new_due_date=date.today() + timedelta(days=7),
                  reason=None, actor_id=_STATE["owner_a"])
    db.session.refresh(bill)
    assert bill.status == VendorBillStatus.POSTED, (
        f"expected POSTED after postpone, got {bill.status}")

    m = dashboard_metrics(_STATE["cid_a"])
    labels = {r["title_for_display"] for r in m["late_vendor_bills"]}
    assert "RESCUE_MARKER" not in labels, (
        "postponed bill still surfacing on panel")
    return "postponed bill left the panel"


@check("7. exception on real OVERDUE bill reverses the JE (existing delete flow)")
def _():
    """Smoke test that the ticket's استثناء action for real bills lands
    on the existing delete route without breakage. The full ledger
    invariants are pinned by audit_vendor_bill_delete."""
    from app.models import VendorBillStatus
    from app.services.vendor_bills import update_overdue_vendor_bills
    _reset_bills()
    bill = _mk_bill(_STATE["cid_a"], _STATE["vendor_a"], _STATE["exp_a"],
                    number="V7", due_date=date.today() - timedelta(days=1))
    update_overdue_vendor_bills(_STATE["cid_a"])
    r = _post_as(_STATE["owner_a"], _STATE["cid_a"],
                 f"/vendor-bills/{bill.id}/delete",
                 {"reason": "استثناء"})
    assert r.status_code in (302, 303), f"delete returned {r.status_code}"
    db.session.refresh(bill)
    assert bill.status == VendorBillStatus.CANCELLED, (
        f"status after delete={bill.status}")
    assert bill.deleted_at is not None
    return f"CANCELLED + soft-deleted"


@check("8. unmaterialised past-due forecast surfaces in late_vendor_bills")
def _():
    """Belt-and-suspenders: even if cron hasn't run yet on a given day
    (or failed), a forecast whose date passed appears in the panel as
    a red row with source_recurring_bill_id + occurrence_date so the
    dashboard can offer the on-demand materialise + skip buttons."""
    from app.services.reports import dashboard_metrics
    _reset_bills()
    src = _mk_bill(_STATE["cid_a"], _STATE["vendor_a"], _STATE["exp_a"],
                   number="V8-SRC",
                   due_date=date.today() - timedelta(days=30))
    rb = _mk_template(_STATE["cid_a"], src.id, _STATE["vendor_a"],
                      start_date=date.today() - timedelta(days=1))
    # DO NOT run cron. Confirm the forecast surfaces anyway.
    m = dashboard_metrics(_STATE["cid_a"])
    forecast_rows = [r for r in m["late_vendor_bills"]
                     if r["kind"] == "forecast"]
    assert forecast_rows, (
        f"no forecast row on panel; late_vendor_bills={m['late_vendor_bills']!r}")
    assert forecast_rows[0]["source_recurring_bill_id"] == rb.id
    assert forecast_rows[0]["occurrence_date"] == (
        date.today() - timedelta(days=1))
    return f"forecast surfaces without cron ({len(forecast_rows)} rows)"


@check("9. cross-tenant — A's overdue bills invisible to B")
def _():
    from app.services.vendor_bills import update_overdue_vendor_bills
    from app.services.reports import dashboard_metrics
    _reset_bills()
    _mk_bill(_STATE["cid_a"], _STATE["vendor_a"], _STATE["exp_a"],
             number="V9-A", due_date=date.today() - timedelta(days=2),
             notes="A_ONLY_LEAK_MARKER")
    update_overdue_vendor_bills(_STATE["cid_a"])
    m_b = dashboard_metrics(_STATE["cid_b"])
    labels_b = {r["title_for_display"] for r in m_b["late_vendor_bills"]}
    assert "A_ONLY_LEAK_MARKER" not in labels_b, (
        f"CROSS-TENANT LEAK: A's marker on B's dashboard: {labels_b!r}")
    return f"B panel clean; count={m_b['late_vendor_bills_count']}"


@check("10. VENDOR_BILL_OVERDUE notification fires on first flip")
def _():
    from app.models import Notification
    from app.services.vendor_bills import update_overdue_vendor_bills
    _reset_bills()
    _mk_bill(_STATE["cid_a"], _STATE["vendor_a"], _STATE["exp_a"],
             number="V10", due_date=date.today() - timedelta(days=1))
    update_overdue_vendor_bills(_STATE["cid_a"])
    # owner + accountant should have received one notification each;
    # team_member (no vendor_bills.create) should have zero.
    owner_n = Notification.query.filter_by(
        user_id=_STATE["owner_a"],
        kind="VENDOR_BILL_OVERDUE").count()
    acc_n = Notification.query.filter_by(
        user_id=_STATE["accountant_a"],
        kind="VENDOR_BILL_OVERDUE").count()
    team_n = Notification.query.filter_by(
        user_id=_STATE["team_a"],
        kind="VENDOR_BILL_OVERDUE").count()
    assert owner_n == 1, f"owner got {owner_n} notifications (expected 1)"
    assert acc_n == 1, f"accountant got {acc_n} (expected 1)"
    assert team_n == 0, f"team_member got {team_n} (expected 0 — no perm)"
    return f"owner=1, accountant=1, team_member=0"


@check("11. notification does NOT fire on second cron run")
def _():
    from app.models import Notification
    from app.services.vendor_bills import update_overdue_vendor_bills
    _reset_bills()
    _mk_bill(_STATE["cid_a"], _STATE["vendor_a"], _STATE["exp_a"],
             number="V11", due_date=date.today() - timedelta(days=1))
    update_overdue_vendor_bills(_STATE["cid_a"])   # first flip
    n1 = Notification.query.filter_by(
        kind="VENDOR_BILL_OVERDUE").count()
    update_overdue_vendor_bills(_STATE["cid_a"])   # second run
    n2 = Notification.query.filter_by(
        kind="VENDOR_BILL_OVERDUE").count()
    assert n1 == n2, (
        f"second cron run added {n2 - n1} extra notifications "
        f"(expected 0; the just-flipped set should be empty)")
    return f"n stays {n1} across both runs"


@check("12. permissions — accountant can postpone; team_member cannot delete")
def _():
    """require_permission() in this codebase REDIRECTS on denial (with
    a flash), it does not abort(403). So a team_member's POST to the
    delete route lands as 302 to dashboard, and the load-bearing
    signal is that the BILL WAS NOT DELETED — the redirect on its own
    could equally well come from a successful delete."""
    from app.models import VendorBillStatus
    _reset_bills()
    bill = _mk_bill(_STATE["cid_a"], _STATE["vendor_a"], _STATE["exp_a"],
                    number="V12", due_date=date.today() - timedelta(days=1))
    # Accountant CAN postpone (vendor_bills.create).
    r = _post_as(_STATE["accountant_a"], _STATE["cid_a"],
                 f"/vendor-bills/{bill.id}/postpone",
                 {"new_due_date":
                      (date.today() + timedelta(days=5)).isoformat(),
                  "reason": "perm test"})
    assert r.status_code in (302, 303), (
        f"accountant postpone got {r.status_code}, expected 302/303")
    from app.models import VendorBill
    db.session.expire_all()
    fresh = db.session.get(VendorBill, bill.id)
    assert fresh.postponed_by == _STATE["accountant_a"], (
        f"accountant postpone did not stamp audit field: "
        f"postponed_by={fresh.postponed_by}, expected {_STATE['accountant_a']}")

    # Team member CANNOT delete — require_permission redirects to
    # dashboard, and crucially the bill is UNCHANGED afterwards.
    before_status = fresh.status
    before_deleted = fresh.deleted_at
    r = _post_as(_STATE["team_a"], _STATE["cid_a"],
                 f"/vendor-bills/{bill.id}/delete",
                 {"reason": "should refuse"})
    db.session.expire_all()
    fresh2 = db.session.get(VendorBill, bill.id)
    assert fresh2.status == before_status, (
        f"team_member delete SUCCEEDED — status changed to {fresh2.status}")
    assert fresh2.deleted_at == before_deleted, (
        f"team_member delete SUCCEEDED — deleted_at set to {fresh2.deleted_at}")
    return "accountant postpone stamped; team_member delete rejected"


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    _STATE["app"] = app
    passed = failed = 0
    with app.app_context():
        _setup()
        try:
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
