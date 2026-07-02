#!/usr/bin/env python3
"""MARSOUD-PARTY-OPENING-BALANCE-01 — the deployment-blocker check.

The ticket demands that after refactoring `Customer.balance` from
sum-of-invoices → sub-account.balance, every existing customer's
balance still reads the SAME number to the cent. Any drift is a bug
we have to find before merging.

This script walks every company in the current DB, computes both
representations for every non-legacy customer (one who has an
account_id — that's the path the refactor changed), and reports any
differences ≥ 1 cent. Exit code 0 = all clean, 1 = drift detected.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db
from app.models import Customer, Company, Vendor, VendorBill


def _legacy_customer_balance(c):
    return sum(
        inv.balance for inv in c.invoices
        if inv.status.value not in ("CANCELLED", "REFUNDED")
    )


def _legacy_vendor_balance(v):
    # Legacy vendor.balance did not exist; approximate from bills:
    # sum(bill.balance) for unpaid bills. Any legacy caller who
    # referenced vendor.balance was reading 0, so this is only useful
    # as an audit signal, not a strict match target.
    return sum(
        float(b.balance or 0) for b in VendorBill.query.filter_by(
            vendor_id=v.id,
        ).all()
        if b.status.value not in ("CANCELLED",)
    )


def _synthetic_scenario():
    """Build a self-contained scenario where sub-accounts DO exist for
    every customer (unlike the local demo DB which is mostly legacy).
    Returns (drift_count, checked_count)."""
    from datetime import date
    from decimal import Decimal
    from app.models import (
        Company, Customer, Invoice, InvoiceItem, InvoiceStatus,
        PaymentMethod, RefundType,
    )
    from app.services.seed_coa import seed_default_coa
    from app.services.subsidiary import (
        ensure_customer_account, record_customer_opening_balance,
    )
    from app.services.invoicing import (
        post_invoice_to_ledger, record_payment, issue_refund,
    )

    print()
    print("─── Synthetic scenario (fresh company with sub-accounts) ───")
    # Wipe any leftover from a prior run via raw SQL. Handles the ugly
    # long-tail: child tables whose FK is NOT company_id (invoice_items
    # keyed on invoice_id, journal_lines keyed on entry_id, etc.) get
    # orphaned by a naive company_id sweep; SQLite reuses IDs so those
    # orphans wire themselves back to the NEW fixture's fresh objects.
    existing = Company.query.filter_by(name="__BAL_REGRESSION__").first()
    if existing:
        old_cid = existing.id
        db.session.close()
        from sqlalchemy import text as _text, inspect as _inspect
        insp = _inspect(db.engine)
        with db.engine.begin() as conn:
            # Grab the doomed FK sets FIRST.
            inv_ids = [
                r[0] for r in conn.execute(_text(
                    "SELECT id FROM invoices WHERE company_id = :c"),
                    {"c": old_cid}).fetchall()
            ]
            bill_ids = [
                r[0] for r in conn.execute(_text(
                    "SELECT id FROM vendor_bills WHERE company_id = :c"),
                    {"c": old_cid}).fetchall()
            ]
            je_ids = [
                r[0] for r in conn.execute(_text(
                    "SELECT id FROM journal_entries WHERE company_id = :c"),
                    {"c": old_cid}).fetchall()
            ]
            # Wipe grand-children of doomed parents.
            if inv_ids:
                _in = ",".join(str(i) for i in inv_ids)
                conn.execute(_text(
                    f"DELETE FROM invoice_items WHERE invoice_id IN ({_in})"))
                conn.execute(_text(
                    f"DELETE FROM payments WHERE invoice_id IN ({_in})"))
            if bill_ids:
                _in = ",".join(str(i) for i in bill_ids)
                conn.execute(_text(
                    f"DELETE FROM vendor_bill_items WHERE bill_id IN ({_in})"))
                conn.execute(_text(
                    f"DELETE FROM vendor_bill_payments WHERE bill_id IN ({_in})"))
            if je_ids:
                _in = ",".join(str(i) for i in je_ids)
                conn.execute(_text(
                    f"DELETE FROM journal_lines WHERE entry_id IN ({_in})"))
            # Now the company_id-keyed sweep can safely finish the job.
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {c["name"] for c in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(
                        _text(f"DELETE FROM {tbl.name} WHERE company_id = :cid"),
                        {"cid": old_cid},
                    )
            conn.execute(
                _text("DELETE FROM companies WHERE id = :cid"),
                {"cid": old_cid},
            )

    c = Company(name="__BAL_REGRESSION__", base_currency="SAR")
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    db.session.commit()
    cid = c.id

    pm = PaymentMethod.query.filter_by(company_id=cid).first()

    scenarios = [
        # (name, opening_amount, invoice_amount, paid_amount, refund_amount)
        ("عميل بدون رصيد افتتاحي، فاتورة كاملة", 0, 1000, 1000, 0),
        ("عميل بدون رصيد افتتاحي، فاتورة جزئية", 0, 500, 200, 0),
        ("عميل برصيد افتتاحي + فاتورة جديدة", 3000, 2000, 0, 0),
        ("عميل برصيد افتتاحي + فاتورة مدفوعة", 500, 300, 300, 0),
        ("عميل عليه مرتجع كامل", 0, 800, 800, 800),
        ("عميل برصيد افتتاحي سالب (مقدم)", -400, 1000, 500, 0),
    ]

    drift = 0
    checked = 0
    for name, opening, inv_amt, paid, refund in scenarios:
        cust = Customer(company_id=cid, name=name)
        db.session.add(cust); db.session.flush()
        ensure_customer_account(cust)
        if opening != 0:
            record_customer_opening_balance(cust, opening)
        if inv_amt > 0:
            inv = Invoice(
                company_id=cid, customer_id=cust.id,
                number=f"BAL-{cust.id}", issue_date=date.today(),
                due_date=date.today(),
                currency="SAR", status=InvoiceStatus.DRAFT,
                tax_rate=Decimal("0"),
            )
            db.session.add(inv); db.session.flush()
            db.session.add(InvoiceItem(
                invoice_id=inv.id, description="سلعة",
                quantity=Decimal("1"),
                unit_price=Decimal(str(inv_amt)),
                line_total=Decimal(str(inv_amt)),
            ))
            db.session.flush()
            inv.recalc(); db.session.flush()
            post_invoice_to_ledger(inv)
            if paid > 0:
                record_payment(inv, paid,
                                 payment_method_id=pm.id if pm else None)
            if refund > 0:
                issue_refund(inv, RefundType.FULL)
        db.session.commit()

        # Legacy = sum(invoice.balance for non-cancelled/refunded)
        legacy = _legacy_customer_balance(cust)
        # NEW = customer.balance (which now reads from sub-account)
        # Since account_id is populated, this hits the new path.
        subsidiary = cust.balance
        # Expected relationship: subsidiary = legacy + opening_balance
        # (opening is INSIDE the sub-account but OUTSIDE the legacy calc).
        expected = legacy + opening
        delta = abs(subsidiary - expected)
        status = "✅" if delta < 0.01 else "❌"
        print(f"  {status} {name[:40]:<40} legacy={legacy:>8.2f} "
                f"+opening={opening:>+8.2f} = expected={expected:>8.2f} "
                f"vs subsidiary={subsidiary:>8.2f}")
        if delta >= 0.01:
            drift += 1
        checked += 1

    # We intentionally leave the fixture company in the DB. Cleanup is
    # done at the START of the next run by the "existing" lookup above,
    # which sidesteps a nasty cascade issue with SQLAlchemy trying to
    # null-out FKs on NOT NULL columns during teardown.
    print(f"  (fixture company #{cid} left in place — next run replaces it)")
    return drift, checked


def main():
    app = create_app()
    diff_count = 0
    warn_count = 0
    total_checked = 0
    with app.app_context():
        print(f"{'company':<30} {'customer':<25} {'legacy':>12}  {'subsidiary':>12}  {'diff':>10}")
        print("─" * 100)
        for co in Company.query.filter_by(is_active=True).all():
            for c in Customer.query.filter_by(company_id=co.id).all():
                total_checked += 1
                legacy = _legacy_customer_balance(c)
                if c.account_id and c.account is not None:
                    subsidiary = float(c.account.balance or 0)
                    delta = abs(legacy - subsidiary)
                    if delta > 0.01:
                        # Check if the delta is explained by an opening
                        # balance recorded via the new system.
                        from app.models import PartyOpeningBalance, PartyType
                        ob = PartyOpeningBalance.query.filter_by(
                            company_id=co.id,
                            party_type=PartyType.CUSTOMER,
                            party_id=c.id,
                        ).first()
                        ob_amt = float(ob.amount) if ob else 0.0
                        if abs(delta - abs(ob_amt)) < 0.01:
                            # The whole delta IS the opening balance —
                            # expected behaviour, not drift.
                            print(f"{co.name[:30]:<30} {c.name[:25]:<25} "
                                    f"{legacy:>12.2f}  {subsidiary:>12.2f}  "
                                    f"{delta:>10.2f}  ← +opening {ob_amt:.2f}")
                            warn_count += 1
                        else:
                            # Unexplained drift.
                            print(f"{co.name[:30]:<30} {c.name[:25]:<25} "
                                    f"{legacy:>12.2f}  {subsidiary:>12.2f}  "
                                    f"{delta:>10.2f}  ⚠ DRIFT")
                            diff_count += 1
                else:
                    # Legacy customer (no sub-account) — the property
                    # falls back to legacy calc, so by construction it
                    # matches. Skip to keep output focused.
                    pass

        print()
        print(f"checked {total_checked} customers")
        if diff_count == 0 and warn_count == 0:
            print("✅ CLEAN — every subsidiary balance matches its legacy value.")
        elif diff_count == 0:
            print(f"✅ CLEAN — {warn_count} row(s) differed but the delta "
                    f"equals a recorded opening balance (expected).")
        else:
            print(f"❌ {diff_count} unexplained drift(s) detected — "
                    f"do NOT merge until fixed.")

        # Synthetic path — exercises the new-path formula end-to-end.
        syn_drift, syn_checked = _synthetic_scenario()
        print()
        if syn_drift == 0:
            print(f"✅ SYNTHETIC — all {syn_checked} scenarios match "
                    f"(legacy + opening = subsidiary).")
        else:
            print(f"❌ SYNTHETIC — {syn_drift} scenarios drifted.")

    sys.exit(0 if (diff_count == 0 and syn_drift == 0) else 1)


if __name__ == "__main__":
    main()
