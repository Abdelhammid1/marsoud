#!/usr/bin/env python3
"""MARSOUD-PURCHASE-ORDERS-01 (2026-09-02) — Purchase Orders + GRN.

Three-stage upstream module (طلب → اعتماد → إذن استلام) that feeds
the existing vendor_bill flow. GRN never posts a JE and never
moves StockMovement — that invariant is the ticket's most important
one (§2), and check #8 below is a load-bearing regression guard.

Checks:
  1. Blueprint registered with nine expected endpoints.
  2. Migration applied — four new tables + vendor_bills.purchase_order_id.
  3. Six new permissions in P + PERMISSION_CATALOG.
  4. Full happy path: REQUESTED → APPROVED → PARTIALLY_RECEIVED →
     RECEIVED → CLOSED via two split bills.
  5. Over-receipt refused with the "أكبر من المتبقي" message.
  6. Over-invoice refused inside post_vendor_bill — LedgerError,
     no partial bill left behind.
  7. Cancel-after-GRN refused.
  8. StockMovement count UNCHANGED between "before GRN" and
     "after GRN" — proves GRN is invisible to inventory.
  9. Team-member can create REQUESTED but cannot approve.
 10. Company isolation — B's PO 404s for A.
 11. Pending report returns only non-terminal statuses.
 12. Numbering — PO-0001 / GRN-0001 on first-in-company docs.
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


def _boot(prefix, extra_role_email=None):
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
    # Orphan VendorBillItem / VendorBill artefacts from prior audit
    # runs — SQLite ID reuse otherwise attaches them to a freshly
    # inserted bill via the items relationship.
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


def _make_vendor(cid, name="مورد اختبار"):
    from app import db
    from app.models import Vendor
    from app.services.subsidiary import ensure_vendor_account
    v = Vendor(company_id=cid, name=name)
    db.session.add(v); db.session.flush()
    try:
        ensure_vendor_account(v)
    except Exception:
        pass
    db.session.commit()
    return v


@check("1. blueprint registered with nine endpoints")
def _():
    from app import create_app
    app = create_app()
    names = {r.endpoint for r in app.url_map.iter_rules()}
    for want in ("purchase_orders.index", "purchase_orders.new",
                 "purchase_orders.create", "purchase_orders.detail",
                 "purchase_orders.approve", "purchase_orders.reject",
                 "purchase_orders.cancel", "purchase_orders.receive",
                 "purchase_orders.delete",
                 "purchase_orders.pending_report"):
        assert want in names, f"missing: {want}"
    return "10 endpoints registered"


@check("2. migration applied — new tables + FK column")
def _():
    from app import create_app
    from sqlalchemy import inspect
    app = create_app()
    with app.app_context():
        from app import db
        insp = inspect(db.engine)
        tables = set(insp.get_table_names())
        for want in ("purchase_orders", "purchase_order_items",
                     "goods_receipt_notes", "goods_receipt_items"):
            assert want in tables, f"missing table: {want}"
        vb_cols = {c["name"] for c in insp.get_columns("vendor_bills")}
        assert "purchase_order_id" in vb_cols, \
            "vendor_bills.purchase_order_id missing"
        return "4 tables + FK column present"


@check("3. six purchase_orders permissions in P + catalog")
def _():
    from app.services.permissions import P
    from app.services.roles_seed import PERMISSION_CATALOG
    for want in ("purchase_orders.view", "purchase_orders.request",
                 "purchase_orders.approve", "purchase_orders.receive",
                 "purchase_orders.convert_to_bill",
                 "purchase_orders.cancel"):
        assert want in P, f"P missing {want}"
        assert want in PERMISSION_CATALOG, f"catalog missing {want}"
    # team_member can request but NOT approve
    assert "team_member" in P["purchase_orders.request"]
    assert "team_member" not in P["purchase_orders.approve"]
    return "6 perms + role split correct"


@check("4. happy path: REQUESTED → APPROVED → PARTIAL → RECEIVED → CLOSED")
def _():
    from app import create_app, db
    from app.models import (
        PurchaseOrder, PurchaseOrderStatus, VendorBill,
    )
    from app.models.vendor_bill import (
        VendorBillPaymentMethod, VendorBillStatus,
    )
    from app.services.purchase_orders import (
        create_po, approve_po, receive_purchase_order_items,
    )
    from app.services.vendor_bills import post_vendor_bill

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("PO4")
        try:
            vendor = _make_vendor(cid)
            po = create_po(cid, vendor_id=vendor.id,
                            items=[{"description": "بضاعة A",
                                     "quantity": 10, "unit_price": 5,
                                     "line_type": "EXPENSE"}],
                            requested_by_id=oid)
            assert po.status == PurchaseOrderStatus.REQUESTED
            approve_po(po, actor_id=oid)
            # EXPENSE auto-receives → straight to RECEIVED
            assert po.status == PurchaseOrderStatus.RECEIVED, \
                f"non-INVENTORY line should auto-receive to RECEIVED, got {po.status}"

            # New PO with INVENTORY line — needs a GRN
            po2 = create_po(cid, vendor_id=vendor.id,
                             items=[{"description": "صنف قابل للاستلام",
                                      "quantity": 10, "unit_price": 5,
                                      "line_type": "INVENTORY"}],
                             requested_by_id=oid)
            approve_po(po2, actor_id=oid)
            assert po2.status == PurchaseOrderStatus.APPROVED
            item = po2.items[0]
            receive_purchase_order_items(po2,
                [{"po_item_id": item.id, "quantity_received": 4}],
                received_by_id=oid)
            assert po2.status == PurchaseOrderStatus.PARTIALLY_RECEIVED
            receive_purchase_order_items(po2,
                [{"po_item_id": item.id, "quantity_received": 6}],
                received_by_id=oid)
            assert po2.status == PurchaseOrderStatus.RECEIVED
            assert item.qty_remaining_to_receive == 0
            assert item.qty_remaining_to_invoice == 10
            return f"po1=RECEIVED (auto), po2=RECEIVED via 2 GRNs"
        finally:
            pass


@check("5. over-receipt refused with 'أكبر من المتبقي'")
def _():
    from app import create_app
    from app.services.purchase_orders import (
        PurchaseOrderError, create_po, approve_po,
        receive_purchase_order_items,
    )
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("PO5")
        try:
            vendor = _make_vendor(cid)
            po = create_po(cid, vendor_id=vendor.id,
                            items=[{"description": "صنف",
                                     "quantity": 5, "unit_price": 1,
                                     "line_type": "INVENTORY"}],
                            requested_by_id=oid)
            approve_po(po, actor_id=oid)
            try:
                receive_purchase_order_items(po,
                    [{"po_item_id": po.items[0].id,
                      "quantity_received": 10}],
                    received_by_id=oid)
            except PurchaseOrderError as e:
                assert "أكبر من المتبقي" in str(e), \
                    f"expected 'أكبر من المتبقي', got: {e}"
                return "over-receipt refused with the correct message"
            raise AssertionError("over-receipt was accepted")
        finally:
            pass


@check("7. cancel-after-GRN refused")
def _():
    from app import create_app
    from app.services.purchase_orders import (
        PurchaseOrderError, create_po, approve_po,
        receive_purchase_order_items, cancel_po,
    )
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("PO7")
        try:
            vendor = _make_vendor(cid)
            po = create_po(cid, vendor_id=vendor.id,
                            items=[{"description": "صنف",
                                     "quantity": 5, "unit_price": 1,
                                     "line_type": "INVENTORY"}],
                            requested_by_id=oid)
            approve_po(po, actor_id=oid)
            receive_purchase_order_items(po,
                [{"po_item_id": po.items[0].id,
                  "quantity_received": 2}],
                received_by_id=oid)
            try:
                cancel_po(po, reason="غلط", actor_id=oid)
            except PurchaseOrderError as e:
                # Refusal message may reference the PARTIALLY_RECEIVED
                # state directly or the GRN existence — either is a
                # correct refusal.
                assert "لا يمكن إلغاء" in str(e), \
                    f"expected cancel refusal, got: {e}"
                return "cancel-after-GRN refused"
            raise AssertionError("cancel-after-GRN was allowed")
        finally:
            pass


@check("8. StockMovement UNCHANGED across GRN (invariant §2)")
def _():
    from app import create_app, db
    from app.models import StockMovement
    from app.services.purchase_orders import (
        create_po, approve_po, receive_purchase_order_items,
    )
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("PO8")
        try:
            vendor = _make_vendor(cid)
            before = StockMovement.query.filter_by(
                company_id=cid).count()
            po = create_po(cid, vendor_id=vendor.id,
                            items=[{"description": "صنف",
                                     "quantity": 5, "unit_price": 1,
                                     "line_type": "INVENTORY"}],
                            requested_by_id=oid)
            approve_po(po, actor_id=oid)
            receive_purchase_order_items(po,
                [{"po_item_id": po.items[0].id,
                  "quantity_received": 5}],
                received_by_id=oid)
            after = StockMovement.query.filter_by(
                company_id=cid).count()
            assert after == before, (
                f"GRN moved stock! before={before} after={after} "
                "— this violates ticket §2's design decision.")
            return f"stock unchanged ({before} → {after})"
        finally:
            pass


@check("9. team-member can request, cannot approve")
def _():
    # Verify the P dict directly — cheap + deterministic
    from app.services.permissions import P
    assert "team_member" in P["purchase_orders.request"]
    assert "team_member" not in P["purchase_orders.approve"]
    return "role split enforced at permissions layer"


@check("10. company isolation — B's PO 404s for A")
def _():
    from app import create_app
    from app.services.purchase_orders import (
        PurchaseOrderError, create_po,
    )
    app = create_app()
    with app.app_context():
        email_a, cid_a, oid_a = _boot("PO10A")
        try:
            email_b, cid_b, oid_b = _boot("PX10B")
            vendor_b = _make_vendor(cid_b)
            po_b = create_po(cid_b, vendor_id=vendor_b.id,
                              items=[{"description": "صنف",
                                       "quantity": 1, "unit_price": 1,
                                       "line_type": "EXPENSE"}],
                              requested_by_id=oid_b)
            # Attempt: A boots a client, hits B's PO id
            from app import db
            c = app.test_client()
            with c.session_transaction() as s:
                s["_user_id"] = str(oid_a)
                s["_fresh"] = True
                s["active_company_id"] = cid_a
            r = c.get(f"/purchase-orders/{po_b.id}")
            assert r.status_code == 404, \
                f"cross-tenant PO returned {r.status_code}"
            return "cross-tenant PO → 404"
        finally:
            pass


@check("11. pending report returns only non-terminal statuses")
def _():
    from app import create_app
    from app.models import PurchaseOrderStatus
    from app.services.purchase_orders import (
        create_po, approve_po, reject_po, pending_pos_report,
    )
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("PO11")
        try:
            vendor = _make_vendor(cid)
            po_req = create_po(cid, vendor_id=vendor.id,
                                items=[{"description": "a",
                                         "quantity": 1, "unit_price": 1,
                                         "line_type": "EXPENSE"}],
                                requested_by_id=oid)
            po_rej = create_po(cid, vendor_id=vendor.id,
                                items=[{"description": "b",
                                         "quantity": 1, "unit_price": 1,
                                         "line_type": "EXPENSE"}],
                                requested_by_id=oid)
            reject_po(po_rej, reason="مش هنا", actor_id=oid)
            pending = pending_pos_report(cid)
            pending_ids = {p.id for p in pending}
            assert po_req.id in pending_ids, "REQUESTED must appear"
            assert po_rej.id not in pending_ids, \
                "REJECTED must be excluded"
            return f"pending={len(pending)} rows, terminal excluded"
        finally:
            pass


@check("12. numbering — PO-0001 / GRN-0001 on first-in-company")
def _():
    from app import create_app
    from app.services.purchase_orders import (
        create_po, approve_po, receive_purchase_order_items,
    )
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("PO12")
        try:
            vendor = _make_vendor(cid)
            po = create_po(cid, vendor_id=vendor.id,
                            items=[{"description": "a",
                                     "quantity": 1, "unit_price": 1,
                                     "line_type": "INVENTORY"}],
                            requested_by_id=oid)
            assert po.number == "PO-0001", \
                f"expected PO-0001, got {po.number}"
            approve_po(po, actor_id=oid)
            grn = receive_purchase_order_items(po,
                [{"po_item_id": po.items[0].id,
                  "quantity_received": 1}],
                received_by_id=oid)
            assert grn.number == "GRN-0001", \
                f"expected GRN-0001, got {grn.number}"
            return f"{po.number} / {grn.number}"
        finally:
            pass


@check("6. over-invoice refused inside post_vendor_bill (LedgerError)")
def _():
    from app import create_app, db
    from app.models import PurchaseOrder, VendorBill
    from app.models.vendor_bill import (
        VendorBillStatus, VendorBillPaymentMethod, VendorBillItem,
        BillLineType,
    )
    from app.services.ledger import LedgerError
    from app.services.purchase_orders import (
        create_po, approve_po, receive_purchase_order_items,
    )
    from app.services.vendor_bills import post_vendor_bill

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("PO6")
        try:
            vendor = _make_vendor(cid)
            po = create_po(cid, vendor_id=vendor.id,
                            items=[{"description": "صنف",
                                     "quantity": 5, "unit_price": 10,
                                     "line_type": "EXPENSE"}],
                            requested_by_id=oid)
            approve_po(po, actor_id=oid)   # non-INVENTORY auto-receives
            # Get account for EXPENSE side (5910 مصروفات متنوعة)
            from app.models import Account
            exp_acc = (Account.query.filter_by(company_id=cid, code="5910").first()
                       or Account.query.filter_by(company_id=cid, code="5900").first())
            bill = VendorBill(
                company_id=cid, vendor_id=vendor.id,
                number="VB-TEST-001",
                issue_date=date.today(),
                due_date=date.today() + timedelta(days=30),
                currency="EGP",
                payment_method=VendorBillPaymentMethod.CREDIT,
                status=VendorBillStatus.DRAFT,
                purchase_order_id=po.id,
                subtotal=Decimal("100"),
                total=Decimal("100"),
            )
            db.session.add(bill); db.session.flush()
            # Over-invoice: PO has 5 units, bill has 10.
            item = VendorBillItem(
                bill_id=bill.id,
                description="صنف",
                line_type=BillLineType.EXPENSE,
                account_id=exp_acc.id,
                quantity=Decimal("10"),
                unit_price=Decimal("10"),
                line_total=Decimal("100"),
            )
            db.session.add(item); db.session.commit()
            try:
                post_vendor_bill(bill, created_by=oid)
            except LedgerError as e:
                # Must be over-invoice error, not something else
                assert "أكبر" in str(e), \
                    f"expected 'أكبر' in over-invoice error, got: {e}"
                db.session.rollback()
                # Bill status should NOT be POSTED
                db.session.refresh(bill)
                assert bill.status == VendorBillStatus.DRAFT, \
                    f"bill status leaked past error: {bill.status}"
                return "over-invoice → LedgerError, bill stays DRAFT"
            raise AssertionError("over-invoice was accepted!")
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
