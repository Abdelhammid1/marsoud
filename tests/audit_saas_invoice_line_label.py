#!/usr/bin/env python3
"""MARSOUD-TKT-SAAS-INVOICE-LINE-LABEL (Abdelhamid 2026-08-31) —
SaaS invoice line description reads "اشتراك مرصود — باقة …"
across the app. The old "اشتراك منصتي — باقة …" wording is
retired both in the source (services/saas_billing.py) and on
existing rows (data migration 5e44c9a13a1c).

Checks:
  1. Source: services/saas_billing.py has no more literal
     "اشتراك منصتي — باقة" strings.
  2. Source: services/saas_billing.py DOES have the new
     "اشتراك مرصود — باقة" label in the two spots that build
     invoice descriptions.
  3. Live: fake up an InvoiceItem row with the old label, run the
     migration's UPDATE statement, verify it rewrote to the new
     label — proves the retroactive fix works.
  4. Legitimate uses of "منصتي" outside invoice-line context stay
     intact (Manasety = the parent company; landing footer, role
     labels, and support wording refer to the company on purpose).
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


@check("1. services/saas_billing.py — no more 'اشتراك منصتي — باقة'")
def _():
    src = _read("app/services/saas_billing.py")
    assert "اشتراك منصتي — باقة" not in src, \
        "old label 'اشتراك منصتي — باقة' still present in " \
        "services/saas_billing.py — the ticket asked for it gone"
    return "old label removed from source"


@check("2. services/saas_billing.py — has the new 'اشتراك مرصود — باقة'")
def _():
    src = _read("app/services/saas_billing.py")
    hits = src.count("اشتراك مرصود — باقة")
    assert hits >= 2, \
        f"expected the new label in both invoice-line builders " \
        f"(2 hits); got {hits}"
    return f"new label appears {hits}× in source"


@check("3. retroactive migration rewrites existing rows")
def _():
    """Insert a fake InvoiceItem row with the old label + apply the
    UPDATE that the migration runs. Prove it rewrites."""
    from datetime import datetime, date, timedelta
    from decimal import Decimal
    from sqlalchemy import text, inspect
    from app import create_app, db

    app = create_app()
    with app.app_context():
        from app.models import (
            Company, Customer, Invoice, InvoiceItem, InvoiceStatus,
        )
        from app.services.seed_coa import seed_default_coa
        from app.services.subsidiary import ensure_customer_account

        insp = inspect(db.engine)
        cids = [r[0] for r in db.session.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__SIL_MIG__%'"))]
        for cid in cids:
            for t in reversed(db.metadata.sorted_tables):
                cols = {c["name"] for c in insp.get_columns(t.name)}
                if "company_id" in cols:
                    db.session.execute(text(
                        f"DELETE FROM {t.name} WHERE company_id = :c"),
                        {"c": cid})
            db.session.execute(text(
                "DELETE FROM companies WHERE id = :c"), {"c": cid})
        db.session.commit()

        c = Company(name="__SIL_MIG__co", base_currency="EGP",
                    subdomain="silmig",
                    subscription_started_at=datetime.utcnow(),
                    subscription_expires_at=datetime(2999, 1, 1))
        db.session.add(c); db.session.commit()
        seed_default_coa(c.id); db.session.commit()
        cust = Customer(company_id=c.id, name="عميل الاختبار")
        db.session.add(cust); db.session.flush()
        ensure_customer_account(cust); db.session.commit()

        inv = Invoice(company_id=c.id, number="INV-SIL-01",
                      customer_id=cust.id,
                      issue_date=date.today(),
                      due_date=date.today() + timedelta(days=30),
                      currency="EGP", tax_rate=Decimal("0"),
                      status=InvoiceStatus.DRAFT)
        db.session.add(inv); db.session.flush()
        old_label = "اشتراك منصتي — باقة Pro (شهري)"
        db.session.add(InvoiceItem(
            invoice_id=inv.id, company_id=c.id,
            description=old_label,
            quantity=Decimal("1"), unit_price=Decimal("500"),
            line_total=Decimal("500"),
        ))
        db.session.commit()

        try:
            # Run the exact UPDATE the migration issues
            db.session.execute(text(
                "UPDATE invoice_items "
                "SET description = REPLACE(description, "
                "  'اشتراك منصتي — باقة', 'اشتراك مرصود — باقة') "
                "WHERE description LIKE '%اشتراك منصتي — باقة%'"
            ))
            db.session.commit()

            row = db.session.execute(text(
                "SELECT description FROM invoice_items "
                "WHERE invoice_id = :i"), {"i": inv.id}).fetchone()
            assert row and row[0] == "اشتراك مرصود — باقة Pro (شهري)", \
                f"UPDATE didn't rewrite; got {row[0] if row else 'nothing'}"
            return "retroactive rewrite works"
        finally:
            for t in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(t.name)}
                if "company_id" in cols:
                    db.session.execute(text(
                        f"DELETE FROM {t.name} WHERE company_id = :c"),
                        {"c": c.id})
            db.session.execute(text(
                "DELETE FROM companies WHERE id = :c"), {"c": c.id})
            db.session.commit()


@check("4. legitimate 'منصتي' outside invoice-line context still there")
def _():
    """These are correct uses — Manasety is the parent company that
    owns Marsoud (the product). Landing pages, role labels, and
    support wording refer to the COMPANY, not to a subscription
    line. If a future edit accidentally sweeps them, this check
    flags it."""
    landing = _read("app/templates/landing.html")
    assert "منصتي للبرمجيات وحلول الذكاء الاصطناعي" in landing, \
        "landing page footer must keep 'منصتي' as the parent " \
        "company name — it's not an invoice line"
    perms = _read("app/services/permissions.py")
    assert "موظف دعم فني (منصتي)" in perms, \
        "support-agent role label must keep '(منصتي)' — that role " \
        "belongs to the parent company"
    return "legitimate parent-company uses preserved"


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
