#!/usr/bin/env python3
"""MARSOUD-BILL-SPLIT — audit for the three-way vendor-bill entry.

/vendor-bills/new used to be one form with a per-row "النوع" dropdown.
It is now a chooser plus three single-type forms, and the inventory
form learned the pack fields /products/new has always had.

Coverage:
  1. /vendor-bills/new renders the chooser with all three cards and no
     line-type dropdown.
  2. An unknown kind slug bounces back to the chooser instead of 404.
  3. Each typed form renders its own fields: expense/asset carry an
     account picker, inventory does not (the account is server-side).
  4. INVENTORY line posted with a blank account resolves to 1300.
  5. A blank account on a non-inventory line raises LedgerError instead
     of the bare ValueError int("") used to throw.
  6. A product created inline from a bill lands in a category — it used
     to be left NULL, outside the hierarchy /products/new enforces.
  7. The pack fields create base + pack units and stamp unit_id on the
     line, so "10 كراتين × 30" posts 300 pieces and not 10 — the bug
     this ticket exists for.
  8. A pack name equal to the base unit name is refused.
  9. No pack fields → base unit only, quantity stays in pieces.
 10. Single-type recurring prefill skips the chooser; a mixed template
     stops on it with the warning.
"""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__BILL_SPLIT_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import (
        Company, Plan, User, UserStatus, Warehouse, Vendor, user_companies,
    )
    from app.services.seed_coa import seed_default_coa
    from app.services.subsidiary import ensure_vendor_account
    from werkzeug.security import generate_password_hash

    existing = Company.query.filter_by(name=COMPANY_NAME).first()
    if existing:
        _teardown_company(existing.id)

    # MARSOUD-CHOOSE-PLAN — an owner whose company has neither plan_id
    # nor intended_plan_id is bounced to /choose-plan before the route
    # runs. intended_plan_id alone clears that gate; plan_id stays NULL
    # so MARSOUD-58 sub-item gating takes its no-plan back-compat path
    # rather than filtering on whatever the cheapest plan happens to
    # list.
    plan = Plan.query.filter_by(is_active=True).first()
    c = Company(name=COMPANY_NAME, base_currency="SAR",
                intended_plan_id=plan.id if plan else None)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)

    wh = Warehouse(company_id=c.id, name="المخزن الرئيسي", code="MAIN")
    db.session.add(wh); db.session.flush()
    vendor = Vendor(company_id=c.id, name="مورد الاختبار")
    db.session.add(vendor); db.session.flush()
    ensure_vendor_account(vendor)

    # MARSOUD-TERMS-CONSENT — stamp the published terms version or the
    # reaccept middleware swallows every request with a redirect.
    try:
        from app.services.legal import get_terms_version
        terms_version = get_terms_version()
    except Exception:  # noqa: BLE001
        terms_version = None
    u = User(email="bill-split-audit@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="BillSplit Owner", is_active=True,
             status=UserStatus.ACTIVE.value,
             terms_version=terms_version)
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner",
    ))
    db.session.commit()
    _STATE.update(company_id=c.id, warehouse_id=wh.id,
                  vendor_id=vendor.id, user_id=u.id)


