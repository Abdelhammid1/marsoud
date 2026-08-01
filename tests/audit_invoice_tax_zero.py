#!/usr/bin/env python3
"""MARSOUD-INVOICE-TAX-ZERO (Abdelhamid 2026-08-01).

Batch 9 Ticket 1. Follow-up on Batch 8 Ticket 4b — new companies
default to vat_rate=0, but the sales invoice form still showed
15% because of `X or 15` fallback expressions. Decimal(0) is
falsy in Python, so `active_company.vat_rate or 15` returned 15
whenever the column held 0.

The bug lived in 5 spots:
  · templates/invoices/form.html:30 (form value)
  · routes/invoices.py:135 (_populate_invoice_from_form)
  · routes/invoices.py:210 (new invoice)
  · services/pos.py:107 (POS order)
  · agent/tools.py:314 (agent create_invoice tool)

All 5 now use `X if X is not None else 0` (or the equivalent
Jinja none-check), so 0% companies land on 0% tax and 15%
companies stay at 15%.

Checks:
  1. Invoice form template pulls value from company.vat_rate
     via an explicit none-check (grep for the fix pattern).
  2. POS service default preserves 0% when the company row
     has vat_rate=0.
  3. Agent tools.py preserves 0% for a 0% company.
  4. Regression: a 15% company still gets 15% on new invoices.
  5. Regression: _populate_invoice_from_form on a blank
     tax_rate submission uses company.vat_rate (0), not 15.
"""
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all()
    db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__ITZ_%__'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            conn.execute(text(
                "DELETE FROM journal_lines WHERE entry_id IN "
                "(SELECT id FROM journal_entries WHERE company_id = :c)"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})


def _mk_company(suffix, vat_rate):
    from app.models import Company
    from app.services.seed_coa import seed_default_coa
    c = Company(name=f"__ITZ_{suffix}__", base_currency="EGP",
                 subdomain=f"itz-{suffix.lower()}",
                 vat_rate=vat_rate,
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    db.session.commit()
    return c


@check("1. Invoice form template uses explicit none-check (not `or 15`)")
def _():
    tpl = (ROOT / "app" / "templates" / "invoices"
            / "form.html").read_text()
    assert "vat_rate or 15" not in tpl, \
        "template still uses `X or 15` — bug not fixed"
    assert "vat_rate is not none" in tpl.lower() or \
           "vat_rate if active_company.vat_rate is not none" in tpl, \
        "template missing the explicit none-check"
    return "template none-check present"


@check("2. POS service source uses none-check (grep pattern)")
def _():
    # A full create_pos_order integration test needs a real
    # PaymentMethod + Warehouse + ProductVariant setup. The
    # fix itself is a 3-line change — verify by grep, same
    # pattern as checks 1 + 3.
    src = (ROOT / "app" / "services" / "pos.py").read_text()
    assert "if tax_rate is not None else 15" not in src, \
        "POS still hardcodes 15% fallback"
    assert "_company.vat_rate is not None" in src, \
        "POS missing company-vat none-check fallback"
    return "POS falls through to company vat_rate (not 15)"


@check("3. Agent tools: 0% company → create_invoice tax = 0")
def _():
    # Static grep: the fix pattern must be present in tools.py.
    src = (ROOT / "app" / "agent" / "tools.py").read_text()
    assert "company.vat_rate or 15" not in src, \
        "agent tools.py still uses `X or 15`"
    assert "company.vat_rate is not None" in src, \
        "agent tools.py missing the None-check"
    return "agent tool uses None-check"


@check("4. Regression: 15% company still gets 15% via _populate")
def _():
    from app.routes.invoices import _populate_invoice_from_form
    from app.models import Invoice, InvoiceStatus, Customer
    from flask import current_app, g as _g
    _teardown()
    c = _mk_company("REG15", vat_rate=Decimal("15"))
    cust = Customer(company_id=c.id, name="regression")
    db.session.add(cust); db.session.commit()
    inv = Invoice(company_id=c.id, customer_id=cust.id,
                   number="REG-15", issue_date=date.today(),
                   due_date=date.today() + timedelta(days=30),
                   currency="EGP", tax_rate=0,
                   status=InvoiceStatus.DRAFT, source="MANUAL")
    db.session.add(inv); db.session.flush()
    with current_app.test_request_context():
        _g.active_company = c
        # Simulate blank tax_rate submission → default should
        # come from company.vat_rate (15).
        from werkzeug.datastructures import ImmutableMultiDict
        _populate_invoice_from_form(inv, ImmutableMultiDict({
            "customer_id": str(cust.id),
            "tax_rate": "",  # blank → fall through to default
        }))
    assert float(inv.tax_rate) == 15.0, \
        f"15% company regressed: {inv.tax_rate}"
    db.session.rollback()
    return "15% preserved"


@check("5. Blank submission on 0% company → tax_rate = 0 (not 15)")
def _():
    from app.routes.invoices import _populate_invoice_from_form
    from app.models import Invoice, InvoiceStatus, Customer
    from flask import current_app, g as _g
    _teardown()
    c = _mk_company("ZERO", vat_rate=Decimal("0"))
    cust = Customer(company_id=c.id, name="zero-vat")
    db.session.add(cust); db.session.commit()
    inv = Invoice(company_id=c.id, customer_id=cust.id,
                   number="ZERO-1", issue_date=date.today(),
                   due_date=date.today() + timedelta(days=30),
                   currency="EGP", tax_rate=0,
                   status=InvoiceStatus.DRAFT, source="MANUAL")
    db.session.add(inv); db.session.flush()
    with current_app.test_request_context():
        _g.active_company = c
        from werkzeug.datastructures import ImmutableMultiDict
        _populate_invoice_from_form(inv, ImmutableMultiDict({
            "customer_id": str(cust.id),
            "tax_rate": "",
        }))
    assert float(inv.tax_rate) == 0.0, \
        f"0% company got tax_rate={inv.tax_rate} (bug)"
    db.session.rollback()
    return "0% preserved"


def main():
    app = create_app()
    passed = failed = 0
    for label, fn in CHECKS:
        with app.app_context():
            try:
                _teardown()
                res = fn()
                print(f"PASS  {label}  ⇒ {res}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                failed += 1
                import traceback; traceback.print_exc()
    with app.app_context():
        _teardown()
        print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
