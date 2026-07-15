#!/usr/bin/env python3
"""MARSOUD-VENDOR-SUBCAT-BACKFILL (Abdelhamid 2026-07-15).

Follow-up: the Sub Category ticket only let you set a sub-cat on
NEW bill lines. Old bills couldn't be tagged retroactively because:
  · DRAFT edit works but is one bill at a time
  · POSTED edit was frozen to "cosmetic-only" fields
Abdelhamid wants a bulk tool for legacy bills.

Two fixes in this batch:
  A. Extend the POSTED-bill edit path to accept sub-category
     updates (safe — taxonomy tag, no ledger impact).
  B. New bulk page /vendors/<id>/bill-items/categorize showing all
     lines for that vendor with an inline sub-cat dropdown + a
     save-all button. Filter for uncategorized only.

Checks:
  1. POSTED bill's /edit accepts item_subcat_<id> and persists it.
  2. POSTED bill's /edit rejects a cross-vendor sub-cat id
     (silently drops to NULL — no leak).
  3. POSTED bill's /edit still refuses to change amounts/accounts
     (ledger integrity untouched — regression guard).
  4. GET /vendors/<id>/bill-items/categorize lists every bill line
     for that vendor.
  5. ?filter=uncategorized narrows to lines with sub_category_id IS NULL.
  6. POST /categorize with a set of item_subcat_<id> updates them
     in one shot.
  7. POST /categorize with an empty value clears the sub-category
     back to NULL.
  8. Save-all validates each sub-cat against the vendor — cross-vendor
     ids are ignored, others still applied.
  9. HTTP page renders — smoke check for the template.
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
            "DELETE FROM users WHERE email LIKE 'vsb-%@x.test'"))
        # Orphan sweep.
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

    for name in ("__VSB__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__VSB__", base_currency="SAR", vat_rate=15)
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

    owner = _mk("vsb-owner@x.test", "owner")

    v_claude = Vendor(company_id=a.id, name="Claude", is_active=True)
    v_google = Vendor(company_id=a.id, name="Google", is_active=True)
    db.session.add_all([v_claude, v_google]); db.session.flush()

    # Sub-cats for Claude (2) + Google (1).
    sc_ab = VendorSubCategory(
        company_id=a.id, vendor_id=v_claude.id, name="Abdelhamid",
        is_active=True, created_by_id=owner.id,
    )
    sc_rf = VendorSubCategory(
        company_id=a.id, vendor_id=v_claude.id, name="Rofida",
        is_active=True, created_by_id=owner.id,
    )
    sc_workspace = VendorSubCategory(
        company_id=a.id, vendor_id=v_google.id, name="Workspace",
        is_active=True, created_by_id=owner.id,
    )
    db.session.add_all([sc_ab, sc_rf, sc_workspace]); db.session.flush()

    exp_acc = Account.query.filter_by(company_id=a.id, code="5210").first()

    # Two POSTED bills for Claude with 3 uncategorized lines total.
    from app.services.numbering import next_number
    b1 = VendorBill(
        company_id=a.id, number=next_number(a.id, "VENDOR_BILL"),
        vendor_id=v_claude.id,
        issue_date=date.today(), due_date=date.today() + timedelta(days=30),
        payment_method=VendorBillPaymentMethod.CASH, currency="SAR",
        status=VendorBillStatus.POSTED,
    )
    db.session.add(b1); db.session.flush()
    it1 = VendorBillItem(
        bill_id=b1.id, description="claude line 1",
        line_type=BillLineType.EXPENSE, account_id=exp_acc.id,
        quantity=1, unit_price=100, line_total=100,
    )
    it2 = VendorBillItem(
        bill_id=b1.id, description="claude line 2",
        line_type=BillLineType.EXPENSE, account_id=exp_acc.id,
        quantity=1, unit_price=200, line_total=200,
    )
    db.session.add_all([it1, it2])
    b2 = VendorBill(
        company_id=a.id, number=next_number(a.id, "VENDOR_BILL"),
        vendor_id=v_claude.id,
        issue_date=date.today(), due_date=date.today() + timedelta(days=30),
        payment_method=VendorBillPaymentMethod.CASH, currency="SAR",
        status=VendorBillStatus.POSTED,
    )
    db.session.add(b2); db.session.flush()
    it3 = VendorBillItem(
        bill_id=b2.id, description="claude line 3",
        line_type=BillLineType.EXPENSE, account_id=exp_acc.id,
        quantity=1, unit_price=300, line_total=300,
    )
    db.session.add(it3)
    # One PRE-CATEGORIZED item to prove filter works.
    it4 = VendorBillItem(
        bill_id=b2.id, description="claude line 4 (pre-tagged)",
        line_type=BillLineType.EXPENSE, account_id=exp_acc.id,
        quantity=1, unit_price=50, line_total=50,
        sub_category_id=sc_ab.id,
    )
    db.session.add(it4)
    db.session.commit()

    _STATE.update(
        a_id=a.id, owner_id=owner.id, exp_acc_id=exp_acc.id,
        claude_id=v_claude.id, google_id=v_google.id,
        sc_ab_id=sc_ab.id, sc_rf_id=sc_rf.id,
        sc_workspace_id=sc_workspace.id,
        b1_id=b1.id, b2_id=b2.id,
        it1_id=it1.id, it2_id=it2.id, it3_id=it3.id, it4_id=it4.id,
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


# ─── POSTED /edit path ────────────────────────────────────────────
@check("1. POSTED bill /edit accepts item_subcat_<id> and persists it")
def _():
    from app.models import VendorBillItem
    client = _login()
    r = client.post(
        f"/vendor-bills/{_STATE['b1_id']}/edit",
        data={
            "vendor_id": str(_STATE["claude_id"]),
            "notes": "backfill test",
            f"item_desc_{_STATE['it1_id']}": "claude line 1",
            f"item_subcat_{_STATE['it1_id']}": str(_STATE["sc_ab_id"]),
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 302), \
        f"status={r.status_code} body={r.data[:200]!r}"
    it1 = db.session.get(VendorBillItem, _STATE["it1_id"])
    db.session.refresh(it1)
    assert it1.sub_category_id == _STATE["sc_ab_id"], \
        f"got {it1.sub_category_id}"
    return "item1 → Abdelhamid via POSTED edit"


@check("2. POSTED /edit silently drops a cross-vendor sub-cat id")
def _():
    from app.models import VendorBillItem
    client = _login()
    r = client.post(
        f"/vendor-bills/{_STATE['b1_id']}/edit",
        data={
            "vendor_id": str(_STATE["claude_id"]),
            f"item_desc_{_STATE['it2_id']}": "claude line 2",
            # Google's sub-cat on a Claude bill → must be dropped.
            f"item_subcat_{_STATE['it2_id']}": str(_STATE["sc_workspace_id"]),
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 302)
    it2 = db.session.get(VendorBillItem, _STATE["it2_id"])
    db.session.refresh(it2)
    assert it2.sub_category_id is None, \
        f"cross-vendor sub-cat leaked: {it2.sub_category_id}"
    return "cross-vendor id dropped to NULL"


@check("3. POSTED /edit still refuses to change amounts (regression)")
def _():
    from app.models import VendorBillItem
    it1 = db.session.get(VendorBillItem, _STATE["it1_id"])
    before_total = float(it1.line_total)
    client = _login()
    r = client.post(
        f"/vendor-bills/{_STATE['b1_id']}/edit",
        data={
            "vendor_id": str(_STATE["claude_id"]),
            # Try to slip an item_unit_price[] through.
            "item_unit_price[]": "99999",
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 302)
    it1 = db.session.get(VendorBillItem, _STATE["it1_id"])
    db.session.refresh(it1)
    assert float(it1.line_total) == before_total, \
        f"line total changed: {before_total} → {it1.line_total}"
    return "amounts stayed frozen"


# ─── Bulk categorize page ─────────────────────────────────────────
@check("4. GET /categorize lists all bill lines for the vendor")
def _():
    r = _login().get(
        f"/vendors/{_STATE['claude_id']}/bill-items/categorize",
        follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.data.decode("utf-8", "ignore")
    for desc in ("claude line 1", "claude line 2", "claude line 3",
                  "claude line 4 (pre-tagged)"):
        assert desc in body, f"missing line: {desc}"
    return "all 4 Claude lines listed"


@check("5. ?filter=uncategorized narrows to sub_category_id IS NULL")
def _():
    r = _login().get(
        f"/vendors/{_STATE['claude_id']}/bill-items/categorize?filter=uncategorized",
        follow_redirects=False)
    assert r.status_code == 200
    body = r.data.decode("utf-8", "ignore")
    # After check 1, item1 has Abdelhamid — so filter=uncategorized
    # should hide it. Also item4 was seeded pre-tagged.
    assert "claude line 3" in body, "uncategorized line 3 missing"
    assert "claude line 4 (pre-tagged)" not in body, \
        "pre-tagged line leaked into uncategorized filter"
    return "uncategorized filter works"


@check("6. POST /categorize updates a batch of items in one shot")
def _():
    from app.models import VendorBillItem
    # Update it2 → Rofida AND it3 → Abdelhamid in the same POST.
    r = _login().post(
        f"/vendors/{_STATE['claude_id']}/bill-items/categorize",
        data={
            f"item_subcat_{_STATE['it2_id']}": str(_STATE["sc_rf_id"]),
            f"item_subcat_{_STATE['it3_id']}": str(_STATE["sc_ab_id"]),
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 302)
    it2 = db.session.get(VendorBillItem, _STATE["it2_id"])
    it3 = db.session.get(VendorBillItem, _STATE["it3_id"])
    db.session.refresh(it2); db.session.refresh(it3)
    assert it2.sub_category_id == _STATE["sc_rf_id"], \
        f"it2: {it2.sub_category_id}"
    assert it3.sub_category_id == _STATE["sc_ab_id"], \
        f"it3: {it3.sub_category_id}"
    return "2 items updated in one POST"


@check("7. Empty sub-cat value clears the category back to NULL")
def _():
    from app.models import VendorBillItem
    r = _login().post(
        f"/vendors/{_STATE['claude_id']}/bill-items/categorize",
        data={f"item_subcat_{_STATE['it4_id']}": ""},
        follow_redirects=False,
    )
    assert r.status_code in (200, 302)
    it4 = db.session.get(VendorBillItem, _STATE["it4_id"])
    db.session.refresh(it4)
    assert it4.sub_category_id is None, \
        f"empty value didn't clear: {it4.sub_category_id}"
    return "empty POST value cleared to NULL"


@check("8. Cross-vendor id in bulk POST is ignored; others still applied")
def _():
    from app.models import VendorBillItem
    r = _login().post(
        f"/vendors/{_STATE['claude_id']}/bill-items/categorize",
        data={
            # Valid: it4 → Abdelhamid
            f"item_subcat_{_STATE['it4_id']}": str(_STATE["sc_ab_id"]),
            # Invalid: it3 → Google's Workspace (cross-vendor)
            f"item_subcat_{_STATE['it3_id']}": str(_STATE["sc_workspace_id"]),
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 302)
    it3 = db.session.get(VendorBillItem, _STATE["it3_id"])
    it4 = db.session.get(VendorBillItem, _STATE["it4_id"])
    db.session.refresh(it3); db.session.refresh(it4)
    # it4 should update to Abdelhamid; it3 should stay at whatever
    # check 6 left it (Abdelhamid) — NOT reset by an invalid value.
    assert it4.sub_category_id == _STATE["sc_ab_id"], \
        f"valid update failed: {it4.sub_category_id}"
    assert it3.sub_category_id == _STATE["sc_ab_id"], \
        f"invalid update leaked through: {it3.sub_category_id}"
    return "valid applied, invalid ignored"


@check("9. Page renders without errors (smoke test)")
def _():
    r = _login().get(
        f"/vendors/{_STATE['claude_id']}/bill-items/categorize",
        follow_redirects=False)
    assert r.status_code == 200
    body = r.data.decode("utf-8", "ignore")
    assert "تصنيف بنود" in body or "التصنيف" in body
    assert "حفظ الكل" in body
    return "page renders OK"


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
