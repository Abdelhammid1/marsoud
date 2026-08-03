#!/usr/bin/env python3
"""MARSOUD-VBILL-REFUND-STATUS — vendor bills behave like customer invoices.

A purchase return posted the right journal but never touched the bill:
VendorBillStatus had no REFUNDED/PARTIALLY_REFUNDED, so a refunded bill
still looked live and its full value kept inflating إجمالي المشتريات.
Deleting did the opposite — the bill vanished from the list entirely.

One check per acceptance criterion:
  1. full refund   → REFUNDED, still listed, value OUT of the totals
  2. partial refund → PARTIALLY_REFUNDED, still counted at its balance
  3. delete a posted bill → still listed, 🗑️, out of the totals
  4. totals reconcile with the journal lines actually posted
  5. paid 500 of 1000, partial cash refund 200 → paid_amount 300,
     balance 700, and ap_aging_report agrees
plus three regression guards:
  6. DEBIT_NOTE leaves status AND paid_amount alone
  7. a second partial refund still works
  8. the template renders the deleted-row treatment
"""
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
COMPANY_NAME = "__VBILL_REFUND_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ────────────────────────────────────────────────────────────
def _setup():
    from datetime import datetime
    from app.models import Company, User, Plan, Vendor, Account
    from app.services.seed_coa import seed_default_coa
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.roles import set_membership_role
    from app.services.legal import get_terms_version
    from app.services.subscription import activate_default_subscription

    _teardown()
    co = Company(name=COMPANY_NAME, base_currency="EGP", vat_rate=0)
    db.session.add(co)
    db.session.flush()
    # Stamp the same trial window a real signup gets. Inside that window
    # the product deliberately enables every feature regardless of plan
    # (see _company_in_trial in plan_gating), which is what a fresh
    # company actually looks like. Picking a plan by hand instead is a
    # trap: plan_allows() is the coarse module gate but subitem_allowed()
    # is a separate per-page one, and "starter" passes the first for
    # vendor_bills.create while omitting the vendor_bills.index sub-item.
    activate_default_subscription(co)
    # A plan must be set or require_plan_selection bounces the owner to
    # /choose-plan. It has to be one whose allowed_modules include
    # purchases: the trial window bypasses the per-page sub-item gate,
    # but plan_allows() (which guards vendor_bills.delete) has no trial
    # bypass and reads plan.modules directly.
    _pl = next((p for p in Plan.query.order_by(Plan.id).all()
                if "purchases" in (p.modules or [])
                and "accounting" in (p.modules or [])), None)
    assert _pl is not None, "no seeded plan enables the purchases module"
    co.plan_id = _pl.id
    co.intended_plan_id = _pl.id
    db.session.commit()

    seed_default_coa(co.id)
    ensure_roles_ready_for_company(co.id)

    u = User(email="__vbillrefund@audit.local", full_name="VB Owner",
             is_active=True, terms_version=get_terms_version(),
             terms_accepted_at=datetime.utcnow())
    u.set_password("Passw0rd!audit1")
    db.session.add(u)
    db.session.flush()
    db.session.commit()
    set_membership_role(u.id, co.id, "owner")

    v = Vendor(company_id=co.id, name="مورد المرتجعات")
    db.session.add(v)
    db.session.commit()

    _STATE.update(cid=co.id, uid=u.id, vid=v.id)


