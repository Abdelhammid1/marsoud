#!/usr/bin/env python3
"""MARSOUD-COMM-CASH-BASIS-01 (Abdelhamid 2026-09-20) — sales
commissions moved from accrual (at invoice posting) to cash-basis
(at each real customer payment), with three fixes to the math:

  a) taxable base uses `taxable_base / total`, not `subtotal / total`
     — the old ratio inflated the base whenever the invoice carried
     an invoice-level discount.
  b) commission is dated to the payment, not the invoice, so
     multi-month collections land in the payroll month the cash
     actually moved.
  c) foreign-invoice payments convert the commission to the
     company's base currency at the payment's exchange rate, and
     the row snapshots `fx_rate` for the audit trail.

Also: the manual settle path fires the "commission paid" email to
the rep.

Seven checks:
  1. Base currency, no discount, no tax → base = payment,
     commission = payment × rate.
  2. Base currency, WITH invoice-level discount → base uses
     `taxable_base`, not `subtotal`.  Pre-fix bug reproduced then
     asserted absent.
  3. Two partial payments on one invoice in different months →
     two commission rows, dated to their own payment dates, sum to
     the total commission that a full-invoice accrual would have
     produced.
  4. `record_commission_accrual_for_invoice` no longer exists.
  5. Posting an invoice no longer creates a commission row on its
     own (must wait for a payment).
  6. Foreign invoice (SAR on an EGP tenant) with exchange_rate on
     the payment → commission amount is EGP (base × fx), fx_rate
     stored on the row.
  7. Idempotency — recording a commission twice for the same
     (invoice, payment) returns the existing row and posts no
     second JE.
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
        # Same orphan-cleanup pattern as tests/audit_void_invoice_paid_slice.py.
        conn.execute(text(
            "DELETE FROM journal_lines WHERE entry_id IN "
            "(SELECT id FROM journal_entries WHERE company_id = :c)"
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
            "DELETE FROM users WHERE email LIKE 'ccb-%@x.test'"))


def _setup():
    from app.models import (
        Company, User, user_companies, Customer, DiscountType,
    )
    from werkzeug.security import generate_password_hash

    for name in ("__COMM_CB__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    # EGP tenant so we can test foreign-invoice (SAR) FX conversion.
    a = Company(name="__COMM_CB__", base_currency="EGP", vat_rate=0)
    db.session.add(a); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(a.id)

    def _mk(email, name):
        u = User(email=email,
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name=name)
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=a.id, role="owner"))
        return u

    owner = _mk("ccb-owner@x.test", "ccb-owner")
    # The sales rep is a User the customer is assigned to.
    rep = _mk("ccb-rep@x.test", "ccb-rep")

    customer = Customer(
        company_id=a.id, name="CCB-Customer",
        email="ccbc@x.test", phone="0500000000",
        sales_rep_id=rep.id, commission_rate=10,   # 10%
    )
    db.session.add(customer); db.session.commit()

    _STATE.update(a_id=a.id, owner_id=owner.id, rep_id=rep.id,
                    customer_id=customer.id)


def _fresh_invoice(*, unit_price, currency="EGP", tax_rate=0,
                    invoice_discount_amount=0):
    """Post + send a SENT invoice with one line at `unit_price`.
    `invoice_discount_amount` optionally reduces the taxable base
    via the AMOUNT discount type so we can exercise the discount-
    inflation bug."""
    from app.models import (
        Invoice, InvoiceItem, InvoiceStatus, DiscountType,
    )
    from app.services.invoicing import post_invoice_to_ledger
    from app.services.numbering import next_number
    number = next_number(_STATE["a_id"], "INVOICE")
    inv = Invoice(
        company_id=_STATE["a_id"],
        number=number,
        customer_id=_STATE["customer_id"],
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency=currency, tax_rate=tax_rate,
        status=InvoiceStatus.DRAFT,
        created_by_id=_STATE["owner_id"],
    )
    if invoice_discount_amount > 0:
        inv.invoice_discount_type = DiscountType.FIXED
        inv.invoice_discount_value = invoice_discount_amount
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, company_id=inv.company_id,
        description="widget",
        quantity=1, unit_price=unit_price, line_total=unit_price,
    ))
    inv.recalc()
    db.session.commit()
    post_invoice_to_ledger(inv, created_by=_STATE["owner_id"])
    inv.status = InvoiceStatus.SENT
    db.session.commit()
    return inv


def _pay(invoice, amount, *, exchange_rate=None, when=None):
    from app.services.invoicing import record_payment
    return record_payment(
        invoice, float(amount), method="cash",
        payment_date=when or date.today(),
        exchange_rate=exchange_rate,
        created_by=_STATE["owner_id"],
        notify=False,
    )


def _commissions_for(invoice):
    from app.models import SalesCommission
    return SalesCommission.query.filter_by(
        invoice_id=invoice.id, is_carry_forward=False,
    ).order_by(SalesCommission.id.asc()).all()


# ─── Checks ────────────────────────────────────────────────────────
@check("1. Base ccy, no tax, no discount: commission = payment × rate")
def _():
    inv = _fresh_invoice(unit_price=1000)   # total=1000, taxable=1000
    _pay(inv, 200)
    rows = _commissions_for(inv)
    assert len(rows) == 1, f"expected 1 commission row, got {len(rows)}"
    r = rows[0]
    # 200 * (1000/1000) * 10% = 20
    assert abs(float(r.amount) - 20.0) < 0.01, \
        f"expected commission 20.00, got {r.amount}"
    assert r.payment_id is not None, "row must be tied to a payment"
    assert r.status == "UNPAID"
    return f"commission {r.amount} on payment 200 (rate 10%)"


@check("2. Base ccy, WITH invoice discount: base uses taxable_base")
def _():
    # Pre-discount subtotal=1000; discount=200; taxable_base=800; total=800.
    # Under the OLD formula: base = payment × (subtotal/total) = payment
    # × (1000/800) = 1.25 × payment (inflated by the discount factor).
    # Under the FIX: base = payment × (taxable_base/total) = payment × 1.0.
    inv = _fresh_invoice(unit_price=1000, invoice_discount_amount=200)
    assert abs(float(inv.subtotal) - 1000) < 0.01
    assert abs(float(inv.taxable_base) - 800) < 0.01
    assert abs(float(inv.total) - 800) < 0.01
    _pay(inv, 400)
    rows = _commissions_for(inv)
    assert len(rows) == 1
    r = rows[0]
    # Correct: 400 × (800/800) × 10% = 40.  Pre-fix would have been
    # 400 × (1000/800) × 10% = 50.  Assert we're at 40, not 50.
    assert abs(float(r.amount) - 40.0) < 0.01, \
        f"expected 40 (post-discount base), got {r.amount} — bug regressed"
    return f"commission {r.amount} on payment 400 (discount not inflating)"


@check("3. Two partial payments in different months → 2 rows, "
        "each dated to its payment")
def _():
    inv = _fresh_invoice(unit_price=1000)
    jan = date(2026, 1, 15)
    mar = date(2026, 3, 20)
    _pay(inv, 400, when=jan)
    _pay(inv, 600, when=mar)
    rows = _commissions_for(inv)
    assert len(rows) == 2, f"expected 2 commission rows, got {len(rows)}"
    r1, r2 = rows
    assert (r1.period_year, r1.period_month) == (2026, 1), \
        f"row1 period {r1.period_year}/{r1.period_month}"
    assert (r2.period_year, r2.period_month) == (2026, 3), \
        f"row2 period {r2.period_year}/{r2.period_month}"
    # Sum = 100 (full commission on the full invoice)
    assert abs(sum(float(r.amount) for r in rows) - 100.0) < 0.01
    return f"row1 40 (Jan), row2 60 (Mar), sum 100"


@check("4. record_commission_accrual_for_invoice is gone (import fails)")
def _():
    import app.services.sales_commissions as sc
    assert not hasattr(sc, "record_commission_accrual_for_invoice"), (
        "accrual function must be deleted, not kept as a shim")
    return "symbol removed"


@check("5. Posting an invoice creates NO commission row on its own")
def _():
    from app.models import SalesCommission
    inv = _fresh_invoice(unit_price=500)
    # Before ANY payment: zero commission rows for this invoice.
    rows = SalesCommission.query.filter_by(invoice_id=inv.id).all()
    assert rows == [], (
        f"posting an invoice should not accrue; got {len(rows)} rows")
    return "no accrual on issue — cash-basis honored"


@check("6. Foreign invoice (SAR on EGP tenant): commission in EGP + "
        "fx_rate stored")
def _():
    # SAR invoice, taxable=1000 SAR.  Payment 500 SAR at rate 8.
    # EGP-side taxable slice: 500 * (1000/1000) * 8 = 4000 EGP.
    # Commission: 4000 * 10% = 400 EGP.
    inv = _fresh_invoice(unit_price=1000, currency="SAR")
    _pay(inv, 500, exchange_rate=8.0)
    rows = _commissions_for(inv)
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    r = rows[0]
    assert abs(float(r.amount) - 400.0) < 0.01, \
        f"expected 400 EGP, got {r.amount}"
    assert r.fx_rate is not None, "fx_rate should be stamped"
    assert abs(float(r.fx_rate) - 8.0) < 0.001, \
        f"fx_rate expected 8.0, got {r.fx_rate}"
    return f"commission 400 EGP (from 500 SAR at 8.0)"


@check("7. Idempotent: recording twice for same (invoice, payment) is a no-op")
def _():
    from app.services.sales_commissions import record_commission_for_payment
    from app.models import Payment, SalesCommission, JournalEntry
    inv = _fresh_invoice(unit_price=1000)
    _pay(inv, 300)
    rows_before = _commissions_for(inv)
    assert len(rows_before) == 1
    payment = Payment.query.filter_by(invoice_id=inv.id).first()
    je_count_before = JournalEntry.query.filter_by(
        company_id=_STATE["a_id"],
        source_type="sales_commission",
        source_id=inv.id,
    ).count()

    # Second call for the same payment → returns existing row, no JE.
    r = record_commission_for_payment(
        inv, payment, 300.0, created_by=_STATE["owner_id"],
    )
    rows_after = _commissions_for(inv)
    assert len(rows_after) == 1, "duplicate row created"
    assert r is not None and r.id == rows_before[0].id, \
        "second call should return the existing row"
    je_count_after = JournalEntry.query.filter_by(
        company_id=_STATE["a_id"],
        source_type="sales_commission",
        source_id=inv.id,
    ).count()
    assert je_count_after == je_count_before, \
        f"second call posted an extra JE ({je_count_before} → {je_count_after})"
    return "second call is a no-op"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}  => {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  => {type(e).__name__}: {e}")
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
    print(f"----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
