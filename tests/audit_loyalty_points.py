#!/usr/bin/env python3
"""MARSOUD-LOYALTY-POINTS-01 (2026-09-02) — retail loyalty program.

Zero-touch to `Invoice.recalc`, `post_invoice_to_ledger`,
`inventory.py`, `ledger.py`. Points earned when an invoice becomes
fully PAID; redemption uses the existing FIXED-discount lane; void
on POS reverses both.

Checks:
  1. Migration applied — table + 3 co columns + 1 cust column + 3
     invoice columns.
  2. award_points_for_invoice earns int(taxable_base // earn_rate).
  3. award idempotent — second call is a no-op.
  4. redeem_points sets FIXED discount + recalcs total.
  5. redeem refuses: (a) insufficient balance, (b) manual discount,
     (c) walk-in.
  6. reverse_points_for_invoice returns redeemed + claws back earned.
  7. Walk-in invoice (customer_id=None) → NO transactions.
  8. adjust_points_manually refuses blank note.
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


def _boot(prefix, *, loyalty=True):
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
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C")
        db.session.add(plan)
    plan.set_modules(["accounting", "sales", "pos", "reports"])
    db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency="EGP",
                subdomain=prefix.lower(), plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1),
                loyalty_enabled=loyalty,
                loyalty_earn_rate=Decimal("10"),
                loyalty_redemption_value=Decimal("0.10"))
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


def _make_customer(cid, name="عميل ولاء"):
    from app import db
    from app.models import Customer
    from app.services.subsidiary import ensure_customer_account
    cust = Customer(company_id=cid, name=name)
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    db.session.commit()
    return cust


_INV_COUNTER = [0]


def _make_invoice(cid, cust, *, subtotal=250, tax_rate=0):
    """Build a minimal Invoice with items_total=subtotal so
    taxable_base==subtotal after recalc."""
    from app import db
    from app.models import Invoice, InvoiceItem
    from app.models.invoice import (
        InvoiceStatus, DiscountType,
    )
    _INV_COUNTER[0] += 1
    inv = Invoice(
        company_id=cid,
        customer_id=cust.id if cust else None,
        number=f"AUD-{cid}-{_INV_COUNTER[0]}",
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="EGP",
        tax_rate=Decimal(str(tax_rate)),
        invoice_discount_type=DiscountType.NONE,
        invoice_discount_value=Decimal("0"),
        status=InvoiceStatus.DRAFT,
        source="MANUAL",
    )
    db.session.add(inv); db.session.flush()
    it = InvoiceItem(
        invoice_id=inv.id, company_id=cid,
        description="بند اختبار",
        quantity=Decimal("1"),
        unit_price=Decimal(str(subtotal)),
        line_total=Decimal(str(subtotal)),
    )
    db.session.add(it); db.session.flush()
    inv.recalc()
    db.session.commit()
    return inv


@check("1. migration applied — table + columns present")
def _():
    from app import create_app
    from sqlalchemy import inspect
    app = create_app()
    with app.app_context():
        from app import db
        insp = inspect(db.engine)
        assert "loyalty_point_transactions" in insp.get_table_names()
        cco = {c["name"] for c in insp.get_columns("companies")}
        for w in ("loyalty_enabled", "loyalty_earn_rate",
                   "loyalty_redemption_value"):
            assert w in cco, f"companies missing {w}"
        assert "loyalty_points_balance" in {
            c["name"] for c in insp.get_columns("customers")}
        cin = {c["name"] for c in insp.get_columns("invoices")}
        for w in ("loyalty_points_earned", "loyalty_points_redeemed",
                   "loyalty_points_awarded_at"):
            assert w in cin, f"invoices missing {w}"
        return "schema present"


@check("2. award_points earns int(taxable_base // earn_rate)")
def _():
    from app import create_app, db
    from app.services.loyalty import award_points_for_invoice
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("LP2")
        try:
            cust = _make_customer(cid)
            inv = _make_invoice(cid, cust, subtotal=250)
            # earn_rate=10 → 25 points
            award_points_for_invoice(inv)
            db.session.refresh(cust); db.session.refresh(inv)
            assert cust.loyalty_points_balance == 25, cust.loyalty_points_balance
            assert inv.loyalty_points_earned == 25
            assert inv.loyalty_points_awarded_at is not None
            return "250 // 10 = 25 pts earned + guard stamped"
        finally:
            pass


@check("3. award idempotent — second call is a no-op")
def _():
    from app import create_app, db
    from app.services.loyalty import award_points_for_invoice
    from app.models import LoyaltyPointTransaction
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("LP3")
        try:
            cust = _make_customer(cid)
            inv = _make_invoice(cid, cust, subtotal=100)
            award_points_for_invoice(inv)
            first = int(cust.loyalty_points_balance)
            n1 = LoyaltyPointTransaction.query.filter_by(
                customer_id=cust.id).count()
            award_points_for_invoice(inv)
            db.session.refresh(cust)
            n2 = LoyaltyPointTransaction.query.filter_by(
                customer_id=cust.id).count()
            assert cust.loyalty_points_balance == first
            assert n1 == n2 == 1
            return "1 transaction, no double-award"
        finally:
            pass


@check("4. redeem_points sets FIXED discount + recalcs total")
def _():
    from app import create_app, db
    from app.services.loyalty import (
        award_points_for_invoice, redeem_points,
    )
    from app.models.invoice import DiscountType
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("LP4")
        try:
            cust = _make_customer(cid)
            # Earn 100 pts on a 1000 invoice first
            inv1 = _make_invoice(cid, cust, subtotal=1000)
            award_points_for_invoice(inv1)
            assert cust.loyalty_points_balance == 100
            # Now redeem 50 pts on a new 500 invoice (value=5)
            inv2 = _make_invoice(cid, cust, subtotal=500)
            redeem_points(inv2, 50)
            db.session.refresh(cust); db.session.refresh(inv2)
            assert inv2.invoice_discount_type == DiscountType.FIXED
            assert abs(float(inv2.invoice_discount_value) - 5.0) < 0.01
            assert abs(float(inv2.total) - 495.0) < 0.01
            assert cust.loyalty_points_balance == 50
            assert inv2.loyalty_points_redeemed == 50
            return "50 pts → 5 EGP off, balance 100→50"
        finally:
            pass


@check("5. redeem refuses insufficient / manual-discount / walk-in")
def _():
    from app import create_app, db
    from app.services.loyalty import (
        award_points_for_invoice, redeem_points, LoyaltyError,
    )
    from app.models.invoice import DiscountType
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("LP5")
        try:
            cust = _make_customer(cid)
            # (a) Insufficient — customer has 0
            inv = _make_invoice(cid, cust, subtotal=100)
            try:
                redeem_points(inv, 10)
            except LoyaltyError as e:
                assert "رصيد" in str(e), f"expected balance msg, got {e}"
            else:
                raise AssertionError("insufficient balance accepted")
            # (b) Manual discount already
            award_points_for_invoice(
                _make_invoice(cid, cust, subtotal=200))   # +20 pts
            inv3 = _make_invoice(cid, cust, subtotal=200)
            inv3.invoice_discount_type = DiscountType.FIXED
            inv3.invoice_discount_value = Decimal("10")
            inv3.recalc()
            db.session.commit()
            try:
                redeem_points(inv3, 10)
            except LoyaltyError as e:
                assert "خصم يدوي" in str(e)
            else:
                raise AssertionError("manual + loyalty combined accepted")
            # (c) Walk-in
            inv4 = _make_invoice(cid, None, subtotal=200)
            try:
                redeem_points(inv4, 10)
            except LoyaltyError as e:
                assert "بدون عميل" in str(e)
            else:
                raise AssertionError("walk-in redeem accepted")
            return "3 refusals correct"
        finally:
            pass


@check("6. reverse returns redeemed + claws back earned")
def _():
    from app import create_app, db
    from app.services.loyalty import (
        award_points_for_invoice, redeem_points,
        reverse_points_for_invoice,
    )
    from app.models import LoyaltyPointTransaction
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("LP6")
        try:
            cust = _make_customer(cid)
            # Earn 30 pts on 300 first
            award_points_for_invoice(
                _make_invoice(cid, cust, subtotal=300))
            assert cust.loyalty_points_balance == 30
            # Second invoice: redeem 10 pts + earn on remainder
            inv2 = _make_invoice(cid, cust, subtotal=500)
            redeem_points(inv2, 10)   # 10 pts spent → balance 20
            award_points_for_invoice(inv2)
            db.session.refresh(cust)
            # taxable_base of inv2 = 500-1 = 499 → 49 pts earned
            assert cust.loyalty_points_balance == 20 + 49
            # Now void inv2 → +10 (refund) then −49 (claw)
            reverse_points_for_invoice(inv2)
            db.session.refresh(cust)
            assert cust.loyalty_points_balance == 30, \
                f"expected back to 30, got {cust.loyalty_points_balance}"
            rows = LoyaltyPointTransaction.query.filter_by(
                customer_id=cust.id).all()
            reasons = [r.reason.value for r in rows]
            assert "REDEEMED_REFUNDED" in reasons
            assert "EARNED_REVERSED" in reasons
            return "balance restored to 30; both reversal rows land"
        finally:
            pass


@check("7. walk-in — NO transactions, no guard stamp")
def _():
    from app import create_app, db
    from app.services.loyalty import award_points_for_invoice
    from app.models import LoyaltyPointTransaction
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("LP7")
        try:
            inv = _make_invoice(cid, None, subtotal=1000)
            award_points_for_invoice(inv)
            n = LoyaltyPointTransaction.query.filter_by(
                company_id=cid).count()
            assert n == 0, f"walk-in leaked {n} transactions"
            return "0 transactions for walk-in"
        finally:
            pass


@check("8. adjust_points_manually refuses blank note")
def _():
    from app import create_app, db
    from app.services.loyalty import (
        adjust_points_manually, LoyaltyError,
    )
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("LP8")
        try:
            cust = _make_customer(cid)
            for bad in ("", "   ", None):
                try:
                    adjust_points_manually(cust, 50, bad, actor_id=oid)
                except LoyaltyError:
                    continue
                raise AssertionError(f"blank note {bad!r} accepted")
            # Valid adjust
            adjust_points_manually(cust, 50, "تعويض شكوى", actor_id=oid)
            db.session.refresh(cust)
            assert cust.loyalty_points_balance == 50
            return "blank refused, valid landed"
        finally:
            pass


# ─── MARSOUD-LOYALTY-POINTS-02-DISPLAY additions ──────────────────────
# Three HTTP-level + service checks proving the display + redeem
# gap is closed: create_pos_order actually consumes points, the API
# endpoint feeding the widget returns the right shape, and a role
# without loyalty.redeem is blocked at the POS route.

def _seed_pos_kit(cid, oid):
    """Warehouse + tracked variant with opening stock + a POS-usable
    PaymentMethod. Mirrors tests/audit_dual_uom_weight.py:217-275
    minus piece tracking. Returns (variant, payment_method, warehouse)."""
    from app import db
    from app.models import (
        Warehouse, Product, ProductVariant, ProductGroup,
        ProductCategory, PaymentMethod, Account,
    )
    from app.services.inventory import receive_stock
    wh = Warehouse.query.filter_by(
        company_id=cid, is_default=True).first()
    if not wh:
        wh = Warehouse(company_id=cid, code="MAIN", name="MAIN",
                        is_default=True)
        db.session.add(wh); db.session.flush()
    grp = ProductGroup.query.filter_by(company_id=cid).first()
    if not grp:
        grp = ProductGroup(company_id=cid, name="عام", is_active=True)
        db.session.add(grp); db.session.flush()
    cat = ProductCategory.query.filter_by(
        company_id=cid, group_id=grp.id).first()
    if not cat:
        cat = ProductCategory(company_id=cid, group_id=grp.id,
                               name="عام", is_active=True)
        db.session.add(cat); db.session.flush()
    p = Product(company_id=cid, name="منتج ولاء", is_tracked=True,
                 category_id=cat.id, default_price=Decimal("200"))
    db.session.add(p); db.session.flush()
    v = ProductVariant(company_id=cid, product_id=p.id,
                        sku=f"LOY-{p.id}", name="",
                        unit_cost=Decimal("50"), is_active=True)
    db.session.add(v); db.session.commit()
    receive_stock(variant=v, warehouse=wh, qty=100,
                   unit_cost=50, actor_id=oid)
    any_asset = Account.query.filter_by(
        company_id=cid, is_postable=True).first()
    pm = PaymentMethod.query.filter_by(
        company_id=cid, is_active=True).first()
    if not pm:
        pm = PaymentMethod(company_id=cid, name="POS-CASH",
                            name_ar="نقدي POS",
                            account_id=any_asset.id, is_active=True,
                            is_default=True)
        db.session.add(pm); db.session.commit()
    return v, pm, wh


@check("9. create_pos_order(points_used=50) redeems + drops balance + "
        "leaves REDEEMED txn row")
def _():
    from app import create_app, db
    from app.models import LoyaltyPointTransaction, LoyaltyReason
    from app.services.loyalty import adjust_points_manually
    from app.services.pos import create_pos_order
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("LP9")
        try:
            cust = _make_customer(cid)
            # Grant a starting balance of 100 pts (redemption_value
            # 0.10 → 10 EGP max). Bypasses the earn path entirely so
            # the check tests only the redeem side.
            adjust_points_manually(cust, 100, "seed for redeem test",
                                    actor_id=oid)
            assert cust.loyalty_points_balance == 100
            variant, pm, _wh = _seed_pos_kit(cid, oid)
            # Cart: 1 unit at 200 EGP. Redeem 50 pts = 5 EGP off →
            # invoice.total = 195.
            invoice = create_pos_order(
                company_id=cid,
                items=[{"variant_id": variant.id, "qty": 1,
                         "unit_price": 200}],
                payment_method_id=pm.id, cashier_id=oid,
                customer_id=cust.id,
                cash_received=1000, tax_rate=0,
                points_used=50,
            )
            db.session.refresh(cust); db.session.refresh(invoice)
            assert invoice.loyalty_points_redeemed == 50, \
                f"got {invoice.loyalty_points_redeemed}"
            # 200 - 5 = 195.
            assert abs(float(invoice.total) - 195.0) < 0.01, \
                f"total={invoice.total} — expected 195.00"
            # Balance ledger: 100 seed - 50 redeem + earn on the paid
            # 195 EGP invoice (rate=10 → int(195/10)=19 earned). Both
            # flows fire in one POS call because record_payment is
            # invoked at the tail end of create_pos_order.
            expected = 100 - 50 + int(195 // 10)
            assert cust.loyalty_points_balance == expected, (
                f"balance={cust.loyalty_points_balance}, "
                f"expected {expected}")
            # One REDEEMED txn linked to this invoice.
            redeem_txn = LoyaltyPointTransaction.query.filter_by(
                customer_id=cust.id,
                reason=LoyaltyReason.REDEEMED,
                source_type="invoice", source_id=invoice.id,
            ).first()
            assert redeem_txn is not None
            assert redeem_txn.points_delta == -50
            # Companion EARNED row for the paid invoice.
            earn_txn = LoyaltyPointTransaction.query.filter_by(
                customer_id=cust.id,
                reason=LoyaltyReason.EARNED,
                source_type="invoice", source_id=invoice.id,
            ).first()
            assert earn_txn is not None, "post-payment earn missing"
            return ("50 pts consumed via POS → 5 EGP off, "
                    f"balance 100→{expected} (both redeem + earn)")
        finally:
            pass


@check("10. GET /pos/api/customer/<id> returns balance + can_redeem "
        "flag as the owner (loyalty.redeem ✓)")
def _():
    from app import create_app, db
    from app.services.loyalty import adjust_points_manually
    from flask_login import login_user
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("LP10")
        try:
            cust = _make_customer(cid)
            adjust_points_manually(cust, 240, "seed", actor_id=oid)
            client = app.test_client()
            with client.session_transaction() as sess:
                sess["_user_id"] = str(oid)
                sess["_fresh"] = True
                sess["active_company_id"] = cid
            r = client.get(f"/pos/api/customer/{cust.id}")
            assert r.status_code == 200, (
                f"got {r.status_code}: {r.get_data(as_text=True)[:200]}")
            body = r.get_json()
            assert body["balance"] == 240, body
            assert body["loyalty_enabled"] is True, body
            assert body["can_redeem"] is True, body   # owner has all perms
            assert abs(body["max_redeem_egp"] - 24.0) < 0.01, body
            # 404 for cross-tenant / unknown id.
            r404 = client.get("/pos/api/customer/999999")
            assert r404.status_code == 404, r404.status_code
            return "owner sees balance 240 + can_redeem=true; 404 on unknown"
        finally:
            pass


@check("11. create_pos_order silently ignores points_used on a "
        "walk-in (customer_id=None)")
def _():
    """The POS widget only shows the redeem input when a customer is
    picked, so a walk-in POST carrying stray `points_used` should be
    a no-op — not an error and not a spurious LoyaltyPointTransaction
    against a non-existent customer. create_pos_order's guard
    (`customer_id` clause) is what stops it; this check pins that
    behaviour so a well-meaning refactor never turns a stray form
    field into a hard failure."""
    from app import create_app, db
    from app.models import (
        LoyaltyPointTransaction, LoyaltyReason, Invoice,
    )
    from app.services.pos import create_pos_order
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("LP11")
        try:
            variant, pm, _wh = _seed_pos_kit(cid, oid)
            invoice = create_pos_order(
                company_id=cid,
                items=[{"variant_id": variant.id, "qty": 1,
                         "unit_price": 200}],
                payment_method_id=pm.id, cashier_id=oid,
                customer_id=None,           # walk-in
                cash_received=1000, tax_rate=0,
                points_used=99,             # ignored
            )
            db.session.refresh(invoice)
            # No discount applied — the whole point of the guard.
            assert abs(float(invoice.total) - 200.0) < 0.01, \
                f"walk-in charged reduced total: {invoice.total}"
            assert invoice.loyalty_points_redeemed == 0
            # No REDEEMED ledger row (an EARN row for the singleton
            # walk-in customer is pre-existing behaviour — see
            # app/services/subsidiary.py:party_ar_account — that
            # attaches the invoice to the "زبون نقدي" record so the
            # ledger has a real party. Not this ticket's business.)
            n_red = LoyaltyPointTransaction.query.filter_by(
                company_id=cid,
                reason=LoyaltyReason.REDEEMED).count()
            assert n_red == 0, (
                f"walk-in produced {n_red} REDEEMED txns despite "
                "the customer_id guard in create_pos_order")
            return ("walk-in + points_used=99 → no discount, "
                    "no REDEEMED txn")
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
