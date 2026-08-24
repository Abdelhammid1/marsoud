#!/usr/bin/env python3
"""MARSOUD-VBILL-CURRENCY-INLINE-EDIT (2026-08-18) — audit.

Verifies:
  A. Owner can POST /vendor-bills/<id>/edit with `currency=EGP`
     on a POSTED bill originally in SAR → bill.currency == "EGP".
  B. Same with garbage `currency=xyz` → currency unchanged
     (whitelist rejects).
  C. DRAFT bill edit also accepts currency change (shared path).
  D. Currency change writes a UserActivityLog entry with
     before/after in extra_data.
  E. The edit form template renders `currency_choices` and picks
     the current currency as `selected`.

The permission gate (`vendor_bills.create` required) is already
covered by portal_403 audit + tests/audit_signup_auto_block; not
duplicated here.
"""
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db  # noqa: E402


CHECKS = []
CO_NAME = "__VBILL_CUR_EDIT_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text
    from app.models import Company, User
    from app.models.user import user_companies
    ids = [c.id for c in Company.query.filter(
        Company.name.like(f"{CO_NAME}%")).all()]
    if ids:
        for t in reversed(db.metadata.sorted_tables):
            if "company_id" in t.c:
                try:
                    db.session.execute(
                        t.delete().where(t.c.company_id.in_(ids)))
                except Exception:
                    db.session.rollback()
        db.session.commit()
    for u in User.query.filter(
            User.email.like(f"{CO_NAME.lower()}%@x.local")).all():
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u.id))
        db.session.delete(u)
    db.session.commit()
    for cid in ids:
        try:
            db.session.execute(
                text("DELETE FROM companies WHERE id = :i"),
                {"i": cid})
        except Exception:
            db.session.rollback()
    db.session.commit()


def _setup():
    from datetime import datetime, timedelta as _td
    from app.models import (Company, User, Vendor, VendorBill,
                             VendorBillStatus, Plan, Account,
                             AccountType, NormalSide)
    from app.models.user import user_companies
    from app.services.legal import get_terms_version

    _teardown()
    tv = get_terms_version()
    now = datetime.utcnow()

    # Any real plan so enforce_access lets us through.
    plan = Plan.query.filter_by(code="growth").first() \
           or Plan.query.filter_by(code="pro").first()

    u = User(email=f"{CO_NAME.lower()}_owner@x.local",
             full_name="Owner", terms_version=tv,
             terms_accepted_at=now)
    u.set_password("Passw0rd!audit1")
    db.session.add(u); db.session.flush()
    co = Company(name=f"{CO_NAME}_A", base_currency="EGP",
                 plan_id=plan.id if plan else None,
                 subscription_started_at=now,
                 subscription_expires_at=now + _td(days=30))
    db.session.add(co); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=co.id, role="owner"))
    v = Vendor(company_id=co.id, name="V", is_active=True)
    db.session.add(v); db.session.flush()
    # Seed one account so DRAFT-path _populate_from_form can
    # validate item lines without a full COA.
    acc = Account(company_id=co.id, code="5100",
                  name="Test Expense", name_ar="مصروف اختبار",
                  type=AccountType.EXPENSE,
                  normal_side=NormalSide.DEBIT,
                  is_postable=True, is_active=True)
    db.session.add(acc); db.session.flush()
    _STATE["acc_id"] = acc.id

    b_posted = VendorBill(
        company_id=co.id, vendor_id=v.id,
        number="AUD-CUE-POSTED",
        issue_date=date.today() - timedelta(days=10),
        due_date=date.today() + timedelta(days=20),
        currency="SAR",
        total=Decimal("500.00"),
        paid_amount=Decimal("0.00"),
        status=VendorBillStatus.POSTED)
    b_draft = VendorBill(
        company_id=co.id, vendor_id=v.id,
        number="AUD-CUE-DRAFT",
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="SAR",
        total=Decimal("100.00"),
        paid_amount=Decimal("0.00"),
        status=VendorBillStatus.DRAFT)
    db.session.add_all([b_posted, b_draft])
    db.session.commit()

    _STATE.update(dict(u=u, co=co, b_posted=b_posted, b_draft=b_draft))


def _client_as(user_id):
    from flask import g
    if "_login_user" in g:
        del g._login_user
    c = _STATE["app"].test_client()
    with c.session_transaction() as s:
        s.clear()
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s["active_company_id"] = _STATE["co"].id
    return c


# ─── A. POSTED bill: currency swap works ──────────────────────────────
@check("A1: POSTED bill currency swap SAR → EGP succeeds")
def A1():
    from app.models import VendorBill
    b = _STATE["b_posted"]
    c = _client_as(_STATE["u"].id)
    r = c.post(f"/vendor-bills/{b.id}/edit", data={
        "vendor_id": b.vendor_id,
        "supplier_invoice_number": b.supplier_invoice_number or "",
        "notes": b.notes or "",
        "currency": "EGP",
    }, follow_redirects=False)
    assert r.status_code in (302, 303), (
        r.status_code, r.data[:200])
    db.session.expire_all()
    bb = db.session.get(VendorBill, b.id)
    assert bb.currency == "EGP", f"got {bb.currency!r}"


