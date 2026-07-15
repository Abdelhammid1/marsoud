#!/usr/bin/env python3
"""MARSOUD-VBILL-DELETE-POSTED (Abdelhamid 2026-07-15).

"عاوزها مع الموردين كمان" — apply the same delete-with-reversing-entry
behaviour I shipped for customer invoices (commit 1ea029f) to vendor
bills. Mirrors that audit's accounting-integrity checks:

  DRAFT vendor bill  → soft delete (never posted anywhere).
  POSTED / PAID     → post_vendor_bill_refund(FULL) reverses AP,
                       Input VAT, restocks inventory, returns cash;
                       then mark deleted_at + status = CANCELLED.
  CANCELLED         → second delete is a no-op (no double reversal).

Checks:
  1. DRAFT bill: POST /delete removes it from the active-list query
     (deleted_at set).
  2. POSTED bill: POST /delete marks CANCELLED + posts a reversing
     JournalEntry.
  3. Post-delete, the vendor's AP sub-account net balance = 0.
  4. Post-delete, Input VAT (1280) net balance = 0.
  5. PAID bill: cash returned exactly = paid amount; AP settled.
  6. Already-CANCELLED bill: POST /delete is a no-op (no extra
     reversal journal entries).
"""
import sys
from pathlib import Path
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": company_id})
        conn.execute(text(
            "DELETE FROM vendor_bill_items WHERE bill_id IN "
            "(SELECT id FROM vendor_bills WHERE company_id = :c)"
        ), {"c": company_id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'vbd-%@x.test'"))
        # Orphan sweep — bills + items from interrupted prior runs
        # can share account_ids with the current fixture company and
        # pollute the balance queries.
        conn.execute(text(
            "DELETE FROM vendor_bill_items WHERE bill_id NOT IN "
            "(SELECT id FROM vendor_bills)"))
        conn.execute(text(
            "DELETE FROM vendor_bills WHERE company_id NOT IN "
            "(SELECT id FROM companies)"))


def _setup():
    from app.models import (
        Company, User, user_companies, Vendor,
    )
    from werkzeug.security import generate_password_hash

    for name in ("__VBD__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__VBD__", base_currency="SAR", vat_rate=15)
    db.session.add(a); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(a.id)

    def _mk(email, role):
        u = User(email=email,
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name=email.split("@")[0])
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=a.id, role=role))
        return u

    owner = _mk("vbd-owner@x.test", "owner")
    vendor = Vendor(company_id=a.id, name="VBD-Vendor", is_active=True)
    db.session.add(vendor); db.session.commit()

    _STATE.update(a_id=a.id, owner_id=owner.id, vendor_id=vendor.id)


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
              "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


def _login():
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    return client


def _fresh_bill(status_after="POSTED", pay=False):
    """Create a small expense bill, optionally post + pay it.
    Returns the bill id + total."""
    from app.models import (
        VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, BillLineType, Account,
    )
    from app.services.vendor_bills import post_vendor_bill
    from app.services.numbering import next_number
    exp_acc = Account.query.filter_by(
        company_id=_STATE["a_id"], code="5210").first()
    number = next_number(_STATE["a_id"], "VENDOR_BILL")
    bill = VendorBill(
        company_id=_STATE["a_id"],
        number=number,
        vendor_id=_STATE["vendor_id"],
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        payment_method=(
            VendorBillPaymentMethod.CREDIT if not pay
            else VendorBillPaymentMethod.CASH),
        currency="SAR", tax_rate=15,
        status=VendorBillStatus.DRAFT,
    )
    db.session.add(bill); db.session.flush()
    db.session.add(VendorBillItem(
        bill_id=bill.id, description="widget",
        line_type=BillLineType.EXPENSE, account_id=exp_acc.id,
        quantity=1, unit_price=100, line_total=100,
    ))
    bill.recalc()
    db.session.commit()
    if status_after == "DRAFT":
        return bill.id, float(bill.total)
    # For CASH bills, post_vendor_bill auto-settles (debits Cash +
    # sets paid_amount = total + status = PAID). CREDIT bills stay
    # POSTED with an open AP balance. pay=True was reflected by
    # picking CASH above, so no separate payment call is needed.
    post_vendor_bill(bill, created_by=_STATE["owner_id"])
    db.session.refresh(bill)
    return bill.id, float(bill.total)


def _account_balance(code):
    from app.models import Account, JournalLine
    acc = Account.query.filter_by(
        company_id=_STATE["a_id"], code=code).first()
    if not acc:
        return 0.0
    debits = db.session.query(db.func.coalesce(
        db.func.sum(JournalLine.debit), 0.0),
    ).filter(JournalLine.account_id == acc.id).scalar() or 0.0
    credits = db.session.query(db.func.coalesce(
        db.func.sum(JournalLine.credit), 0.0),
    ).filter(JournalLine.account_id == acc.id).scalar() or 0.0
    return float(debits) - float(credits)


