#!/usr/bin/env python3
"""MARSOUD-COST-CENTERS-01 (2026-09-02) — cost centers.

New dimension threaded through post_journal, reverse_journal,
post_vendor_bill, and the manual journal form. Every existing caller
of post_journal() keeps working because cost_center_id is optional.

Checks:
  1. Blueprint + report route registered.
  2. Migration applied — cost_centers table + three FK columns.
  3. Two new permissions in P + PERMISSION_CATALOG.
  4. post_journal accepts a mixed set of CC-tagged + un-tagged lines
     — persists cost_center_id correctly, balance math unaffected.
  5. Cross-tenant CC id → LedgerError.
  6. reverse_journal on a CC-tagged JE inherits the CC on every
     reversal line.
  7. post_vendor_bill with item.cost_center_id set → item-derived
     JournalLine carries the CC id; AP + VAT legs stay NULL.
  8. Delete CC with journal references → 400 flash refusal; toggle
     active still flips the flag.
  9. Report GET /reports/cost-centers renders 200 with the seeded
     row + correct "غير مُصنّف" gap.
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


@check("1. blueprint + report route registered")
def _():
    from app import create_app
    app = create_app()
    names = {r.endpoint for r in app.url_map.iter_rules()}
    for want in ("cost_centers.index", "cost_centers.new",
                 "cost_centers.create", "cost_centers.edit",
                 "cost_centers.update", "cost_centers.toggle_active",
                 "cost_centers.delete",
                 "reports.cost_centers_report"):
        assert want in names, f"missing: {want}"
    return "endpoints registered"


@check("2. migration applied — table + 3 FK columns")
def _():
    from app import create_app
    from sqlalchemy import inspect
    app = create_app()
    with app.app_context():
        from app import db
        insp = inspect(db.engine)
        assert "cost_centers" in insp.get_table_names()
        for tbl in ("journal_lines", "vendor_bill_items",
                     "invoice_items"):
            cols = {c["name"] for c in insp.get_columns(tbl)}
            assert "cost_center_id" in cols, \
                f"{tbl} missing cost_center_id"
        return "table + 3 FK columns present"


@check("3. two permissions in P + catalog")
def _():
    from app.services.permissions import P
    from app.services.roles_seed import PERMISSION_CATALOG
    for want in ("cost_centers.manage", "cost_centers.view"):
        assert want in P
        assert want in PERMISSION_CATALOG
    # Manage is owner/admin only — structural
    assert P["cost_centers.manage"] == {"owner", "admin"}
    return "2 perms + structural discipline"


@check("4. post_journal accepts mixed CC/uncc lines, balance math OK")
def _():
    from app import create_app, db
    from app.models import Account, JournalLine
    from app.services.ledger import post_journal

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CC4")
        try:
            cc = _make_cc(cid)
            cash = Account.query.filter_by(company_id=cid, code="1110").first()
            exp = Account.query.filter_by(company_id=cid, code="5910").first()
            entry = post_journal(company_id=cid,
                description="mixed lines",
                lines=[
                    {"account_id": exp.id, "debit": 100, "credit": 0,
                     "cost_center_id": cc.id},
                    {"account_id": cash.id, "debit": 0, "credit": 100},
                ],
                entry_date=date.today())
            lines = JournalLine.query.filter_by(entry_id=entry.id).all()
            cc_ids = {l.account_id: l.cost_center_id for l in lines}
            assert cc_ids[exp.id] == cc.id, "expense line missing CC"
            assert cc_ids[cash.id] is None, "cash line should be untagged"
            return "mixed CC/uncc lines persisted correctly"
        finally:
            pass


@check("5. cross-tenant CC id → LedgerError")
def _():
    from app import create_app
    from app.models import Account
    from app.services.ledger import post_journal, LedgerError

    app = create_app()
    with app.app_context():
        email_a, cid_a, oid_a = _boot("CC5A")
        try:
            email_b, cid_b, oid_b = _boot("CX5B")
            cc_b = _make_cc(cid_b, code="OTHER-01", name="B's CC")
            cash_a = Account.query.filter_by(company_id=cid_a, code="1110").first()
            exp_a = Account.query.filter_by(company_id=cid_a, code="5910").first()
            try:
                post_journal(company_id=cid_a,
                    description="cross-tenant CC attempt",
                    lines=[
                        {"account_id": exp_a.id, "debit": 50, "credit": 0,
                         "cost_center_id": cc_b.id},
                        {"account_id": cash_a.id, "debit": 0, "credit": 50},
                    ],
                    entry_date=date.today())
            except LedgerError as e:
                assert "مركز التكلفة" in str(e), \
                    f"expected مركز التكلفة guard, got: {e}"
                return "cross-tenant CC rejected"
            raise AssertionError("cross-tenant CC accepted")
        finally:
            pass


@check("6. reverse_journal inherits CC on every mirror line")
def _():
    from app import create_app, db
    from app.models import Account, JournalLine
    from app.services.ledger import post_journal, reverse_journal

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CC6")
        try:
            cc = _make_cc(cid)
            cash = Account.query.filter_by(company_id=cid, code="1110").first()
            exp = Account.query.filter_by(company_id=cid, code="5910").first()
            src = post_journal(company_id=cid,
                description="original",
                lines=[
                    {"account_id": exp.id, "debit": 200, "credit": 0,
                     "cost_center_id": cc.id},
                    {"account_id": cash.id, "debit": 0, "credit": 200,
                     "cost_center_id": cc.id},
                ],
                entry_date=date.today())
            rev = reverse_journal(src.id, created_by=oid)
            for l in JournalLine.query.filter_by(entry_id=rev.id).all():
                assert l.cost_center_id == cc.id, \
                    f"reversal line missing CC on account {l.account_id}"
            return "reversal inherits CC on all lines"
        finally:
            pass


@check("7. VendorBill item CC flows to JournalLine (AP+VAT stay NULL)")
def _():
    from app import create_app, db
    from app.models import (
        Account, VendorBill, JournalLine,
    )
    from app.models.vendor_bill import (
        VendorBillStatus, VendorBillPaymentMethod, VendorBillItem,
        BillLineType,
    )
    from app.services.subsidiary import ensure_vendor_account
    from app.services.vendor_bills import post_vendor_bill
    from app.models import Vendor

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CC7")
        try:
            cc = _make_cc(cid)
            vendor = Vendor(company_id=cid, name="مورد")
            db.session.add(vendor); db.session.flush()
            ensure_vendor_account(vendor)
            exp = Account.query.filter_by(company_id=cid, code="5910").first()
            bill = VendorBill(
                company_id=cid, vendor_id=vendor.id,
                number="VB-CC-001",
                issue_date=date.today(),
                due_date=date.today() + timedelta(days=30),
                currency="EGP",
                payment_method=VendorBillPaymentMethod.CREDIT,
                status=VendorBillStatus.DRAFT,
                subtotal=Decimal("100"),
                total=Decimal("100"),
            )
            db.session.add(bill); db.session.flush()
            db.session.add(VendorBillItem(
                bill_id=bill.id,
                description="مصاريف تسويق",
                line_type=BillLineType.EXPENSE,
                account_id=exp.id,
                quantity=Decimal("1"),
                unit_price=Decimal("100"),
                line_total=Decimal("100"),
                cost_center_id=cc.id,
            ))
            db.session.commit()
            post_vendor_bill(bill, created_by=oid)
            db.session.refresh(bill)
            # JE lines: one on expense (should carry CC), one on AP
            # sub-account (should NOT).
            lines = JournalLine.query.filter_by(
                entry_id=bill.journal_entry_id).all()
            found_exp = [l for l in lines if l.account_id == exp.id]
            assert found_exp and found_exp[0].cost_center_id == cc.id, \
                "expense line missing CC"
            other = [l for l in lines if l.account_id != exp.id]
            assert all(l.cost_center_id is None for l in other), \
                "aggregate AP/VAT leg incorrectly tagged"
            return "item CC → JE line; aggregate legs untagged"
        finally:
            pass


@check("8. delete CC with references refused; toggle-active works")
def _():
    from app import create_app, db
    from app.models import Account, CostCenter
    from app.services.ledger import post_journal

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CC8")
        try:
            cc = _make_cc(cid)
            cash = Account.query.filter_by(company_id=cid, code="1110").first()
            exp = Account.query.filter_by(company_id=cid, code="5910").first()
            post_journal(company_id=cid, description="uses CC",
                         lines=[
                             {"account_id": exp.id, "debit": 10, "credit": 0,
                              "cost_center_id": cc.id},
                             {"account_id": cash.id, "debit": 0, "credit": 10},
                         ],
                         entry_date=date.today())
            client = app.test_client()
            with client.session_transaction() as s:
                s["_user_id"] = str(oid)
                s["_fresh"] = True
                s["active_company_id"] = cid
            r = client.post(f"/cost-centers/{cc.id}/delete",
                             follow_redirects=False)
            assert r.status_code in (302, 303)
            db.session.refresh(cc)
            assert cc.deleted_at is None, \
                "delete succeeded despite ledger references"
            # Toggle active — should work fine
            r = client.post(f"/cost-centers/{cc.id}/toggle-active",
                             follow_redirects=False)
            assert r.status_code in (302, 303)
            db.session.refresh(cc)
            assert cc.is_active is False
            return "delete refused, toggle active flips"
        finally:
            pass


@check("9. /reports/cost-centers renders + gap arithmetic correct")
def _():
    from app import create_app, db
    from app.models import Account
    from app.services.ledger import post_journal

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("CC9")
        try:
            cc = _make_cc(cid)
            cash = Account.query.filter_by(company_id=cid, code="1110").first()
            exp = Account.query.filter_by(company_id=cid, code="5910").first()
            # Tagged 300, untagged 200 — expect gap=200
            post_journal(company_id=cid, description="tagged expense",
                         lines=[
                             {"account_id": exp.id, "debit": 300, "credit": 0,
                              "cost_center_id": cc.id},
                             {"account_id": cash.id, "debit": 0, "credit": 300},
                         ],
                         entry_date=date.today())
            post_journal(company_id=cid, description="untagged expense",
                         lines=[
                             {"account_id": exp.id, "debit": 200, "credit": 0},
                             {"account_id": cash.id, "debit": 0, "credit": 200},
                         ],
                         entry_date=date.today())
            client = app.test_client()
            with client.session_transaction() as s:
                s["_user_id"] = str(oid)
                s["_fresh"] = True
                s["active_company_id"] = cid
            r = client.get("/reports/cost-centers")
            assert r.status_code == 200, (
                f"got {r.status_code} → {r.headers.get('Location')}")
            html = r.data.decode("utf-8")
            assert "MKT-01" in html
            assert "300.00" in html   # classified total
            assert "200.00" in html   # unclassified gap
            return "report renders + gap arithmetic 300/200 present"
        finally:
            pass


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