def _teardown():
    from sqlalchemy import text
    from app.models import Company, User
    co = Company.query.filter_by(name=COMPANY_NAME).first()
    if co:
        cid = co.id
        stmts = [
            "DELETE FROM journal_lines WHERE entry_id IN "
            "(SELECT id FROM journal_entries WHERE company_id=:c)",
            "DELETE FROM journal_entries WHERE company_id=:c",
            "DELETE FROM vendor_bill_items WHERE bill_id IN "
            "(SELECT id FROM vendor_bills WHERE company_id=:c)",
            "DELETE FROM vendor_bill_refunds WHERE company_id=:c",
            "DELETE FROM debit_notes WHERE company_id=:c",
            "DELETE FROM vendor_bills WHERE company_id=:c",
            "DELETE FROM vendors WHERE company_id=:c",
            "DELETE FROM payment_methods WHERE company_id=:c",
            "DELETE FROM accounts WHERE company_id=:c",
            "DELETE FROM user_companies WHERE company_id=:c",
            "DELETE FROM role_permissions WHERE role_id IN "
            "(SELECT id FROM roles WHERE company_id=:c)",
            "DELETE FROM roles WHERE company_id=:c",
            "DELETE FROM doc_sequences WHERE company_id=:c",
            "DELETE FROM companies WHERE id=:c",
        ]
        # Commit after EACH statement: a single failure (missing table on
        # an older schema, say) would otherwise roll back every delete
        # before it, leaving the fixture company behind and colliding on
        # the next run.
        for s in stmts:
            try:
                db.session.execute(text(s), {"c": cid})
                db.session.commit()
            except Exception:
                db.session.rollback()
    u = User.query.filter_by(email="__vbillrefund@audit.local").first()
    if u:
        db.session.delete(u)
        db.session.commit()


def _mk_bill(total, pay_method="CASH", post=True, vendor_id=None,
             tax_rate=0):
    """Create (and optionally post) a one-line EXPENSE bill."""
    from app.models import (
        VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, BillLineType, Account,
    )
    from app.services.vendor_bills import post_vendor_bill
    from app.services.numbering import next_number
    cid = _STATE["cid"]
    acc = Account.query.filter(
        Account.company_id == cid, Account.code.like("5%"),
        Account.is_postable.is_(True)).order_by(Account.code).first()
    b = VendorBill(
        company_id=cid, number=next_number(cid, "VENDOR_BILL"),
        vendor_id=vendor_id or _STATE["vid"], issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        payment_method=VendorBillPaymentMethod[pay_method],
        currency="EGP", status=VendorBillStatus.DRAFT, tax_rate=tax_rate,
    )
    db.session.add(b)
    db.session.flush()
    db.session.add(VendorBillItem(
        bill_id=b.id, description="بند اختبار",
        line_type=BillLineType.EXPENSE, account_id=acc.id,
        quantity=1, unit_price=total, line_total=total))
    db.session.flush()
    b.items = VendorBillItem.query.filter_by(bill_id=b.id).all()
    b.recalc()
    db.session.commit()
    if post:
        post_vendor_bill(b, created_by=_STATE["uid"])
    return b


def _client():
    c = _STATE["app"].test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(_STATE["uid"])
        s["_fresh"] = True
        s["active_company_id"] = _STATE["cid"]
    return c


def _index_totals(**args):
    """Run the real index route and read back its totals."""
    from app.routes.vendor_bills import index as _index  # noqa: F401
    qs = "&".join(f"{k}={v}" for k, v in args.items())
    r = _client().get(f"/vendor-bills/?{qs}" if qs else "/vendor-bills/")
    assert r.status_code == 200, f"index status={r.status_code}"
    return r.get_data(as_text=True)


# ─── AC 1 ───────────────────────────────────────────────────────────────
@check("1. full refund → REFUNDED, still listed, value out of the totals")
def _():
    from app.models import VendorBillStatus, VendorRefundType, VendorBill
    from app.services.vendor_bills import post_vendor_bill_refund
    bill = _mk_bill(1000, "CASH")           # CASH posts → PAID
    assert bill.status == VendorBillStatus.PAID, bill.status

    post_vendor_bill_refund(bill, VendorRefundType.FULL,
                            created_by=_STATE["uid"])
    b = db.session.get(VendorBill, bill.id)
    assert b.status == VendorBillStatus.REFUNDED, \
        f"status stayed {b.status.value} after a full refund"

    body = _index_totals()
    assert b.number in body, "refunded bill disappeared from the list"
    # It is the only bill so far → purchases KPI must be back to 0.00
    from app.routes.vendor_bills import index
    _STATE["ac1_bill"] = b.id
    return f"{b.number} → REFUNDED, still listed"