def _teardown_company(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        bill_ids = [r[0] for r in conn.execute(text(
            "SELECT id FROM vendor_bills WHERE company_id = :c"),
            {"c": company_id}).fetchall()]
        je_ids = [r[0] for r in conn.execute(text(
            "SELECT id FROM journal_entries WHERE company_id = :c"),
            {"c": company_id}).fetchall()]
        if bill_ids:
            _in = ",".join(str(i) for i in bill_ids)
            conn.execute(text(
                f"DELETE FROM vendor_bill_items WHERE bill_id IN ({_in})"))
        if je_ids:
            _in = ",".join(str(i) for i in je_ids)
            conn.execute(text(
                f"DELETE FROM journal_lines WHERE entry_id IN ({_in})"))
        conn.execute(text(
            "DELETE FROM recurring_bill_overrides WHERE company_id = :c"
        ), {"c": company_id})
        conn.execute(text(
            "DELETE FROM recurring_bills WHERE company_id = :c"
        ), {"c": company_id})
        # product_units has no company_id — drop it via its products.
        prod_ids = [r[0] for r in conn.execute(text(
            "SELECT id FROM products WHERE company_id = :c"),
            {"c": company_id}).fetchall()]
        if prod_ids:
            _in = ",".join(str(i) for i in prod_ids)
            conn.execute(text(
                f"DELETE FROM product_units WHERE product_id IN ({_in})"))
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email = 'bill-split-audit@x.test'"))
        # SQLite reuses ids — sweep stock rows whose variant is gone so
        # a later run's fresh variant doesn't inherit their balance.
        for t in ("stock_balances", "stock_movements", "stock_lots"):
            conn.execute(text(
                f"DELETE FROM {t} WHERE variant_id NOT IN "
                "(SELECT id FROM product_variants)"))


def _reset_g():
    from flask import g
    for k in ("active_company", "_active_company", "active_company_id"):
        if hasattr(g, k):
            try:
                delattr(g, k)
            except Exception:  # noqa: BLE001
                pass


def _client():
    from flask import current_app
    _reset_g()
    c = current_app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["user_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["company_id"]
    return c


def _new_bill(number):
    """A DRAFT shell for _populate_from_form to fill."""
    from app.models import VendorBill, VendorBillStatus
    b = VendorBill(
        company_id=_STATE["company_id"], vendor_id=_STATE["vendor_id"],
        number=number, issue_date=date.today(), due_date=date.today(),
        status=VendorBillStatus.DRAFT,
    )
    db.session.add(b); db.session.flush()
    return b


def _inventory_form(**over):
    """A one-row INVENTORY post with a brand-new product."""
    from werkzeug.datastructures import MultiDict
    base = {
        "vendor_id": str(_STATE["vendor_id"]),
        "issue_date": date.today().isoformat(),
        "due_date": date.today().isoformat(),
        "payment_method": "CASH",
        "tax_rate": "0",
        "item_description[]": "بضاعة",
        "item_line_type[]": "INVENTORY",
        "item_account_id[]": "",          # inventory form posts none
        "item_quantity[]": "10",
        "item_unit_price[]": "90",
        "item_warehouse_id[]": str(_STATE["warehouse_id"]),
        "item_variant_id[]": "",
        "item_new_product_name[]": "منتج جديد",
        "item_new_product_sku[]": "NEW-SKU-1",
        "item_new_pack_name[]": "",
        "item_new_pack_pieces[]": "",
        "item_new_pack_sale_price[]": "",
    }
    base.update(over)
    return MultiDict(base)


# ─── Route-shape checks ───────────────────────────────────────────────
@check("1. /vendor-bills/new is the three-way chooser")
def _():
    r = _client().get("/vendor-bills/new")
    assert r.status_code == 200, f"status={r.status_code}"
    body = r.get_data(as_text=True)
    for slug in ("expense", "asset", "inventory"):
        assert f"/vendor-bills/new/{slug}" in body, f"missing {slug} card"
    # The old per-row type dropdown must be gone from the entry point.
    assert 'name="item_line_type[]"' not in body, \
        "chooser still ships the old line-type dropdown"
    return "all three cards, no line-type dropdown"


@check("2. unknown kind slug redirects back to the chooser")
def _():
    r = _client().get("/vendor-bills/new/banana")
    assert r.status_code == 302, f"status={r.status_code}"
    loc = r.headers.get("Location", "")
    assert loc.endswith("/vendor-bills/new"), f"went to {loc}"
    return "unknown slug → chooser (no 404)"


@check("3. each typed form renders only its own fields")
def _():
    client = _client()
    seen = {}
    for slug in ("expense", "asset", "inventory"):
        r = client.get(f"/vendor-bills/new/{slug}")
        assert r.status_code == 200, f"{slug}: status={r.status_code}"
        body = r.get_data(as_text=True)
        # Every row still ships a hidden line_type so the parser is
        # unchanged, but it is fixed, not a <select>.
        assert 'name="item_line_type[]"' in body, f"{slug}: no line_type"
        assert '<select name="item_line_type[]"' not in body, \
            f"{slug}: line-type dropdown came back"
        seen[slug] = body
    assert 'name="item_account_id[]"' in seen["expense"], \
        "expense form lost its account picker"
    assert 'name="item_useful_life_years[]"' in seen["asset"], \
        "asset form lost the useful-life field"
    assert 'name="item_account_id[]"' not in seen["inventory"], \
        "inventory form still asks for an account"
    assert 'name="item_new_pack_pieces[]"' in seen["inventory"], \
        "inventory form is missing the pack fields"
    assert 'name="item_new_pack_pieces[]"' not in seen["expense"], \
        "pack fields leaked into the expense form"
    return "expense/asset keep accounts; inventory hides it, adds packs"


# ─── Account resolution ───────────────────────────────────────────────
@check("4. INVENTORY line with a blank account resolves to 1300")
def _():
    from app.models import Account
    from app.routes.vendor_bills import _populate_from_form
    bill = _new_bill("BS-ACC-1")
    _populate_from_form(bill, _inventory_form())
    db.session.commit()
    inv_acc = Account.query.filter_by(
        company_id=_STATE["company_id"], code="1300").first()
    assert len(bill.items) == 1, f"got {len(bill.items)} lines"
    assert bill.items[0].account_id == inv_acc.id, \
        f"account {bill.items[0].account_id} != 1300 ({inv_acc.id})"
    return "blank account → 1300 المخزون"


@check("5. blank account on a non-inventory line raises LedgerError")
def _():
    from werkzeug.datastructures import MultiDict
    from app.services.ledger import LedgerError
    from app.routes.vendor_bills import _populate_from_form
    bill = _new_bill("BS-ACC-2")
    form = MultiDict({
        "vendor_id": str(_STATE["vendor_id"]),
        "issue_date": date.today().isoformat(),
        "due_date": date.today().isoformat(),
        "payment_method": "CASH", "tax_rate": "0",
        "item_description[]": "إيجار",
        "item_line_type[]": "EXPENSE",
        "item_account_id[]": "",
        "item_quantity[]": "1",
        "item_unit_price[]": "100",
    })
    try:
        _populate_from_form(bill, form)
    except LedgerError as e:
        db.session.rollback()
        assert "إيجار" in str(e), f"error doesn't name the line: {e}"
        return f"LedgerError, not ValueError: {e}"
    except ValueError as e:
        db.session.rollback()
        raise AssertionError(f"still the raw int('') crash: {e}")
    db.session.rollback()
    raise AssertionError("blank expense account was accepted silently")


# ─── Inline product creation ──────────────────────────────────────────
@check("6. a bill-born product lands in a category (not NULL)")
def _():
    from app.models import Product, ProductVariant
    from app.routes.vendor_bills import _populate_from_form
    bill = _new_bill("BS-CAT-1")
    _populate_from_form(bill, _inventory_form(**{
        "item_new_product_name[]": "منتج بتصنيف",
        "item_new_product_sku[]": "CAT-SKU-1",
    }))
    db.session.commit()
    v = db.session.get(ProductVariant, bill.items[0].variant_id)
    p = db.session.get(Product, v.product_id)
    assert p.category_id is not None, \
        "bill-born product still has category_id NULL"
    return f"category_id={p.category_id}"


@check("7. pack fields → 10 كراتين × 30 posts 300 قطعة, not 10")
def _():
    from app.models import (
        Product, ProductVariant, ProductUnit, StockBalance,
        VendorBillPaymentMethod,
    )
    from app.routes.vendor_bills import _populate_from_form
    from app.services.vendor_bills import post_vendor_bill
    bill = _new_bill("BS-PACK-1")
    bill.payment_method = VendorBillPaymentMethod.CASH
    _populate_from_form(bill, _inventory_form(**{
        "item_new_product_name[]": "بيض",
        "item_new_product_sku[]": "PACK-SKU-1",
        "item_new_pack_name[]": "كرتونة",
        "item_new_pack_pieces[]": "30",
        "item_new_pack_sale_price[]": "120",
        "item_quantity[]": "10",
        "item_unit_price[]": "90",
    }))
    db.session.commit()

    item = bill.items[0]
    v = db.session.get(ProductVariant, item.variant_id)
    p = db.session.get(Product, v.product_id)
    units = ProductUnit.query.filter_by(product_id=p.id).all()
    base = [u for u in units if u.is_base]
    pack = [u for u in units if not u.is_base]
    assert len(base) == 1, f"expected 1 base unit, got {len(base)}"
    assert len(pack) == 1, f"expected 1 pack unit, got {len(pack)}"
    assert pack[0].unit_name == "كرتونة"
    assert float(pack[0].conversion_factor) == 30.0
    # The line must point at the pack unit, or posting treats 10 as 10
    # pieces — the whole point of the ticket.
    assert item.unit_id == pack[0].id, \
        f"line unit_id={item.unit_id}, pack unit={pack[0].id}"

    post_vendor_bill(bill)
    db.session.commit()
    bal = StockBalance.query.filter_by(
        variant_id=v.id, warehouse_id=_STATE["warehouse_id"]).first()
    assert bal is not None, "nothing landed in stock"
    assert float(bal.qty) == 300.0, f"expected 300 قطعة, got {bal.qty}"
    # 10 × 90 = 900 spread over 300 pieces = 3.00 each.
    assert abs(float(bal.value) / float(bal.qty) - 3.0) < 0.001, \
        f"per-piece cost = {float(bal.value) / float(bal.qty)}"
    return "300 قطعة @ 3.00 (was 10 @ 90.00)"


@check("8. pack name equal to the base unit name is refused")
def _():
    from app.models import Product
    from app.services.ledger import LedgerError
    from app.routes.vendor_bills import _populate_from_form
    before = Product.query.filter_by(
        company_id=_STATE["company_id"]).count()
    bill = _new_bill("BS-PACK-2")
    try:
        _populate_from_form(bill, _inventory_form(**{
            "item_new_product_name[]": "منتج مكرر",
            "item_new_product_sku[]": "DUP-SKU-1",
            # ensure_base_unit's default name — colliding with it would
            # make convert_to_base ambiguous.
            "item_new_pack_name[]": "قطعة",
            "item_new_pack_pieces[]": "12",
        }))
    except LedgerError as e:
        db.session.rollback()
        after = Product.query.filter_by(
            company_id=_STATE["company_id"]).count()
        assert after == before, \
            f"half-created product left behind ({before} → {after})"
        return f"refused + rolled back: {e}"
    db.session.rollback()
    raise AssertionError("collision with the base unit name was accepted")


@check("9. no pack fields → base unit only, quantity stays in pieces")
def _():
    from app.models import (
        Product, ProductVariant, ProductUnit, StockBalance,
        VendorBillPaymentMethod,
    )
    from app.routes.vendor_bills import _populate_from_form
    from app.services.vendor_bills import post_vendor_bill
    bill = _new_bill("BS-NOPACK-1")
    bill.payment_method = VendorBillPaymentMethod.CASH
    _populate_from_form(bill, _inventory_form(**{
        "item_new_product_name[]": "قلم",
        "item_new_product_sku[]": "NOPACK-SKU-1",
        "item_new_pack_pieces[]": "",     # buying by the piece
        "item_quantity[]": "7",
        "item_unit_price[]": "5",
    }))
    db.session.commit()
    item = bill.items[0]
    v = db.session.get(ProductVariant, item.variant_id)
    p = db.session.get(Product, v.product_id)
    units = ProductUnit.query.filter_by(product_id=p.id).all()
    assert len(units) == 1 and units[0].is_base, \
        f"expected base unit only, got {[u.unit_name for u in units]}"
    assert item.unit_id is None, \
        f"no pack was asked for but unit_id={item.unit_id}"
    post_vendor_bill(bill)
    db.session.commit()
    bal = StockBalance.query.filter_by(
        variant_id=v.id, warehouse_id=_STATE["warehouse_id"]).first()
    assert float(bal.qty) == 7.0, f"expected 7, got {bal.qty}"
    return "base unit only; 7 stayed 7"


# ─── Recurring prefill routing ────────────────────────────────────────
@check("10. single-type prefill skips the chooser; mixed stops on it")
def _():
    from app.models import (
        VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, BillLineType, RecurringBill, Account,
    )
    cid = _STATE["company_id"]
    exp_acc = (Account.query.filter_by(company_id=cid, code="5100").first()
               or Account.query.filter_by(company_id=cid, code="5200").first())
    inv_acc = Account.query.filter_by(company_id=cid, code="1300").first()

    def _mk_recurring(number, kinds):
        b = VendorBill(
            company_id=cid, vendor_id=_STATE["vendor_id"], number=number,
            issue_date=date.today(), due_date=date.today(),
            status=VendorBillStatus.DRAFT,
            payment_method=VendorBillPaymentMethod.BANK,
            tax_rate=Decimal("0"),
        )
        db.session.add(b); db.session.flush()
        for lt in kinds:
            db.session.add(VendorBillItem(
                bill_id=b.id, description=f"بند {lt.value}", line_type=lt,
                account_id=(inv_acc.id if lt == BillLineType.INVENTORY
                            else exp_acc.id),
                quantity=Decimal("1"), unit_price=Decimal("100"),
            ))
        db.session.flush()
        b.recalc()
        rb = RecurringBill(
            company_id=cid, source_bill_id=b.id,
            vendor_id=_STATE["vendor_id"], amount=b.total or Decimal("0"),
            currency="SAR", interval_unit="MONTH", interval_count=1,
            start_date=date.today(), active=True,
        )
        db.session.add(rb); db.session.commit()
        return rb.id

    single = _mk_recurring("BS-RB-1", [BillLineType.EXPENSE])
    mixed = _mk_recurring("BS-RB-2", [BillLineType.EXPENSE,
                                      BillLineType.INVENTORY])
    client = _client()

    r = client.get(f"/vendor-bills/new?from_recurring={single}")
    assert r.status_code == 302, f"single: status={r.status_code}"
    loc = r.headers.get("Location", "")
    assert "/vendor-bills/new/expense" in loc, f"single went to {loc}"
    assert f"from_recurring={single}" in loc, f"prefill id dropped: {loc}"

    r = client.get(f"/vendor-bills/new?from_recurring={mixed}")
    assert r.status_code == 200, f"mixed: status={r.status_code}"
    body = r.get_data(as_text=True)
    assert "أنواع مختلفة" in body, "mixed template got no warning"

    # And the typed form keeps only the lines it can represent.
    r = client.get(f"/vendor-bills/new/inventory?from_recurring={mixed}")
    assert r.status_code == 200, f"inventory form: status={r.status_code}"
    body = r.get_data(as_text=True)
    assert "INVENTORY" in body, "inventory line missing from prefill"
    assert "بند EXPENSE" not in body, \
        "expense line leaked into the inventory form's prefill"
    return "single → expense form; mixed → chooser warning + filtered"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        from tests._orphan_sweep import preflight
        preflight()
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
                if "company_id" in _STATE:
                    _teardown_company(_STATE["company_id"])
                    print("\n(cleaned up fixture company)")
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