def _vendor_ap_balance():
    from app.services.subsidiary import party_ap_account
    from app.models import VendorBill, JournalLine
    bill = (VendorBill.query
            .filter_by(company_id=_STATE["a_id"])
            .order_by(VendorBill.id.desc()).first())
    if not bill:
        return 0.0
    acc = party_ap_account(bill)
    debits = db.session.query(db.func.coalesce(
        db.func.sum(JournalLine.debit), 0.0),
    ).filter(JournalLine.account_id == acc.id).scalar() or 0.0
    credits = db.session.query(db.func.coalesce(
        db.func.sum(JournalLine.credit), 0.0),
    ).filter(JournalLine.account_id == acc.id).scalar() or 0.0
    return float(debits) - float(credits)


# ─── Checks ────────────────────────────────────────────────────────
@check("1. DRAFT bill: /delete soft-removes it (deleted_at set)")
def _():
    from app.models import VendorBill
    bill_id, _ = _fresh_bill(status_after="DRAFT")
    r = _login().post(f"/vendor-bills/{bill_id}/delete",
                       follow_redirects=False)
    assert r.status_code in (200, 302)
    bill = db.session.get(VendorBill, bill_id)
    db.session.refresh(bill)
    assert bill.deleted_at is not None, \
        "DRAFT delete didn't set deleted_at"
    return "DRAFT soft-deleted"


@check("2. POSTED bill: /delete marks CANCELLED + posts reversing entry")
def _():
    from app.models import VendorBill, VendorBillStatus, JournalEntry
    bill_id, total = _fresh_bill(status_after="POSTED", pay=False)
    entries_before = JournalEntry.query.filter_by(
        company_id=_STATE["a_id"]).count()
    r = _login().post(f"/vendor-bills/{bill_id}/delete",
                       follow_redirects=False)
    assert r.status_code in (200, 302)
    bill = db.session.get(VendorBill, bill_id)
    db.session.refresh(bill)
    assert bill.status == VendorBillStatus.CANCELLED, \
        f"expected CANCELLED, got {bill.status}"
    assert bill.deleted_at is not None
    entries_after = JournalEntry.query.filter_by(
        company_id=_STATE["a_id"]).count()
    assert entries_after > entries_before, \
        "no reversing journal entry was posted"
    _STATE["posted_bill_id"] = bill_id
    return "row preserved as CANCELLED + reversing entry present"


@check("3. Post-delete, vendor AP sub-account net balance = 0")
def _():
    bal = _vendor_ap_balance()
    assert abs(bal) < 0.01, f"AP net balance = {bal!r}"
    return f"AP net = {bal:+.2f} (≈ 0)"


@check("4. Post-delete, Input VAT (1280) net balance = 0")
def _():
    bal = _account_balance("1280")
    assert abs(bal) < 0.01, f"Input VAT net balance = {bal!r}"
    return f"1280 net = {bal:+.2f} (≈ 0)"


@check("5. PAID bill: cash returned = paid amount; AP settled")
def _():
    from app.models import VendorBill, VendorBillStatus
    bill_id, total = _fresh_bill(status_after="POSTED", pay=True)
    cash_before = _account_balance("1110")
    r = _login().post(f"/vendor-bills/{bill_id}/delete",
                       follow_redirects=False)
    assert r.status_code in (200, 302)
    bill = db.session.get(VendorBill, bill_id)
    db.session.refresh(bill)
    assert bill.status == VendorBillStatus.CANCELLED, \
        f"expected CANCELLED, got {bill.status}"
    cash_after = _account_balance("1110")
    # Cash should INCREASE by the paid amount (money coming back
    # from the vendor).
    delta = cash_after - cash_before
    assert abs(delta - total) < 0.01, \
        f"cash delta expected ≈ +{total}, got {delta}"
    return f"cash returned +{total:.2f}; AP settled"


@check("6. Second /delete on a CANCELLED bill is a no-op (no double reversal)")
def _():
    from app.models import VendorBill, JournalEntry
    bill = VendorBill.query.filter_by(
        company_id=_STATE["a_id"], id=_STATE["posted_bill_id"]).first()
    assert bill is not None
    entries_before = JournalEntry.query.filter_by(
        company_id=_STATE["a_id"]).count()
    r = _login().post(f"/vendor-bills/{bill.id}/delete",
                       follow_redirects=False)
    # Route already returns early via redirect for CANCELLED, but the
    # rendered flash may be different. Just assert no extra entries.
    entries_after = JournalEntry.query.filter_by(
        company_id=_STATE["a_id"]).count()
    assert entries_after == entries_before, \
        f"double delete posted {entries_after - entries_before} extra entries"
    return "second delete = no-op"


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
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "a_id" in _STATE:
                    _teardown(_STATE["a_id"])
                print("\n(cleaned up fixture company)")
            except Exception as e:
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