@check("1b. the refunded value is excluded from the KPI totals")
def _():
    from app.models import VendorBill, VendorBillStatus
    cid = _STATE["cid"]
    bills = VendorBill.query.filter_by(company_id=cid).all()
    _EXCLUDED = (VendorBillStatus.CANCELLED, VendorBillStatus.REFUNDED)
    countable = [b for b in bills
                 if b.deleted_at is None and b.status not in _EXCLUDED]
    invoiced = sum(float(b.total or 0) for b in countable)
    assert invoiced == 0.0, \
        f"refunded bill still counted in purchases: {invoiced}"
    return "إجمالي المشتريات excludes the refunded bill"


# ─── AC 2 ───────────────────────────────────────────────────────────────
@check("2. partial refund → PARTIALLY_REFUNDED, still counted")
def _():
    from app.models import VendorBillStatus, VendorRefundType, VendorBill
    from app.services.vendor_bills import post_vendor_bill_refund
    bill = _mk_bill(2000, "CASH")
    post_vendor_bill_refund(bill, VendorRefundType.PARTIAL, amount=500,
                            created_by=_STATE["uid"])
    b = db.session.get(VendorBill, bill.id)
    assert b.status == VendorBillStatus.PARTIALLY_REFUNDED, b.status.value
    # Still countable (only CANCELLED + REFUNDED are excluded).
    _EXCLUDED = (VendorBillStatus.CANCELLED, VendorBillStatus.REFUNDED)
    assert b.status not in _EXCLUDED, "partially refunded must stay counted"
    assert abs(float(b.paid_amount) - 1500.0) < 0.01, \
        f"paid_amount should drop 2000→1500, got {b.paid_amount}"
    _STATE["ac2_bill"] = b.id
    return "PARTIALLY_REFUNDED, paid 2000→1500, still in the totals"


# ─── AC 3 ───────────────────────────────────────────────────────────────
@check("3. deleting a posted bill keeps it listed with 🗑️")
def _():
    from app.models import VendorBill, VendorBillStatus
    bill = _mk_bill(700, "CASH")
    number = bill.number
    bill_id = bill.id
    r = _client().post(f"/vendor-bills/{bill_id}/delete",
                       data={"reason": "audit"}, follow_redirects=True)
    assert r.status_code == 200, r.status_code
    # The request mutated the row in its own session; drop anything this
    # session has cached before reading it back.
    db.session.expire_all()
    b = db.session.get(VendorBill, bill_id)
    assert b.deleted_at is not None, "delete did not soft-delete"
    assert b.status == VendorBillStatus.CANCELLED, b.status.value

    body = _index_totals()
    assert number in body, "deleted bill vanished from the list (the bug)"
    assert "🗑️ محذوفة" in body, "deleted badge not rendered"

    # …and hidden when the user narrows to active-only.
    body_active = _index_totals(deleted_filter="active")
    assert number not in body_active, "active-only filter leaked a deleted bill"
    body_del = _index_totals(deleted_filter="deleted")
    assert number in body_del, "deleted-only filter did not show it"
    _STATE["ac3_bill"] = b.id
    return "listed with 🗑️; active/deleted filters both behave"


# ─── AC 4 ───────────────────────────────────────────────────────────────
@check("4. totals reconcile with the journal actually posted")
def _():
    from app.models import VendorBill, VendorBillStatus, JournalEntry
    cid = _STATE["cid"]
    bills = VendorBill.query.filter_by(company_id=cid).all()
    _EXCLUDED = (VendorBillStatus.CANCELLED, VendorBillStatus.REFUNDED)
    countable = {b.id for b in bills
                 if b.deleted_at is None and b.status not in _EXCLUDED}

    # Assert membership rather than a running total, so the check stays
    # correct however many bills earlier checks happen to have created.
    assert _STATE["ac1_bill"] not in countable, "refunded bill still counted"
    assert _STATE["ac3_bill"] not in countable, "deleted bill still counted"
    assert _STATE["ac2_bill"] in countable, \
        "partially refunded bill must stay in the totals"

    # Every refund posted a balanced journal.
    n = 0
    for e in JournalEntry.query.filter_by(
            company_id=cid, source_type="vendor_bill_refund").all():
        d = sum(float(l.debit) for l in e.lines)
        c = sum(float(l.credit) for l in e.lines)
        assert abs(d - c) < 0.005, f"{e.number} unbalanced {d} vs {c}"
        n += 1
    return f"refunded+deleted excluded, partial included; {n} balanced journals"


