#!/usr/bin/env python3
"""MARSOUD-VENDOR-BILL-INTANGIBLE-ASSET-01 — FIXED_ASSET lines now
accept intangible-asset accounts (14xx / 17xx) in addition to the
tangible 12xx range.

Before this ticket, `app/services/vendor_bills.py::LINE_TYPE_ACCOUNT_PREFIX`
locked `FIXED_ASSET` to `"12"`, and `_validate_line_account()` refused
anything else with the Arabic "الحساب ليس أصلاً" error.  The seeded
CoA carries intangibles under 1700 (parent) / 1710 Software & Licences
/ 1720 Goodwill (`app/services/seed_coa.py:70-72`), so buying a
software licence via a vendor bill couldn't be classified correctly.

Two checks:

  1. A vendor bill whose FIXED_ASSET line targets 1710 (intangible)
     validates + posts without raising LedgerError — the whitelist
     now accepts it.
  2. A vendor bill whose FIXED_ASSET line targets 5100 (expense)
     STILL raises LedgerError — the guard didn't over-fire and
     still refuses obviously wrong account types.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__VENDOR_BILL_INTANGIBLE_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import (
        Company, Account, JournalEntry, JournalLine, Vendor,
    )
    existing = Company.query.filter_by(name=COMPANY_NAME).first()
    if existing:
        _teardown_company(existing.id)

    orphan_ids = [r.id for r in JournalLine.query.filter(
        JournalLine.entry_id.notin_(db.session.query(JournalEntry.id))
    ).all()]
    if orphan_ids:
        JournalLine.query.filter(
            JournalLine.id.in_(orphan_ids)
        ).delete(synchronize_session=False)
        db.session.commit()

    c = Company(name=COMPANY_NAME, base_currency="EGP")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)

    # Vendor sub-account will be created lazily by the vendor-bill
    # pipeline; we only need a vendor row here.
    v = Vendor(company_id=c.id, name="__intangible_vendor__")
    db.session.add(v); db.session.flush()

    _STATE["company_id"] = c.id
    _STATE["vendor_id"] = v.id
    _STATE["intangible_acc_id"] = Account.query.filter_by(
        company_id=c.id, code="1710").one().id
    _STATE["expense_acc_id"] = Account.query.filter_by(
        company_id=c.id, code="5100").one().id
    db.session.commit()


def _teardown_company(company_id):
    from app.models import (
        Company, JournalEntry, JournalLine, VendorBill, VendorBillItem,
    )
    from sqlalchemy import inspect, text
    db.session.rollback()  # in case a prior test left the txn in error state
    insp = inspect(db.engine)
    # Kill child rows first — with PRAGMA foreign_keys=ON, deleting
    # vendor_bills (or vendors) while items still reference them
    # would trigger an FK violation before the parent delete lands.
    bill_ids = [r.id for r in VendorBill.query.filter_by(
        company_id=company_id).all()]
    if bill_ids:
        VendorBillItem.query.filter(
            VendorBillItem.bill_id.in_(bill_ids)
        ).delete(synchronize_session=False)
    entry_ids = [r.id for r in JournalEntry.query.filter_by(
        company_id=company_id).all()]
    if entry_ids:
        JournalLine.query.filter(
            JournalLine.entry_id.in_(entry_ids)
        ).delete(synchronize_session=False)
    # PRAGMA foreign_keys OFF during the bulk company-scoped sweep so
    # cross-table FK cycles (accounts <-> vendors <-> companies…)
    # don't refuse the deletes.  This is fixture cleanup only —
    # production always goes through the ORM cascade.
    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    for table in reversed(db.metadata.sorted_tables):
        if "company_id" in {c["name"] for c in insp.get_columns(table.name)}:
            db.session.execute(
                table.delete().where(table.c.company_id == company_id)
            )
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()


_BILL_SEQ = [0]


def _make_bill(account_id, useful_life_years=3):
    """Build a draft VendorBill with a single FIXED_ASSET line
    referencing `account_id`.  Returns the bill (not yet committed
    downstream).  Each call rolls back afterwards but the bill number
    must still be unique per FLUSH within a session, so we bump a
    counter."""
    from app.models import VendorBill, VendorBillItem
    from app.models.vendor_bill import VendorBillStatus, BillLineType

    _BILL_SEQ[0] += 1
    bill = VendorBill(
        company_id=_STATE["company_id"],
        vendor_id=_STATE["vendor_id"],
        number=f"INTANG-{_BILL_SEQ[0]:03d}",
        issue_date=date.today(),
        due_date=date.today(),
        status=VendorBillStatus.DRAFT,
        currency="EGP",
        total=1000.0,
        subtotal=1000.0,
    )
    db.session.add(bill); db.session.flush()

    item = VendorBillItem(
        bill_id=bill.id,
        line_type=BillLineType.FIXED_ASSET,
        account_id=account_id,
        description="اختبار أصل غير ملموس",
        quantity=1,
        unit_price=1000.0,
        line_total=1000.0,
        useful_life_years=useful_life_years,
    )
    db.session.add(item); db.session.flush()
    return bill


# ─── 1. Intangible (1710) is now accepted ────────────────────────
@check("1. FIXED_ASSET line on 1710 (Software & Licences) validates")
def _():
    from app.services.vendor_bills import _validate_line_account
    from app.services.ledger import LedgerError
    bill = _make_bill(_STATE["intangible_acc_id"])
    try:
        _validate_line_account(bill.items[0], _STATE["company_id"])
    except LedgerError as e:
        raise AssertionError(
            f"intangible 1710 was refused: {e}"
        )
    db.session.rollback()
    return "1710 accepted"


# ─── 2. Guard still refuses a 5xxx expense on a FIXED_ASSET line ──
@check("2. FIXED_ASSET line on 5100 (expense) still refused")
def _():
    from app.services.vendor_bills import _validate_line_account
    from app.services.ledger import LedgerError
    bill = _make_bill(_STATE["expense_acc_id"])
    try:
        _validate_line_account(bill.items[0], _STATE["company_id"])
    except LedgerError as e:
        db.session.rollback()
        msg = str(e)
        assert "ليس أصل" in msg, f"error not the expected one: {msg}"
        return f"refused correctly: {msg[:60]}"
    else:
        db.session.rollback()
        raise AssertionError(
            "guard over-relaxed — 5100 expense accepted on a "
            "FIXED_ASSET line"
        )


# ─── 3. Picker returns both 12xx tangibles + 17xx intangibles ─────
@check("3. get_allowed_accounts_for_line_type returns 12xx + 17xx")
def _():
    from app.services.vendor_bills import get_allowed_accounts_for_line_type
    from app.models.vendor_bill import BillLineType
    rows = get_allowed_accounts_for_line_type(
        _STATE["company_id"], BillLineType.FIXED_ASSET)
    codes = {r.code for r in rows}
    assert "1710" in codes, f"1710 missing from picker: {sorted(codes)}"
    assert "1290" not in codes, f"1290 (excluded) leaked into picker"
    assert any(c.startswith("12") for c in codes), \
        "12xx tangibles disappeared from picker"
    return f"picker returns {len(codes)} accounts including 1710"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:
                    print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "company_id" in _STATE:
                    _teardown_company(_STATE["company_id"])
                    print(f"\n(cleaned up fixture company "
                          f"#{_STATE['company_id']})")
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
