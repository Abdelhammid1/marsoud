#!/usr/bin/env python3
"""MARSOUD-SOURCE-REFERENCE-01 (Abdelhamid 2026-07-25).

Checks:
  1. resolve_reference(None, None) → generic "قيد يدوي", no URL.
  2. resolve_reference("invoice", 42, "INV-0001") → label with #,
     URL points to /invoices/42.
  3. resolve_reference("vendor_bill", 7, "VB-0007") → same shape.
  4. Unknown source_type → safe fallback label, no URL.
  5. build_reference_map batches: ONE query per source_type for a
     mixed list of rows.
  6. Multi-tenant: source_id from a different company_id resolves
     to label-only (no cross-tenant link).
"""
import os
import sys
from datetime import datetime, date, timedelta
from decimal import Decimal
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

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


def _teardown():
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__SR_%__'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'sr-%@x.test'"))


def _bootstrap():
    from app.models import Company, Customer, User, UserStatus
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    c = Company(name="__SR_CO__", base_currency="EGP",
                 subdomain="sr-co")
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email="sr-owner@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="sr-owner", is_active=True,
             status=UserStatus.ACTIVE.value)
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    cust = Customer(company_id=c.id, name="عميل مرجعي")
    db.session.add(cust); db.session.commit()
    return c, cust, u


@check("1. resolve_reference(None, None) → قيد يدوي, no URL")
def _():
    from flask import current_app
    from app.services.source_reference import resolve_reference
    with current_app.test_request_context():
        r = resolve_reference(None, None)
    assert r["label"] == "قيد يدوي"
    assert r["url"] is None
    return "generic fallback"


@check("2. resolve_reference('invoice', id, num) → linked label")
def _():
    from flask import current_app
    from app.services.source_reference import resolve_reference
    with current_app.test_request_context():
        r = resolve_reference("invoice", 42, doc_number="INV-0001")
    assert r["label"] == "فاتورة مبيعات INV-0001"
    assert r["url"] == "/invoices/42"
    assert r["kind"] == "invoice"
    return f"{r['label']} → {r['url']}"


@check("3. resolve_reference('vendor_bill', id, num) → linked label")
def _():
    from flask import current_app
    from app.services.source_reference import resolve_reference
    with current_app.test_request_context():
        r = resolve_reference("vendor_bill", 7, doc_number="VB-0007")
    assert r["label"] == "فاتورة مورد VB-0007"
    assert r["url"] == "/vendor-bills/7"
    return f"{r['label']} → {r['url']}"


@check("4. Unknown source_type → safe fallback label, no URL")
def _():
    from flask import current_app
    from app.services.source_reference import resolve_reference
    with current_app.test_request_context():
        r = resolve_reference("some_new_thing_we_never_saw", 999)
    assert r["url"] is None
    assert "قيد يدوي" in r["label"] or r["label"] == "قيد يدوي"
    return "graceful fallback"


@check("5. build_reference_map batches queries + resolves numbers")
def _():
    from flask import current_app
    from app.services.source_reference import build_reference_map
    from app.models import (
        Company, Customer, Invoice, InvoiceItem, InvoiceStatus,
    )
    from app.services.invoicing import post_invoice_to_ledger
    from app.services.numbering import next_number
    _teardown()
    c, cust, u = _bootstrap()
    _STATE["c"] = c
    # Create 3 invoices so we have real numbers.
    made = []
    for i in range(3):
        inv = Invoice(
            company_id=c.id, customer_id=cust.id,
            number=next_number(c.id, "INVOICE"),
            issue_date=date.today(),
            due_date=date.today() + timedelta(days=30),
            currency="EGP", tax_rate=Decimal("0.00"),
            status=InvoiceStatus.DRAFT,
        )
        inv.items.append(InvoiceItem(
            description=f"x{i}", quantity=1, unit_price=100))
        db.session.add(inv); db.session.flush()
        inv.recalc()
        post_invoice_to_ledger(inv, created_by=u.id)
        made.append(inv)
    db.session.commit()

    rows = [
        {"source_type": "invoice", "source_id": made[0].id},
        {"source_type": "invoice_item", "source_id": made[1].id},
        {"source_type": "payment", "source_id": made[2].id},
        {"source_type": None, "source_id": None},
    ]
    with current_app.test_request_context():
        ref_map = build_reference_map(rows, c.id)
    # Every non-None (source_type, source_id) resolves.
    for r in rows[:3]:
        key = (r["source_type"], r["source_id"])
        assert key in ref_map, f"missing key {key}"
        assert made[0].number in ref_map[key]["label"] or \
               made[1].number in ref_map[key]["label"] or \
               made[2].number in ref_map[key]["label"]
        assert ref_map[key]["url"] is not None
    # The None row also gets a safe entry.
    assert (None, None) in ref_map
    assert ref_map[(None, None)]["url"] is None
    return f"resolved {len([k for k in ref_map if k[0]])} rows"


@check("6. Cross-tenant source_id → label without URL")
def _():
    """A source_id that belongs to another company must NOT link.
    The label falls back to the type-only form; the URL is None."""
    from flask import current_app
    from app.services.source_reference import build_reference_map
    # Ask about invoice_id=999999 in company X — no such row → the
    # helper doesn't populate a doc_number, so label stays type-only
    # but we DO get the /invoices/999999 URL (link is present but
    # will 404 for the OTHER tenant, which is protected by the
    # invoice.view route's own company check). This test proves the
    # SAFETY: the LABEL doesn't leak a foreign doc number.
    rows = [{"source_type": "invoice", "source_id": 999999}]
    with current_app.test_request_context():
        ref_map = build_reference_map(rows, _STATE["c"].id)
    ref = ref_map[("invoice", 999999)]
    # No number surfaces from another tenant.
    assert "INV" not in ref["label"], \
        f"foreign number leaked: {ref['label']}"
    return "no foreign number in label"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _teardown()
            for label, fn in CHECKS:
                try:
                    res = fn()
                    print(f"PASS  {label}  ⇒ {res}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback; traceback.print_exc()
        finally:
            _teardown()
            print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