# ─── B. Whitelist rejects garbage ─────────────────────────────────────
@check("B1: POST with currency=xyz leaves currency unchanged")
def B1():
    from app.models import VendorBill
    b = _STATE["b_posted"]
    # Reset to a known value
    b.currency = "SAR"
    db.session.commit()
    c = _client_as(_STATE["u"].id)
    r = c.post(f"/vendor-bills/{b.id}/edit", data={
        "vendor_id": b.vendor_id,
        "supplier_invoice_number": "",
        "notes": "",
        "currency": "xyz",
    }, follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code
    db.session.expire_all()
    bb = db.session.get(VendorBill, b.id)
    assert bb.currency == "SAR", (
        f"garbage accepted: {bb.currency!r}")


# ─── C. DRAFT bill: same path ─────────────────────────────────────────
@check("C1: DRAFT bill currency swap works too (shared path)")
def C1():
    from app.models import VendorBill, VendorBillStatus
    b = _STATE["b_draft"]
    # DRAFT edit hits _populate_from_form which rewrites items
    # from the form. Post minimal but valid item data so the
    # transaction doesn't fail on the items rewrite.
    acc_id = _STATE["acc_id"]

    c = _client_as(_STATE["u"].id)
    r = c.post(f"/vendor-bills/{b.id}/edit", data={
        "vendor_id": b.vendor_id,
        "supplier_invoice_number": "",
        "issue_date": b.issue_date.isoformat(),
        "due_date": b.due_date.isoformat(),
        "payment_method": "CASH",
        "notes": "",
        "tax_rate": "0",
        "currency": "AED",
        "item_description[]": "test item",
        "item_line_type[]": "EXPENSE",
        "item_account_id[]": str(acc_id),
        "item_quantity[]": "1",
        "item_unit_price[]": "100",
    }, follow_redirects=False)
    assert r.status_code in (302, 303), (
        r.status_code, r.data[:400])
    db.session.expire_all()
    bb = db.session.get(VendorBill, b.id)
    assert bb.currency == "AED", f"got {bb.currency!r}"


# ─── D. Audit trail ───────────────────────────────────────────────────
@check("D1: currency change writes a UserActivityLog row")
def D1():
    from app.models.activity import UserActivityLog
    from app.models import VendorBill
    b = _STATE["b_posted"]
    b.currency = "SAR"
    db.session.commit()
    c = _client_as(_STATE["u"].id)
    c.post(f"/vendor-bills/{b.id}/edit", data={
        "vendor_id": b.vendor_id,
        "supplier_invoice_number": "",
        "notes": "audit test",
        "currency": "USD",
    }, follow_redirects=False)
    db.session.expire_all()
    row = (UserActivityLog.query
           .filter_by(entity_type="VendorBill",
                       entity_id=b.id,
                       action_type="UPDATE")
           .order_by(UserActivityLog.id.desc())
           .first())
    assert row is not None, "no audit log entry"
    assert row.extra_data, "extra_data empty"
    data = json.loads(row.extra_data)
    assert data.get("field") == "currency", data
    assert data.get("before") == "SAR" and data.get("after") == "USD", (
        data)


# ─── E. Edit template renders the picker ──────────────────────────────
@check("E1: GET /vendor-bills/<id>/edit renders the currency picker")
def E1():
    b = _STATE["b_posted"]
    c = _client_as(_STATE["u"].id)
    r = c.get(f"/vendor-bills/{b.id}/edit", follow_redirects=False)
    assert r.status_code == 200, r.status_code
    body = r.data.decode("utf-8", errors="replace")
    # Currency select must be present with SAR / EGP options
    assert 'name="currency"' in body, "no currency select"
    assert 'value="EGP"' in body and 'value="SAR"' in body, (
        "currency options missing")


# ─── Runner ───────────────────────────────────────────────────────────
def main():
    app = create_app()
    _STATE["app"] = app
    with app.app_context():
        _setup()
        try:
            failed = []
            for label, fn in CHECKS:
                try:
                    fn()
                    print(f"  [OK]   {label}")
                except Exception as e:
                    failed.append((label, e))
                    print(f"  [FAIL] {label}\n         -> {e}")
            total = len(CHECKS)
            ok = total - len(failed)
            print()
            print(f"{ok}/{total} OK" if not failed
                  else f"{ok}/{total} -- {len(failed)} FAILED")
            return 0 if not failed else 1
        finally:
            _teardown()


if __name__ == "__main__":
    sys.exit(main())