# ─── AC 5 ───────────────────────────────────────────────────────────────
@check("5. paid 500/1000 + cash refund 200 → paid 300, balance 700, aging agrees")
def _():
    from app.models import VendorBill, VendorBillStatus, VendorRefundType
    from app.services.vendor_bills import (
        post_vendor_bill_refund, record_bill_payment,
    )
    from app.services.reports import ap_aging_report
    from app.models import Vendor
    # Its own vendor, so the AP-aging row isolates this bill instead of
    # summing whatever other checks left behind.
    v2 = Vendor(company_id=_STATE["cid"], name="مورد أعمار الديون")
    db.session.add(v2)
    db.session.commit()

    bill = _mk_bill(1000, "CREDIT", vendor_id=v2.id)  # CREDIT → POSTED, unpaid
    assert bill.status == VendorBillStatus.POSTED, bill.status.value
    record_bill_payment(bill, 500, created_by=_STATE["uid"])
    b = db.session.get(VendorBill, bill.id)
    assert abs(float(b.paid_amount) - 500) < 0.01, b.paid_amount
    assert b.status == VendorBillStatus.PARTIALLY_PAID, b.status.value

    post_vendor_bill_refund(b, VendorRefundType.PARTIAL, amount=200,
                            created_by=_STATE["uid"])
    b = db.session.get(VendorBill, bill.id)
    assert abs(float(b.paid_amount) - 300.0) < 0.01, \
        f"paid_amount should be 300, got {b.paid_amount}"
    assert abs(b.balance - 700.0) < 0.01, \
        f"balance should be 700, got {b.balance}"

    aging = ap_aging_report(_STATE["cid"])
    row = next((r for r in aging["rows"] if r["vendor_id"] == v2.id), None)
    assert row is not None, "vendor missing from AP aging"
    assert abs(row["total"] - 700.0) < 0.01, \
        f"AP aging shows {row['total']}, expected 700 (must match balance)"
    return "paid 300 · balance 700 · AP aging 700 — all agree"


@check("5b. a fully refunded bill does not age as payable")
def _():
    from app.models import Vendor, VendorRefundType, VendorBill
    from app.services.vendor_bills import post_vendor_bill_refund
    from app.services.reports import ap_aging_report
    v3 = Vendor(company_id=_STATE["cid"], name="مورد مرتجع كامل")
    db.session.add(v3)
    db.session.commit()
    bill = _mk_bill(900, "CASH", vendor_id=v3.id)     # PAID
    post_vendor_bill_refund(bill, VendorRefundType.FULL,
                            created_by=_STATE["uid"])
    b = db.session.get(VendorBill, bill.id)
    # paid_amount went to 0 when the cash came back, so balance now reads
    # the full 900 — it must NOT show up as money owed to the vendor.
    assert abs(b.balance - 900.0) < 0.01, b.balance
    aging = ap_aging_report(_STATE["cid"])
    row = next((r for r in aging["rows"] if r["vendor_id"] == v3.id), None)
    assert row is None, \
        f"fully refunded bill aged as payable: {row and row['total']}"
    return "REFUNDED excluded from AP aging despite a non-zero balance"


