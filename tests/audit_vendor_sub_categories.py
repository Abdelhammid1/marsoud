#!/usr/bin/env python3
"""MARSOUD-VENDOR-SUBCAT (Abdelhamid 2026-07-14).

Per-vendor sub-category taxonomy on vendor bill lines. Adds:
  · vendor_sub_categories table
  · vendor_bill_items.sub_category_id (nullable FK)
  · management UI at /vendors/<id>/sub-categories
  · JSON API at /vendors/<id>/sub-categories.json for the bill form
  · report at /reports/vendor-sub-categories

Acceptance criteria from the ticket:
  · new sub-category field on bill lines (existing description stays)
  · each vendor has its own taxonomy
  · categories load per vendor
  · report can group by vendor and by sub-category
  · nothing existing breaks
  · deleting a used category is blocked
  · deactivation is always safe

Checks:
  1. Schema — vendor_sub_categories table + vendor_bill_items.sub_category_id.
  2. Service.create_sub_category persists a row + rejects duplicate.
  3. Same name across DIFFERENT vendors is allowed.
  4. Service.rename honours the same uniqueness rule.
  5. Service.set_active toggles the flag.
  6. Service.delete blocked when the category is used in a bill.
  7. Service.delete succeeds when unused.
  8. JSON API returns ONLY active categories for the passed vendor;
     empty for a cross-tenant vendor id.
  9. Report groups totals by (vendor, sub-category); uncategorized
     lines land under "بدون تصنيف".
 10. Vendor-bill line save honours item_sub_category_id[]: a valid
     id from the SAME vendor lands on the line; a cross-vendor id
     is silently dropped to NULL.
 11. GET /vendors/<id>/sub-categories renders the management page.
 12. GET /reports/vendor-sub-categories renders the report page.
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
        conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"),
                     {"c": company_id})
        # vendor_bill_items has no company_id — wipe rows tied to
        # bills of this company BEFORE the generic sweep runs.
        # SQLite has foreign_keys=OFF by default (Flask-SQLAlchemy),
        # so cascade doesn't fire automatically.
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
            "DELETE FROM users WHERE email LIKE 'vsc-%@x.test'"))
        # Kill any orphan rows left over from prior interrupted runs:
        # bill items with no bill, bills with no company, sub-categories
        # with no vendor/company. Self-healing so a rerun after a
        # KeyboardInterrupt doesn't leak numbers into future runs.
        conn.execute(text(
            "DELETE FROM vendor_bill_items WHERE bill_id NOT IN "
            "(SELECT id FROM vendor_bills)"))
        conn.execute(text(
            "DELETE FROM vendor_bills WHERE company_id NOT IN "
            "(SELECT id FROM companies)"))
        conn.execute(text(
            "DELETE FROM vendor_sub_categories WHERE company_id NOT IN "
            "(SELECT id FROM companies) "
            "OR vendor_id NOT IN (SELECT id FROM vendors)"))


def _setup():
    from app.models import Company, User, user_companies, Vendor, Account
    from werkzeug.security import generate_password_hash

    for name in ("__VSC__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__VSC__", base_currency="SAR", vat_rate=15)
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

    owner = _mk("vsc-owner@x.test", "owner")

    v_claude = Vendor(
        company_id=a.id, name="Claude", email="c@x.test", is_active=True,
    )
    v_google = Vendor(
        company_id=a.id, name="Google", email="g@x.test", is_active=True,
    )
    db.session.add_all([v_claude, v_google]); db.session.commit()

    # Pick an expense leaf account for later bill-line inserts.
    exp_acc = Account.query.filter_by(
        company_id=a.id, code="5210").first()
    _STATE.update(
        a_id=a.id, owner_id=owner.id,
        claude_id=v_claude.id, google_id=v_google.id,
        exp_account_id=exp_acc.id if exp_acc else None,
    )


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


# ─── Schema ────────────────────────────────────────────────────────
@check("1. schema present: vendor_sub_categories + item.sub_category_id")
def _():
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    assert "vendor_sub_categories" in insp.get_table_names(), \
        "table missing"
    cols = {c["name"] for c in insp.get_columns("vendor_bill_items")}
    assert "sub_category_id" in cols, "sub_category_id column missing"
    return "table + column present"


# ─── Service ───────────────────────────────────────────────────────
@check("2. create_sub_category persists + rejects duplicate")
def _():
    from app.services.vendor_sub_categories import (
        create_sub_category, SubCategoryError,
    )
    sc = create_sub_category(
        company_id=_STATE["a_id"], vendor_id=_STATE["claude_id"],
        name="Abdelhamid", created_by_id=_STATE["owner_id"],
    )
    assert sc.id
    try:
        create_sub_category(
            company_id=_STATE["a_id"], vendor_id=_STATE["claude_id"],
            name="Abdelhamid",
        )
        assert False, "duplicate name accepted"
    except SubCategoryError as e:
        assert "موجود" in str(e)
    _STATE["sc_ab_id"] = sc.id
    return f"row {sc.id} created; duplicate rejected"


@check("3. same name allowed across different vendors")
def _():
    from app.services.vendor_sub_categories import create_sub_category
    sc = create_sub_category(
        company_id=_STATE["a_id"], vendor_id=_STATE["google_id"],
        name="Abdelhamid",
    )
    assert sc.id
    _STATE["sc_ab_google_id"] = sc.id
    return "Claude/Abdelhamid + Google/Abdelhamid coexist"


@check("4. rename_sub_category honours uniqueness")
def _():
    from app.services.vendor_sub_categories import (
        create_sub_category, rename_sub_category, SubCategoryError,
    )
    peer = create_sub_category(
        company_id=_STATE["a_id"], vendor_id=_STATE["claude_id"],
        name="Rofida",
    )
    _STATE["sc_rofida_id"] = peer.id
    # Rename to a name already taken on Claude → rejected.
    from app.models import VendorSubCategory
    sc = db.session.get(VendorSubCategory, peer.id)
    try:
        rename_sub_category(sc, name="Abdelhamid")
        assert False, "duplicate rename accepted"
    except SubCategoryError:
        pass
    # Rename to a fresh name → accepted.
    rename_sub_category(sc, name="Rofida-2")
    db.session.refresh(sc)
    assert sc.name == "Rofida-2"
    # Reset for later checks.
    rename_sub_category(sc, name="Rofida")
    return "rejected dup; accepted fresh; reset"


@check("5. set_active toggles the flag")
def _():
    from app.services.vendor_sub_categories import set_active
    from app.models import VendorSubCategory
    sc = db.session.get(VendorSubCategory, _STATE["sc_ab_id"])
    set_active(sc, False)
    db.session.refresh(sc)
    assert sc.is_active is False
    set_active(sc, True)
    db.session.refresh(sc)
    assert sc.is_active is True
    return "toggled off + on"


@check("6. delete blocked when the category is used in a bill")
def _():
    """Create a bill referencing sc_ab, then try to delete → refused."""
    from app.models import (
        VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, BillLineType,
    )
    from app.services.numbering import next_number
    from app.services.vendor_sub_categories import (
        delete_sub_category, SubCategoryError,
    )
    number = next_number(_STATE["a_id"], "VENDOR_BILL")
    bill = VendorBill(
        company_id=_STATE["a_id"], number=number,
        vendor_id=_STATE["claude_id"],
        issue_date=date.today(), due_date=date.today() + timedelta(days=30),
        payment_method=VendorBillPaymentMethod.CASH,
        currency="SAR",
        status=VendorBillStatus.DRAFT,
    )
    db.session.add(bill); db.session.flush()
    db.session.add(VendorBillItem(
        bill_id=bill.id, description="subscription",
        line_type=BillLineType.EXPENSE,
        account_id=_STATE["exp_account_id"],
        quantity=1, unit_price=100, line_total=100,
        sub_category_id=_STATE["sc_ab_id"],
    ))
    db.session.commit()
    _STATE["bill_id"] = bill.id
    from app.models import VendorSubCategory
    sc = db.session.get(VendorSubCategory, _STATE["sc_ab_id"])
    try:
        delete_sub_category(sc)
        assert False, "delete succeeded on a category in use"
    except SubCategoryError as e:
        assert "مستخدم" in str(e)
    return "delete blocked as expected"


@check("7. delete succeeds when unused")
def _():
    from app.services.vendor_sub_categories import (
        create_sub_category, delete_sub_category,
    )
    from app.models import VendorSubCategory
    sc = create_sub_category(
        company_id=_STATE["a_id"], vendor_id=_STATE["claude_id"],
        name="TempCategory",
    )
    delete_sub_category(sc)
    assert db.session.get(VendorSubCategory, sc.id) is None
    return "unused category hard-deleted"


# ─── API ───────────────────────────────────────────────────────────
@check("8. JSON API returns only active categories; safe cross-tenant")
def _():
    from app.services.vendor_sub_categories import set_active
    from app.models import VendorSubCategory
    # Deactivate one of Claude's — must not surface in the API.
    sc = db.session.get(VendorSubCategory, _STATE["sc_rofida_id"])
    set_active(sc, False)

    r = _login().get(
        f"/vendors/{_STATE['claude_id']}/sub-categories.json",
        follow_redirects=False,
    )
    assert r.status_code == 200
    data = r.get_json()
    names = {row["name"] for row in data}
    assert "Abdelhamid" in names, "active category missing from API"
    assert "Rofida" not in names, "deactivated category leaked into API"

    # A vendor id we don't own returns []. Simulate by creating another
    # company's vendor and calling with its id.
    from app.models import Company, Vendor
    other = Company(name="__VSC_OTHER__", base_currency="SAR")
    db.session.add(other); db.session.flush()
    v_other = Vendor(company_id=other.id, name="Other", is_active=True)
    db.session.add(v_other); db.session.commit()
    r2 = _login().get(
        f"/vendors/{v_other.id}/sub-categories.json",
        follow_redirects=False,
    )
    assert r2.status_code == 200 and r2.get_json() == [], \
        "cross-tenant vendor id leaked data"
    # Cleanup this side company.
    db.session.delete(v_other); db.session.commit()
    db.session.delete(other); db.session.commit()

    # Reset Rofida for later checks.
    set_active(sc, True)
    return "API scoped + active-only"


# ─── Report ────────────────────────────────────────────────────────
@check("9. Report groups totals by (vendor, sub-category)")
def _():
    """Insert one more bill line with NO sub-category so uncategorized
    lines get their own bucket."""
    from app.models import (
        VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, BillLineType,
    )
    from app.services.numbering import next_number
    from app.services.vendor_sub_categories import report_totals_by_vendor
    number = next_number(_STATE["a_id"], "VENDOR_BILL")
    bill = VendorBill(
        company_id=_STATE["a_id"], number=number,
        vendor_id=_STATE["google_id"],
        issue_date=date.today(), due_date=date.today() + timedelta(days=30),
        payment_method=VendorBillPaymentMethod.CASH, currency="SAR",
        status=VendorBillStatus.DRAFT,
    )
    db.session.add(bill); db.session.flush()
    db.session.add(VendorBillItem(
        bill_id=bill.id, description="workspace",
        line_type=BillLineType.EXPENSE,
        account_id=_STATE["exp_account_id"],
        quantity=1, unit_price=250, line_total=250,
        sub_category_id=_STATE["sc_ab_google_id"],
    ))
    db.session.add(VendorBillItem(
        bill_id=bill.id, description="uncategorized item",
        line_type=BillLineType.EXPENSE,
        account_id=_STATE["exp_account_id"],
        quantity=1, unit_price=75, line_total=75,
        sub_category_id=None,
    ))
    db.session.commit()

    rows = report_totals_by_vendor(_STATE["a_id"])
    # Sanity: Claude has one entry (Abdelhamid, 100), Google has two
    # entries (Abdelhamid, 250 + بدون تصنيف, 75).
    per_vendor = {}
    for r in rows:
        per_vendor.setdefault(r["vendor_name"], []).append(
            (r["sub_category_name"], r["total"]))
    assert ("Abdelhamid", 100.0) in per_vendor.get("Claude", []), \
        f"missing Claude/Abdelhamid line: {per_vendor}"
    google_map = dict(per_vendor.get("Google", []))
    assert google_map.get("Abdelhamid") == 250.0, \
        f"missing Google/Abdelhamid=250: {google_map}"
    assert google_map.get("بدون تصنيف") == 75.0, \
        f"missing Google/بدون تصنيف=75: {google_map}"
    return "grouping correct + uncategorized bucket present"


# ─── Form save ─────────────────────────────────────────────────────
@check("10. bill line save honours item_sub_category_id[]; cross-vendor id → NULL")
def _():
    """Post a new draft bill to /vendor-bills/new with two lines:
    one carrying a valid Claude sub-category id, one carrying an id
    that belongs to Google (cross-vendor). The Google-id line should
    save as NULL, not with the wrong category."""
    from app.models import VendorBill, VendorBillItem
    r = _login().post("/vendor-bills/new", data={
        "vendor_id": str(_STATE["claude_id"]),
        "payment_method": "CASH",
        "issue_date": date.today().isoformat(),
        "due_date": (date.today() + timedelta(days=30)).isoformat(),
        "tax_rate": "0",
        # Two lines.
        "item_description[]": ["line-with-good-subcat",
                                 "line-with-cross-vendor-subcat"],
        "item_line_type[]": ["EXPENSE", "EXPENSE"],
        "item_account_id[]": [str(_STATE["exp_account_id"]),
                                str(_STATE["exp_account_id"])],
        "item_quantity[]": ["1", "1"],
        "item_unit_price[]": ["50", "80"],
        # First: legit Claude sub-cat. Second: a Google sub-cat id
        # attached to a Claude bill.
        "item_sub_category_id[]": [str(_STATE["sc_ab_id"]),
                                     str(_STATE["sc_ab_google_id"])],
    }, follow_redirects=False)
    assert r.status_code in (200, 302), \
        f"status={r.status_code} body={r.data[:200]!r}"
    bill = VendorBill.query.filter_by(
        company_id=_STATE["a_id"], vendor_id=_STATE["claude_id"],
    ).order_by(VendorBill.id.desc()).first()
    lines = VendorBillItem.query.filter_by(
        bill_id=bill.id).order_by(VendorBillItem.id).all()
    assert len(lines) == 2, f"got {len(lines)} lines"
    good, bad = lines
    assert good.sub_category_id == _STATE["sc_ab_id"], \
        f"first line lost its category: {good.sub_category_id}"
    assert bad.sub_category_id is None, \
        f"cross-vendor category leaked in: {bad.sub_category_id}"
    return "line 1 saved with Claude/Abdelhamid; line 2 dropped to NULL"


# ─── UI ────────────────────────────────────────────────────────────
@check("11. GET /vendors/<id>/sub-categories renders the management page")
def _():
    r = _login().get(
        f"/vendors/{_STATE['claude_id']}/sub-categories",
        follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.data.decode("utf-8", "ignore")
    assert "Abdelhamid" in body
    assert "التصنيفات الفرعية" in body or "تصنيفات فرعية" in body
    return "management page renders + shows Abdelhamid"


@check("12. GET /reports/vendor-sub-categories renders totals table")
def _():
    r = _login().get(
        "/reports/vendor-sub-categories", follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.data.decode("utf-8", "ignore")
    assert "Claude" in body and "Google" in body, \
        "vendors missing from report body"
    assert "Abdelhamid" in body
    assert "بدون تصنيف" in body
    return "report renders + shows all buckets"


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
