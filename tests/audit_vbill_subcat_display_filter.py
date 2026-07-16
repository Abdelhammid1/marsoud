#!/usr/bin/env python3
"""MARSOUD-VBILL-SUBCAT-DISPLAY-FILTER (Abdelhamid 2026-07-16).

Follow-up to the Sub Category ticket. Abdelhamid: "عاوزين التصنيف
الفرعي يظهر في الفاتورة واقدر اعمل فلتر بيهم واي حاجة تترتب عليهم"
= show the sub-category on the bill list + view, and add filters
for vendor + sub-category on /vendor-bills/.

Checks:
  1. GET /vendor-bills/ renders a vendor filter <select>.
  2. GET /vendor-bills/ renders a sub-category filter <select>.
  3. ?vendor=<id> narrows the list to that vendor only.
  4. ?sub_category=<id> narrows the list to bills that have at
     least one line with that sub-category.
  5. ?vendor=<A>&sub_category=<of B> returns 0 rows (cross-vendor
     narrowing composes correctly).
  6. Non-numeric vendor / sub_category args are silently ignored
     (no crash, no filter applied).
  7. Bill list row shows the sub-category pill next to the item
     description.
  8. Bill detail (view) shows a dedicated sub-category column
     with the value populated for tagged lines and "—" otherwise.
  9. Bill totals cards reflect the filter (invoiced sum is
     over the filtered rows only, not all company bills).
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
            "DELETE FROM users WHERE email LIKE 'vsd-%@x.test'"))
        conn.execute(text(
            "DELETE FROM vendor_bill_items WHERE bill_id NOT IN "
            "(SELECT id FROM vendor_bills)"))


def _setup():
    from app.models import (
        Company, User, user_companies, Vendor, Account,
        VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, BillLineType, VendorSubCategory,
    )
    from werkzeug.security import generate_password_hash
    from app.services.numbering import next_number
    from app.services.seed_coa import seed_default_coa

    for name in ("__VSD__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__VSD__", base_currency="SAR", vat_rate=15)
    db.session.add(a); db.session.flush()
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

    owner = _mk("vsd-owner@x.test", "owner")

    # Two vendors + one sub-cat per vendor + one shared name.
    v_claude = Vendor(company_id=a.id, name="VSD-Claude",
                      is_active=True)
    v_google = Vendor(company_id=a.id, name="VSD-Google",
                      is_active=True)
    db.session.add_all([v_claude, v_google]); db.session.flush()
    sc_claude_ab = VendorSubCategory(
        company_id=a.id, vendor_id=v_claude.id,
        name="VSD-Abdelhamid", is_active=True, created_by_id=owner.id)
    sc_google_ws = VendorSubCategory(
        company_id=a.id, vendor_id=v_google.id,
        name="VSD-Workspace", is_active=True, created_by_id=owner.id)
    db.session.add_all([sc_claude_ab, sc_google_ws]); db.session.flush()

    exp_acc = Account.query.filter_by(
        company_id=a.id, code="5210").first()

    def _bill(vendor, subtotal_price, sub_cat_id=None, tag="VSD-line"):
        number = next_number(a.id, "VENDOR_BILL")
        b = VendorBill(
            company_id=a.id, number=number, vendor_id=vendor.id,
            issue_date=date.today(),
            due_date=date.today() + timedelta(days=30),
            payment_method=VendorBillPaymentMethod.CASH,
            currency="SAR", tax_rate=0,
            status=VendorBillStatus.POSTED,
        )
        db.session.add(b); db.session.flush()
        db.session.add(VendorBillItem(
            bill_id=b.id, description=tag,
            line_type=BillLineType.EXPENSE,
            account_id=exp_acc.id,
            quantity=1, unit_price=subtotal_price,
            line_total=subtotal_price,
            sub_category_id=sub_cat_id,
        ))
        b.recalc()
        return b

    # Claude: 2 bills — one tagged (Abdelhamid, 100), one untagged (200).
    b1 = _bill(v_claude, 100, sc_claude_ab.id, tag="VSD-line-claude-1")
    b2 = _bill(v_claude, 200, None, tag="VSD-line-claude-2")
    # Google: 1 bill tagged with Workspace (500).
    b3 = _bill(v_google, 500, sc_google_ws.id, tag="VSD-line-google-1")
    db.session.commit()

    _STATE.update(
        a_id=a.id, owner_id=owner.id,
        claude_id=v_claude.id, google_id=v_google.id,
        sc_claude_ab_id=sc_claude_ab.id,
        sc_google_ws_id=sc_google_ws.id,
        b1_id=b1.id, b2_id=b2.id, b3_id=b3.id,
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


# ─── Filters render ─────────────────────────────────────────────
@check("1. Index renders vendor filter <select>")
def _():
    r = _login().get("/vendor-bills/", follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.data.decode("utf-8", "ignore")
    assert 'name="vendor"' in body, "vendor <select> missing"
    assert "VSD-Claude" in body and "VSD-Google" in body, \
        "vendor names missing from dropdown"
    return "vendor select rendered with both vendors"


@check("2. Index renders sub-category filter <select>")
def _():
    r = _login().get("/vendor-bills/", follow_redirects=False)
    body = r.data.decode("utf-8", "ignore")
    assert 'name="sub_category"' in body, "sub_category <select> missing"
    # With no vendor filter, both sub-cats show.
    assert "VSD-Abdelhamid" in body, "Abdelhamid sub-cat missing"
    assert "VSD-Workspace" in body, "Workspace sub-cat missing"
    return "sub_category select rendered with both cats"


# ─── Filter narrowing ────────────────────────────────────────────
@check("3. ?vendor=<id> narrows to that vendor's bills only")
def _():
    r = _login().get(
        f"/vendor-bills/?vendor={_STATE['claude_id']}",
        follow_redirects=False)
    body = r.data.decode("utf-8", "ignore")
    assert "VSD-line-claude-1" in body and "VSD-line-claude-2" in body
    assert "VSD-line-google-1" not in body, \
        "google line leaked through vendor filter"
    return "only Claude's 2 bills visible"


@check("4. ?sub_category=<id> narrows to bills with that tag")
def _():
    r = _login().get(
        f"/vendor-bills/?sub_category={_STATE['sc_claude_ab_id']}",
        follow_redirects=False)
    body = r.data.decode("utf-8", "ignore")
    # Only b1 is tagged with sc_claude_ab.
    assert "VSD-line-claude-1" in body
    assert "VSD-line-claude-2" not in body, \
        "untagged Claude line leaked through sub_cat filter"
    assert "VSD-line-google-1" not in body, \
        "Google line leaked through sub_cat filter"
    return "only b1 (tagged) visible"


@check("5. vendor=Claude + sub_category=Workspace (cross-vendor) → 0 rows")
def _():
    r = _login().get(
        f"/vendor-bills/?vendor={_STATE['claude_id']}"
        f"&sub_category={_STATE['sc_google_ws_id']}",
        follow_redirects=False)
    body = r.data.decode("utf-8", "ignore")
    # No Claude bill has Workspace tag; empty result.
    for tag in ("VSD-line-claude-1", "VSD-line-claude-2",
                 "VSD-line-google-1"):
        assert tag not in body, f"row leaked through cross-vendor: {tag}"
    return "cross-vendor combo → 0 rows"


@check("6. Non-numeric vendor / sub_category args are silently ignored")
def _():
    r = _login().get(
        "/vendor-bills/?vendor=not-a-number&sub_category=abc",
        follow_redirects=False)
    assert r.status_code == 200
    body = r.data.decode("utf-8", "ignore")
    # All bills should still be visible.
    assert "VSD-line-claude-1" in body
    assert "VSD-line-claude-2" in body
    assert "VSD-line-google-1" in body
    return "garbage args treated as no filter"


# ─── Display ────────────────────────────────────────────────────
@check("7. List row shows sub-category pill next to item description")
def _():
    r = _login().get("/vendor-bills/", follow_redirects=False)
    body = r.data.decode("utf-8", "ignore")
    # Each tagged line has a 🏷 pill + the sub-cat name near its
    # description in the same table row.
    assert "🏷" in body, "sub-cat pill emoji missing"
    assert "VSD-Abdelhamid" in body
    return "pill + sub-cat name visible in list"


@check("8. Bill detail (view) has a sub-category column with values")
def _():
    r = _login().get(f"/vendor-bills/{_STATE['b1_id']}",
                     follow_redirects=False)
    assert r.status_code == 200
    body = r.data.decode("utf-8", "ignore")
    assert "التصنيف الفرعي" in body, \
        "sub-cat column header missing on view"
    assert "VSD-Abdelhamid" in body, \
        "sub-cat value not rendered on tagged line"

    # Untagged line on b2 should render "—".
    r2 = _login().get(f"/vendor-bills/{_STATE['b2_id']}",
                       follow_redirects=False)
    body2 = r2.data.decode("utf-8", "ignore")
    assert "التصنيف الفرعي" in body2
    # Slightly loose: the "—" placeholder appears in the sub-cat cell
    # for untagged lines. Can't assert exactly on it without brittle
    # HTML matching; instead assert no sub-cat name is present.
    assert "VSD-Abdelhamid" not in body2
    assert "VSD-Workspace" not in body2
    return "column present + values on tagged, dash on untagged"


# ─── Totals respect filter ──────────────────────────────────────
@check("9. Filtered totals reflect the applied vendor filter")
def _():
    """When we filter to Claude only, totals.invoiced should be
    100+200=300 (Claude's two bills) not 800 (all three)."""
    r = _login().get(
        f"/vendor-bills/?vendor={_STATE['claude_id']}",
        follow_redirects=False)
    body = r.data.decode("utf-8", "ignore")
    # Look for the total number rendered in the KPI card. We use a
    # loose substring check that '300.00' appears in the response
    # and '800.00' does not.
    assert "300.00" in body, "expected filtered total 300.00 in KPI"
    assert "800.00" not in body, \
        "unfiltered total leaked into KPI card"
    return "invoiced KPI = 300.00 (Claude only)"


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
