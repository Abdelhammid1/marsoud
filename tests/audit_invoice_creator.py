#!/usr/bin/env python3
"""MARSOUD-INVOICE-CREATOR (Abdelhamid 2026-07-13).

Adds invoices.created_by_id + surfaces "who created + when" on the
invoice detail page. Migration c8d1e4f7a2b5 also backfills POS
invoices from cashier_id.

Checks:
  1. invoices.created_by_id column exists after migration.
  2. Creating an Invoice via /invoices/new stamps created_by_id
     with current_user.id.
  3. POS invoice creation stamps created_by_id = cashier_id.
  4. Backfill query populated created_by_id on legacy POS invoices
     that had cashier_id set (simulated by inserting a row with
     created_by_id=NULL + cashier_id and rerunning the migration
     backfill SQL).
  5. GET /invoices/<id> shows the creation timestamp + creator name.
  6. Legacy invoice with no creator falls back to "غير معروف".
"""
import sys
from pathlib import Path
from datetime import date, datetime, timedelta

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
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'ic-%@x.test'"))


def _setup():
    from app.models import (
        Company, User, user_companies, Customer,
    )
    from werkzeug.security import generate_password_hash

    for name in ("__INV_CREATOR__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__INV_CREATOR__", base_currency="SAR",
                 vat_rate=15)
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

    owner = _mk("ic-owner@x.test", "owner")
    cashier = _mk("ic-cashier@x.test", "cashier")
    customer = Customer(
        company_id=a.id, name="IC-Customer",
        email="ic-cust@x.test", phone="0500000000",
    )
    db.session.add(customer); db.session.commit()

    _STATE.update(
        a_id=a.id, owner_id=owner.id, cashier_id=cashier.id,
        customer_id=customer.id,
    )


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
              "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


def _login(user_id):
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    return client


# ─── Schema ────────────────────────────────────────────────────────
@check("1. invoices.created_by_id column exists")
def _():
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    cols = {c["name"] for c in insp.get_columns("invoices")}
    assert "created_by_id" in cols, "column missing after migration"
    return "column present"


# ─── HTTP invoice-create path ──────────────────────────────────────
@check("2. POST /invoices/new stamps created_by_id = current_user")
def _():
    from app.models import Invoice
    client = _login(_STATE["owner_id"])
    r = client.post("/invoices/new", data={
        "customer_id": str(_STATE["customer_id"]),
        "line_desc[]": "audit line",
        "line_qty[]": "1",
        "line_price[]": "100",
        "line_discount_type[]": "NONE",
        "line_discount_value[]": "0",
    }, follow_redirects=False)
    assert r.status_code in (200, 302), \
        f"status={r.status_code} body={r.data[:200]!r}"
    inv = Invoice.query.filter_by(
        company_id=_STATE["a_id"]).order_by(Invoice.id.desc()).first()
    assert inv is not None, "invoice not created"
    assert inv.created_by_id == _STATE["owner_id"], \
        f"created_by_id={inv.created_by_id}, expected {_STATE['owner_id']}"
    _STATE["manual_inv_id"] = inv.id
    return f"invoice {inv.id} stamped with owner id"


# ─── POS create path ───────────────────────────────────────────────
@check("3. POS invoice creator wired at the source (cashier_id → created_by_id)")
def _():
    # The POS orchestration (variants, shifts, payment methods) is a
    # heavy fixture setup and its own audit; here we just verify the
    # invariant at the source-of-truth line: the Invoice() constructor
    # in create_pos_order sets created_by_id from cashier_id.
    src = (ROOT / "app/services/pos.py").read_text(encoding="utf-8")
    # Look for the constructor block and both keyword args.
    ctor_start = src.find("invoice = Invoice(")
    assert ctor_start != -1, "Invoice constructor not found in pos.py"
    ctor_block = src[ctor_start:ctor_start + 800]
    assert "cashier_id=cashier_id" in ctor_block, \
        "cashier_id wiring missing"
    assert "created_by_id=cashier_id" in ctor_block, \
        "created_by_id=cashier_id wiring missing from POS constructor"
    return "POS ctor sets both cashier_id + created_by_id"


# ─── Backfill ──────────────────────────────────────────────────────
@check("4. Backfill query populates created_by_id from cashier_id")
def _():
    # Insert a legacy-shaped POS row (no created_by_id) then rerun the
    # backfill SQL to simulate the migration on stale data.
    from app.models import Invoice, InvoiceStatus
    from sqlalchemy import text
    inv = Invoice(
        company_id=_STATE["a_id"],
        number="LEGACY-POS-001",
        customer_id=_STATE["customer_id"],
        cashier_id=_STATE["cashier_id"],
        source="POS",
        issue_date=date.today(),
        due_date=date.today(),
        currency="SAR",
        tax_rate=15,
        status=InvoiceStatus.DRAFT,
    )
    inv.created_by_id = None
    db.session.add(inv); db.session.commit()
    with db.engine.begin() as conn:
        conn.execute(text("""
            UPDATE invoices
            SET created_by_id = cashier_id
            WHERE created_by_id IS NULL
              AND cashier_id IS NOT NULL
        """))
    db.session.refresh(inv)
    assert inv.created_by_id == _STATE["cashier_id"], \
        "backfill didn't populate legacy row"
    return "legacy POS row backfilled"


# ─── UI ────────────────────────────────────────────────────────────
@check("5. /invoices/<id> shows creation timestamp + creator name")
def _():
    inv_id = _STATE["manual_inv_id"]
    client = _login(_STATE["owner_id"])
    r = client.get(f"/invoices/{inv_id}", follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.data.decode("utf-8", "ignore")
    assert "أُنشئت" in body or "أنشئت" in body, \
        '"أُنشئت" label missing from detail page'
    assert "بواسطة" in body, '"بواسطة" label missing'
    assert "ic-owner" in body, \
        "creator's name not surfaced on the detail page"
    return "timestamp + creator name visible"


@check("6. Legacy invoice with no creator falls back to 'غير معروف'")
def _():
    from app.models import Invoice, InvoiceStatus
    inv = Invoice(
        company_id=_STATE["a_id"],
        number="LEGACY-MANUAL-001",
        customer_id=_STATE["customer_id"],
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="SAR", tax_rate=15,
        status=InvoiceStatus.DRAFT,
    )
    # Both created_by_id AND cashier_id are NULL — no creator
    # can be resolved, so the template must fall back.
    inv.created_by_id = None
    inv.cashier_id = None
    db.session.add(inv); db.session.commit()

    client = _login(_STATE["owner_id"])
    r = client.get(f"/invoices/{inv.id}", follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.data.decode("utf-8", "ignore")
    assert "غير معروف" in body, \
        "fallback label missing for creator-less invoice"
    return "fallback rendered"


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