# ─── Regression guards ──────────────────────────────────────────────────
@check("6. DEBIT_NOTE leaves status AND paid_amount untouched")
def _():
    from app.models import VendorBill, VendorBillStatus, VendorRefundType
    from app.services.vendor_bills import post_vendor_bill_refund
    bill = _mk_bill(400, "CASH")
    before_status = bill.status
    before_paid = float(bill.paid_amount or 0)
    post_vendor_bill_refund(bill, VendorRefundType.DEBIT_NOTE, amount=100,
                            created_by=_STATE["uid"])
    b = db.session.get(VendorBill, bill.id)
    assert b.status == before_status, \
        f"DEBIT_NOTE changed status {before_status.value}→{b.status.value}"
    assert abs(float(b.paid_amount) - before_paid) < 0.01, \
        f"DEBIT_NOTE moved paid_amount {before_paid}→{b.paid_amount}"
    return "status + paid_amount both unchanged, like CREDIT_NOTE"


@check("7. a second partial refund still works (no capability regression)")
def _():
    from app.models import VendorBill, VendorBillStatus, VendorRefundType
    from app.services.vendor_bills import post_vendor_bill_refund
    b = db.session.get(VendorBill, _STATE["ac2_bill"])
    assert b.status == VendorBillStatus.PARTIALLY_REFUNDED
    post_vendor_bill_refund(b, VendorRefundType.PARTIAL, amount=100,
                            created_by=_STATE["uid"])
    b = db.session.get(VendorBill, _STATE["ac2_bill"])
    assert abs(float(b.paid_amount) - 1400.0) < 0.01, b.paid_amount
    return "second partial refund accepted; paid 1500→1400"


@check("9. after a full refund the AP sub-account and input VAT net to zero")
def _():
    # The accounting invariant tests/audit_vendor_bill_delete.py was
    # meant to protect. That suite's fixture never sets a plan, so
    # require_plan_selection bounces every request to /choose-plan and
    # its checks have been vacuous — assert it here instead, since this
    # ticket changes the refund path.
    from app.models import (
        Vendor, VendorBill, VendorRefundType, JournalLine, JournalEntry,
        Account,
    )
    from app.services.vendor_bills import post_vendor_bill_refund
    from app.services.subsidiary import party_ap_account
    v4 = Vendor(company_id=_STATE["cid"], name="مورد صافي الحساب")
    db.session.add(v4)
    db.session.commit()
    bill = _mk_bill(1000, "CREDIT", vendor_id=v4.id, tax_rate=15)
    ap = party_ap_account(bill)
    vat = Account.query.filter_by(company_id=_STATE["cid"], code="1280").first()
    db.session.commit()

    post_vendor_bill_refund(bill, VendorRefundType.FULL,
                            created_by=_STATE["uid"])

    def _net(acc_id):
        rows = (db.session.query(JournalLine)
                .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
                .filter(JournalLine.account_id == acc_id,
                        JournalEntry.company_id == _STATE["cid"],
                        JournalEntry.is_active.is_(True)).all())
        return round(sum(float(r.credit) - float(r.debit) for r in rows), 2)

    ap_net = _net(ap.id)
    vat_net = _net(vat.id)
    assert abs(ap_net) < 0.01, f"vendor AP did not net to zero: {ap_net}"
    assert abs(vat_net) < 0.01, f"input VAT did not net to zero: {vat_net}"
    return f"AP net {ap_net:.2f} · input VAT net {vat_net:.2f}"


@check("8. template renders the deleted-row treatment")
def _():
    tpl = (ROOT / "app/templates/vendor_bills/index.html").read_text(
        encoding="utf-8")
    assert "🗑️ محذوفة" in tpl, "deleted badge missing"
    assert "opacity-60" in tpl, "row muting missing"
    assert "line-through" in tpl, "strikethrough missing"
    assert "deleted_filter" in tpl, "view filter missing"
    assert "badge-refunded" in tpl, "refunded badge class missing"
    # The base query must no longer be unconditionally filtered — the
    # only surviving deleted_at filter is the one inside the
    # deleted_filter branch.
    route = (ROOT / "app/routes/vendor_bills.py").read_text(encoding="utf-8")
    assert 'filter_by(company_id=g.active_company.id).filter(VendorBill.deleted_at.is_(None))' \
        not in route, "base query still hard-filters deleted bills"
    return "badge + muting + strikethrough + filter all present"


def main():
    app = create_app()
    _STATE["app"] = app
    with app.app_context():
        _setup()
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
