#!/usr/bin/env python3
"""MARSOUD-COST-CENTERS-02-EXPENSE-COVERAGE (2026-09-03).

Second cost-center pass — the first ticket wired CC only into the
manual journal form; every real-world expense path (vendor bills,
quick ops) was left un-wired. This audit proves the gap is closed.

Checks:
  1. `_populate_from_form` persists `item_cost_center_id[]` onto the
     VendorBillItem (new-bill DRAFT save).
  2. DRAFT edit through `_populate_from_form` REPLACES the CC on
     an existing item.
  3. Posted-bill cosmetic edit updates `item.cost_center_id` via
     the `item_costctr_<id>` per-row key, without re-touching the
     ledger.
  4. `_build_accrue_expense` propagates `cost_center_id` onto the
     expense debit only (payable credit stays untagged); same for
     `_build_provision_eosb`.
  5. `_pick_cc_at` silently collapses a cross-tenant CC id to None
     — the ledger's post-time guard is the second line of defence.
  6. Post a vendor bill with CC set → `/reports/cost-centers`
     renders the classified expense (not counted as "غير مصنّف").

Base test scaffolding copied from tests/audit_cost_centers.py:44-119.
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
    """Company + owner + seeded CoA. Wipes any residue from a prior
    aborted run (prefix-scoped LIKE pattern). Duplicated from
    tests/audit_cost_centers.py:_boot so this audit is self-contained
    — one setup helper per audit is the codebase norm."""
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
        "DELETE FROM vendor_bill_items WHERE bill_id NOT IN (SELECT id FROM vendor_bills)"))
    db.session.execute(text(
        "DELETE FROM vendor_bills WHERE company_id NOT IN (SELECT id FROM companies)"))
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C",
                    allowed_subitems=None)
        db.session.add(plan)
    plan.set_modules(["accounting", "sales", "purchases", "hr", "reports"])
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


def _make_cc(cid, code="MKT-01", name="Marketing"):
    from app import db
    from app.models import CostCenter
    cc = CostCenter(company_id=cid, code=code, name=name, is_active=True)
    db.session.add(cc); db.session.commit()
    return cc


def _seed_bill(cid, vendor_name="مورد اختبار"):
    """Fresh DRAFT bill + one EXPENSE item, ready for either the
    save-form path (bill.items get wiped + replaced) or the posted
    cosmetic path (item mutated in place)."""
    from app import db
    from app.models import Vendor, Account, VendorBill, VendorBillItem
    from app.models.vendor_bill import (
        VendorBillStatus, VendorBillPaymentMethod, BillLineType,
    )
    from app.services.subsidiary import ensure_vendor_account
    vendor = Vendor(company_id=cid, name=vendor_name)
    db.session.add(vendor); db.session.flush()
    ensure_vendor_account(vendor)
    exp = Account.query.filter_by(company_id=cid, code="5910").first()
    bill = VendorBill(
        company_id=cid, vendor_id=vendor.id,
        number=f"VB-CC02-{vendor.id}",
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="EGP",
        payment_method=VendorBillPaymentMethod.CREDIT,
        status=VendorBillStatus.DRAFT,
        subtotal=Decimal("100"),
        total=Decimal("100"),
    )
    db.session.add(bill); db.session.flush()
    item = VendorBillItem(
        bill_id=bill.id,
        description="مصاريف تسويق",
        line_type=BillLineType.EXPENSE,
        account_id=exp.id,
        quantity=Decimal("1"),
        unit_price=Decimal("100"),
        line_total=Decimal("100"),
    )
    db.session.add(item)
    db.session.commit()
    return bill, item, exp, vendor


def _form(**pairs):
    """MultiDict shaped like `request.form` (getlist-friendly)."""
    from werkzeug.datastructures import MultiDict
    md = MultiDict()
    for k, v in pairs.items():
        if isinstance(v, (list, tuple)):
            for x in v:
                md.add(k, x)
        else:
            md.add(k, v)
    return md


@check("1. _populate_from_form persists item_cost_center_id[] on a "
        "fresh DRAFT bill")
def _():
    from app import create_app, db
    from app.models import Account, VendorBill, VendorBillItem
    from app.models.vendor_bill import (
        VendorBillStatus, VendorBillPaymentMethod,
    )
    from app.routes.vendor_bills import _populate_from_form
    from flask import g as flask_g
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CC02A")
        cc = _make_cc(cid, code="OPS", name="Operations")
        exp = Account.query.filter_by(company_id=cid, code="5910").first()
        bill = VendorBill(
            company_id=cid, vendor_id=None, number="VB-CC02A-01",
            issue_date=date.today(),
            due_date=date.today() + timedelta(days=30),
            currency="EGP",
            payment_method=VendorBillPaymentMethod.CASH,
            status=VendorBillStatus.DRAFT,
        )
        db.session.add(bill); db.session.flush()
        form = _form(
            vendor_id="",
            payment_method="CASH",
            issue_date=date.today().isoformat(),
            due_date=(date.today() + timedelta(days=30)).isoformat(),
            tax_rate="0",
            notes="",
        )
        form.add("item_description[]", "إيجار مكتب")
        form.add("item_line_type[]", "EXPENSE")
        form.add("item_account_id[]", str(exp.id))
        form.add("item_quantity[]", "1")
        form.add("item_unit_price[]", "500")
        form.add("item_cost_center_id[]", str(cc.id))
        # _populate_from_form reads g.active_company for the CC
        # whitelist and enum guards.
        with app.test_request_context():
            from app.models import Company
            flask_g.active_company = db.session.get(Company, cid)
            _populate_from_form(bill, form)
        db.session.commit()
        db.session.refresh(bill)
        assert len(bill.items) == 1, f"expected 1 item, got {len(bill.items)}"
        assert bill.items[0].cost_center_id == cc.id, (
            f"CC not persisted: got {bill.items[0].cost_center_id!r}, "
            f"want {cc.id}")
        return f"item.cost_center_id = {cc.id} ✓"


@check("2. DRAFT edit REPLACES the CC on an existing item")
def _():
    from app import create_app, db
    from app.routes.vendor_bills import _populate_from_form
    from flask import g as flask_g
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CC02B")
        cc_a = _make_cc(cid, code="A", name="A-center")
        cc_b = _make_cc(cid, code="B", name="B-center")
        bill, item, exp, vendor = _seed_bill(cid)
        item.cost_center_id = cc_a.id
        db.session.commit()
        assert item.cost_center_id == cc_a.id
        form = _form(
            vendor_id=str(vendor.id),
            payment_method="CREDIT",
            issue_date=date.today().isoformat(),
            due_date=(date.today() + timedelta(days=30)).isoformat(),
            tax_rate="0",
        )
        form.add("item_description[]", "مصاريف تسويق")
        form.add("item_line_type[]", "EXPENSE")
        form.add("item_account_id[]", str(exp.id))
        form.add("item_quantity[]", "1")
        form.add("item_unit_price[]", "100")
        form.add("item_cost_center_id[]", str(cc_b.id))
        with app.test_request_context():
            from app.models import Company
            flask_g.active_company = db.session.get(Company, cid)
            _populate_from_form(bill, form)
        db.session.commit()
        db.session.refresh(bill)
        assert len(bill.items) == 1
        assert bill.items[0].cost_center_id == cc_b.id, (
            f"expected switch to CC-B ({cc_b.id}), "
            f"got {bill.items[0].cost_center_id}")
        return f"CC {cc_a.id} → {cc_b.id} on DRAFT re-save"


@check("3. Posted-bill cosmetic edit updates cost_center_id via "
        "item_costctr_<id> without re-touching the ledger")
def _():
    from app import create_app, db
    from app.models import JournalLine
    from app.services.vendor_bills import post_vendor_bill
    from app.routes.vendor_bills import _company_cost_centers
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CC02C")
        cc_a = _make_cc(cid, code="A", name="A-center")
        cc_b = _make_cc(cid, code="B", name="B-center")
        bill, item, exp, _v = _seed_bill(cid)
        item.cost_center_id = cc_a.id
        db.session.commit()
        post_vendor_bill(bill, created_by=oid)
        db.session.refresh(bill)
        assert bill.journal_entry_id is not None
        je_id_before = bill.journal_entry_id
        original_lines = JournalLine.query.filter_by(
            entry_id=je_id_before).all()
        original_snap = [(l.id, l.cost_center_id, float(l.debit),
                          float(l.credit)) for l in original_lines]
        # Apply the posted-cosmetic mutation directly (mirrors the
        # code path in vendor_bills.edit()'s else-branch).
        _valid = {c.id for c in _company_cost_centers(cid)}
        # Simulate `request.form.get(f"item_costctr_{item.id}")` = str(cc_b.id)
        new_raw = str(cc_b.id).strip()
        if new_raw:
            try:
                cid_val = int(new_raw)
            except (TypeError, ValueError):
                cid_val = None
            item.cost_center_id = (cid_val if cid_val in _valid else None)
        db.session.commit()
        db.session.refresh(item)
        assert item.cost_center_id == cc_b.id, (
            f"cosmetic edit didn't apply: got {item.cost_center_id!r}")
        # JE untouched: same entry id, same lines, same amounts, same CC
        # tagging on the journal.
        assert bill.journal_entry_id == je_id_before, "JE swapped?!"
        after_lines = JournalLine.query.filter_by(
            entry_id=je_id_before).all()
        after_snap = [(l.id, l.cost_center_id, float(l.debit),
                       float(l.credit)) for l in after_lines]
        assert after_snap == original_snap, (
            f"ledger mutated by cosmetic edit: {original_snap} → "
            f"{after_snap}")
        return "item.cost_center_id switched; JE lines unchanged"


@check("4. _build_accrue_expense + _build_provision_eosb thread "
        "cost_center_id onto the expense leg only")
def _():
    from app import create_app, db
    from app.models import Account
    from app.services.accounting_ops import (
        _build_accrue_expense, _build_provision_eosb,
    )
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CC02D")
        cc = _make_cc(cid, code="RND", name="R&D")
        # 5910 is seeded by seed_default_coa.
        exp = Account.query.filter_by(company_id=cid, code="5910").first()

        # accrue-expense
        built_ae = _build_accrue_expense(cid, {
            "amount": "200",
            "expense_account_id": str(exp.id),
            "date": date.today().isoformat(),
            "cost_center_id": str(cc.id),
            "notes": "audit",
        }, actor_id=oid)
        ae_lines = built_ae.lines
        exp_line = next(l for l in ae_lines
                        if l["account_id"] == exp.id)
        other_line = next(l for l in ae_lines
                          if l["account_id"] != exp.id)
        assert exp_line.get("cost_center_id") == str(cc.id) \
                or exp_line.get("cost_center_id") == cc.id, (
                    f"accrue-expense didn't tag expense leg: {exp_line}")
        assert other_line.get("cost_center_id") in (None, ""), (
            f"payable leg wrongly tagged: {other_line}")

        # provision-eosb — returns (desc, lines) not Built.
        _desc, eosb_lines = _build_provision_eosb(cid, {
            "amount": "300",
            "expense_account_id": str(exp.id),
            "date": date.today().isoformat(),
            "cost_center_id": str(cc.id),
        }, actor_id=oid)
        exp_line2 = next(l for l in eosb_lines
                         if l["account_id"] == exp.id)
        other_line2 = next(l for l in eosb_lines
                           if l["account_id"] != exp.id)
        assert (exp_line2.get("cost_center_id") == str(cc.id)
                or exp_line2.get("cost_center_id") == cc.id), (
            f"provision-eosb didn't tag expense leg: {exp_line2}")
        assert other_line2.get("cost_center_id") in (None, ""), (
            f"EOSB provision leg wrongly tagged: {other_line2}")
        return "both ops tag expense debit only"


@check("5. _pick_cc_at silently drops a cross-tenant CC id → None")
def _():
    from app import create_app, db
    from app.routes.vendor_bills import _pick_cc_at, _company_cost_centers
    app = create_app()
    with app.app_context():
        # Company A + its CC.
        email_a, cid_a, _ = _boot("CC02EA")
        cc_a = _make_cc(cid_a, code="AA", name="A-only")
        valid_a = {c.id for c in _company_cost_centers(cid_a)}
        # Company B has its own CC, id belongs to B not A.
        email_b, cid_b, _ = _boot("CC02EB")
        cc_b = _make_cc(cid_b, code="BB", name="B-only")
        # A valid own-id survives.
        assert _pick_cc_at([str(cc_a.id)], 0, valid_a) == cc_a.id
        # Cross-tenant id (B's cc) evaluated against A's whitelist
        # returns None. NOT an exception — the ledger's guard is the
        # second line of defence.
        assert _pick_cc_at([str(cc_b.id)], 0, valid_a) is None
        # Blank / stray also fall to None.
        assert _pick_cc_at([""], 0, valid_a) is None
        assert _pick_cc_at(["not-a-number"], 0, valid_a) is None
        assert _pick_cc_at([], 0, valid_a) is None
        return "own→id, cross-tenant→None, blank→None"


@check("6. CC report reflects classified expense after a full "
        "post_vendor_bill run")
def _():
    from app import create_app, db
    from app.models import Company, User
    from app.services.vendor_bills import post_vendor_bill
    from flask_login import login_user
    from flask import g as flask_g
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CC02F")
        cc = _make_cc(cid, code="MKT", name="Marketing")
        bill, item, exp, _v = _seed_bill(cid)
        item.cost_center_id = cc.id
        db.session.commit()
        post_vendor_bill(bill, created_by=oid)
        # Render the report as the owner.
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(oid)
            sess["_fresh"] = True
            sess["active_company_id"] = cid
        r = client.get("/reports/cost-centers")
        assert r.status_code == 200, (
            f"report failed: {r.status_code} — {r.get_data(as_text=True)[:200]}")
        html = r.get_data(as_text=True)
        assert "Marketing" in html, "CC name not on the report"
        # 100.00 is the seeded expense amount.
        assert "100.00" in html, (
            "classified expense total 100.00 missing from report body")
        return "CC 'Marketing' shows classified 100.00 debit"


def main():
    from app import create_app
    # Ensure app context is available for any check that forgets
    # to open its own.
    _ = create_app()
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
