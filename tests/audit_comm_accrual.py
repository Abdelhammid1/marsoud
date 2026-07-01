#!/usr/bin/env python3
"""MARSOUD-COMM-ACCRUAL audit — commission accrues at invoice time.

Covers every acceptance criterion from the ticket:

  1.  Posting an invoice for a customer with sales_rep + commission_rate
      immediately creates a SalesCommission row keyed to the INVOICE
      month + a journal entry dated invoice.issue_date.
  2.  Journal shape: Dr 5280 amount / Cr 2150 amount.
  3.  Base = invoice.subtotal × rate / 100 (NOT payment-based).
  4.  Recording a payment does NOT create a second commission row.
  5.  Recording a payment does NOT re-date the commission (past is
      frozen).
  6.  If a legacy invoice somehow lacks an accrual, record_payment
      backfills it dated to the INVOICE date, not the payment date.
  7.  Second call to record_commission_accrual_for_invoice is a no-op
      (idempotency).
  8.  Customer without sales_rep/commission_rate → no commission at all.
  9.  Backfill script dry-run: reports plan without writing.
 10.  Backfill --apply: creates missing rows dated to invoice date.
 11.  Backfill re-dates a mis-dated row + its journal entry to the
      invoice month.
 12.  Backfill is idempotent: second apply run is a no-op.
 13.  Monthly profit reflects revenue AND commission in the same month.
"""
import sys, time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__COMM_ACCRUAL_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import Company, User, Customer
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    existing = Company.query.filter_by(name=COMPANY_NAME).first()
    if existing:
        _teardown(existing.id)
    c = Company(name=COMPANY_NAME, base_currency="SAR")
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    owner = User.query.filter_by(email="demo@manasety.ai").first()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner",
    ))
    # Customer with sales rep + 10% commission
    cust = Customer(
        company_id=c.id, name="عميل بعمولة",
        phone="0500001", email="c@x.y", is_active=True,
        sales_rep_id=owner.id, commission_rate=Decimal("10"),
    )
    db.session.add(cust); db.session.flush()
    # Customer without commission
    cust2 = Customer(
        company_id=c.id, name="عميل بدون عمولة",
        phone="0500002", email="c2@x.y", is_active=True,
    )
    db.session.add(cust2); db.session.commit()
    _STATE.update(
        company_id=c.id, owner_id=owner.id,
        cust_with_id=cust.id, cust_without_id=cust2.id,
    )


def _teardown(company_id):
    from app.models import (
        Company, JournalEntry, JournalLine, Invoice, InvoiceItem, Payment,
        SalesCommission,
    )
    from app.models.user import user_companies
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    entry_ids = [r.id for r in JournalEntry.query.filter_by(
        company_id=company_id).all()]
    if entry_ids:
        JournalLine.query.filter(JournalLine.entry_id.in_(entry_ids)
                                  ).delete(synchronize_session=False)
    inv_ids = [r.id for r in Invoice.query.filter_by(
        company_id=company_id).all()]
    if inv_ids:
        InvoiceItem.query.filter(InvoiceItem.invoice_id.in_(inv_ids)
                                  ).delete(synchronize_session=False)
        Payment.query.filter(Payment.invoice_id.in_(inv_ids)
                              ).delete(synchronize_session=False)
    SalesCommission.query.filter_by(company_id=company_id).delete()
    db.session.execute(user_companies.delete().where(
        user_companies.c.company_id == company_id))
    for t in reversed(db.metadata.sorted_tables):
        if "company_id" in {c["name"] for c in insp.get_columns(t.name)}:
            db.session.execute(t.delete().where(t.c.company_id == company_id))
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()


_INV_COUNTER = {"n": 0}


def _make_invoice(customer_id, *, issue_date, subtotal=1000, tax_rate=15):
    """Insert a fresh invoice + one item, no commit yet.
    Monotonic counter so we never collide on invoice numbers."""
    from app.models import Invoice, InvoiceItem, InvoiceStatus
    _INV_COUNTER["n"] += 1
    net = Decimal(str(subtotal))
    tax = net * Decimal(str(tax_rate)) / 100
    total = net + tax
    inv = Invoice(
        company_id=_STATE["company_id"],
        customer_id=customer_id,
        number=f"AUD-{_INV_COUNTER['n']:06d}",
        issue_date=issue_date,
        due_date=issue_date + timedelta(days=30),
        currency="SAR",
        status=InvoiceStatus.DRAFT,
        subtotal=net, tax_amount=tax, total=total, taxable_base=net,
        tax_rate=Decimal(str(tax_rate)),
    )
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="خدمة",
        quantity=1, unit_price=net, line_total=net,
    ))
    db.session.flush()
    return inv


# ─── Checks ────────────────────────────────────────────────────────────
@check("1. Posting invoice with commission creates accrual keyed to invoice month")
def _():
    from app.models import SalesCommission
    from app.services.invoicing import post_invoice_to_ledger
    d = date(2026, 6, 30)   # June invoice
    inv = _make_invoice(_STATE["cust_with_id"], issue_date=d)
    post_invoice_to_ledger(inv)
    db.session.commit()
    row = SalesCommission.query.filter_by(
        invoice_id=inv.id, is_carry_forward=False,
    ).first()
    assert row is not None, "no commission row created"
    assert row.period_year == 2026 and row.period_month == 6
    assert float(row.amount) == 100.0, \
        f"expected 100 (10% of 1000), got {row.amount}"
    assert row.status == "UNPAID"
    _STATE["inv1_id"] = inv.id
    _STATE["comm1_id"] = row.id
    return f"row #{row.id} period 6/2026 amount 100 status UNPAID"


@check("2. Commission journal entry dated invoice.issue_date, not today")
def _():
    from app.models import SalesCommission, JournalEntry
    row = db.session.get(SalesCommission, _STATE["comm1_id"])
    entry = db.session.get(JournalEntry, row.journal_entry_id)
    assert entry.date == date(2026, 6, 30), \
        f"journal dated {entry.date}, expected 2026-06-30"
    return f"journal dated {entry.date}"


@check("3. Journal shape: Dr 5280 100 / Cr 2150 100")
def _():
    from app.models import SalesCommission, JournalEntry, JournalLine, Account
    row = db.session.get(SalesCommission, _STATE["comm1_id"])
    lines = JournalLine.query.filter_by(entry_id=row.journal_entry_id).all()
    assert len(lines) == 2
    by_code = {db.session.get(Account, l.account_id).code: l for l in lines}
    assert "5280" in by_code and "2150" in by_code
    assert float(by_code["5280"].debit) == 100.0
    assert float(by_code["2150"].credit) == 100.0
    return "Dr 5280 100 / Cr 2150 100"


@check("4. Recording a payment does NOT create a second commission row")
def _():
    from app.models import Invoice, SalesCommission
    from app.services.invoicing import record_payment
    inv = db.session.get(Invoice, _STATE["inv1_id"])
    n_before = SalesCommission.query.filter_by(invoice_id=inv.id).count()
    record_payment(inv, float(inv.total), method="cash",
                    payment_date=date(2026, 7, 1), notify=False)
    db.session.commit()
    n_after = SalesCommission.query.filter_by(invoice_id=inv.id).count()
    assert n_after == n_before, \
        f"payment created extra commission rows: {n_before} → {n_after}"
    return f"count unchanged ({n_before} → {n_after})"


@check("5. Payment does NOT re-date the commission (period stays June)")
def _():
    from app.models import SalesCommission, JournalEntry
    row = db.session.get(SalesCommission, _STATE["comm1_id"])
    assert row.period_year == 2026 and row.period_month == 6, \
        f"period drifted to {row.period_year}-{row.period_month}"
    entry = db.session.get(JournalEntry, row.journal_entry_id)
    assert entry.date == date(2026, 6, 30), \
        f"journal date drifted to {entry.date}"
    return "period + journal date frozen at invoice date"


@check("6. Legacy invoice with no accrual: record_payment back-fills at invoice date")
def _():
    """Simulate a legacy invoice that was posted BEFORE this ticket by
    skipping the accrual step, then record a payment and verify the
    accrual gets back-filled at the INVOICE date (not payment date)."""
    from app.models import (
        Invoice, InvoiceItem, InvoiceStatus, SalesCommission, JournalEntry,
    )
    from app.services.invoicing import post_invoice_to_ledger, record_payment
    # Build a legacy-like invoice manually, without going through the
    # accrual hook: temporarily monkey-patch the accrual function out.
    from app.services import sales_commissions as sc_mod
    orig = sc_mod.record_commission_accrual_for_invoice
    sc_mod.record_commission_accrual_for_invoice = lambda inv, **k: None
    d = date(2026, 5, 15)
    try:
        inv = _make_invoice(_STATE["cust_with_id"], issue_date=d)
        post_invoice_to_ledger(inv)
        db.session.commit()
    finally:
        sc_mod.record_commission_accrual_for_invoice = orig
    # No commission row yet
    assert SalesCommission.query.filter_by(invoice_id=inv.id).count() == 0

    # Now pay it in JULY — record_payment should backfill at invoice date (MAY)
    record_payment(inv, float(inv.total), method="cash",
                    payment_date=date(2026, 7, 5), notify=False)
    db.session.commit()

    row = SalesCommission.query.filter_by(invoice_id=inv.id).first()
    assert row is not None, "backfill didn't happen on payment"
    assert row.period_year == 2026 and row.period_month == 5, \
        f"backfilled to {row.period_year}-{row.period_month}, expected 5/2026"
    entry = db.session.get(JournalEntry, row.journal_entry_id)
    assert entry.date == date(2026, 5, 15), \
        f"journal dated {entry.date}, expected 2026-05-15"
    return "backfilled accrual dated to invoice month (5/2026), NOT payment"


@check("7. Second accrual call for the same invoice is a no-op (idempotent)")
def _():
    from app.models import SalesCommission, Invoice
    from app.services.sales_commissions import record_commission_accrual_for_invoice
    inv = db.session.get(Invoice, _STATE["inv1_id"])
    n_before = SalesCommission.query.filter_by(invoice_id=inv.id).count()
    row = record_commission_accrual_for_invoice(inv)
    db.session.commit()
    n_after = SalesCommission.query.filter_by(invoice_id=inv.id).count()
    assert n_after == n_before
    assert row is not None and row.id == _STATE["comm1_id"]
    return f"count unchanged ({n_before}) + returned existing row"


@check("8. Customer without sales_rep/commission_rate → no commission")
def _():
    from app.models import SalesCommission
    from app.services.invoicing import post_invoice_to_ledger
    inv = _make_invoice(_STATE["cust_without_id"], issue_date=date(2026, 6, 30))
    post_invoice_to_ledger(inv)
    db.session.commit()
    n = SalesCommission.query.filter_by(invoice_id=inv.id).count()
    assert n == 0
    return "no commission row for customer with no rep"


# ─── Backfill script ───────────────────────────────────────────────────
@check("9. Backfill dry-run reports plan without writing")
def _():
    """Simulate a legacy state: strip commissions off inv1 + inv2, then
    run backfill dry-run and confirm it plans to create + doesn't write."""
    from app.models import (
        SalesCommission, Invoice, InvoiceItem, InvoiceStatus, Payment,
        JournalEntry, JournalLine,
    )
    from app.services.invoicing import post_invoice_to_ledger
    from app.services import sales_commissions as sc_mod
    # Fresh legacy-state fixture: 2 unpaid invoices from different months
    orig = sc_mod.record_commission_accrual_for_invoice
    sc_mod.record_commission_accrual_for_invoice = lambda inv, **k: None
    try:
        inv_a = _make_invoice(_STATE["cust_with_id"], issue_date=date(2026, 4, 15))
        inv_b = _make_invoice(_STATE["cust_with_id"], issue_date=date(2026, 5, 20))
        post_invoice_to_ledger(inv_a)
        post_invoice_to_ledger(inv_b)
        db.session.commit()
    finally:
        sc_mod.record_commission_accrual_for_invoice = orig
    _STATE["legacy_inv_a_id"] = inv_a.id
    _STATE["legacy_inv_b_id"] = inv_b.id

    from scripts.backfill_commission_accrual import run as backfill_run
    result = backfill_run(_STATE["company_id"], dry_run=True)
    # We expect at least 2 new creates
    assert result["planned_creates"] >= 2, \
        f"planned_creates={result['planned_creates']}"
    # And nothing wrote yet
    for lid in (inv_a.id, inv_b.id):
        assert SalesCommission.query.filter_by(invoice_id=lid).count() == 0, \
            "dry-run should not have written"
    return f"planned {result['planned_creates']} creates, no writes"


@check("10. Backfill --apply creates missing rows dated to invoice date")
def _():
    from app.models import SalesCommission, Invoice, JournalEntry
    from scripts.backfill_commission_accrual import run as backfill_run
    result = backfill_run(_STATE["company_id"], dry_run=False)
    for lid, expected_month in (
        (_STATE["legacy_inv_a_id"], 4),
        (_STATE["legacy_inv_b_id"], 5),
    ):
        row = SalesCommission.query.filter_by(invoice_id=lid).first()
        assert row is not None, f"backfill missed invoice {lid}"
        assert row.period_month == expected_month
        inv = db.session.get(Invoice, lid)
        entry = db.session.get(JournalEntry, row.journal_entry_id)
        assert entry.date == inv.issue_date, \
            f"journal date {entry.date} != invoice.issue_date {inv.issue_date}"
    return f"created {result['applied']['created']} rows all dated to invoice"


@check("11. Backfill re-dates a mis-dated row + its journal entry")
def _():
    from app.models import SalesCommission, JournalEntry
    from scripts.backfill_commission_accrual import run as backfill_run
    # Take one of the backfilled rows and corrupt its period + journal date
    row = SalesCommission.query.filter_by(
        invoice_id=_STATE["legacy_inv_a_id"]
    ).first()
    row.period_year = 2027
    row.period_month = 1
    je = db.session.get(JournalEntry, row.journal_entry_id)
    je.date = date(2027, 1, 15)
    db.session.commit()
    result = backfill_run(_STATE["company_id"], dry_run=False)
    db.session.expire_all()
    row = db.session.get(SalesCommission, row.id)
    je = db.session.get(JournalEntry, row.journal_entry_id)
    assert row.period_year == 2026 and row.period_month == 4, \
        f"still bad: {row.period_year}-{row.period_month}"
    assert je.date == date(2026, 4, 15), f"journal date still bad: {je.date}"
    return f"row re-dated + journal date corrected"


@check("12. Backfill is idempotent: second apply is a no-op")
def _():
    from scripts.backfill_commission_accrual import run as backfill_run
    result = backfill_run(_STATE["company_id"], dry_run=True)
    assert result["planned_creates"] == 0 and result["planned_redates"] == 0, \
        f"still work to do: creates={result['planned_creates']}, redates={result['planned_redates']}"
    return "no more changes to make"


@check("13. Monthly profit: revenue + commission expense in the same month")
def _():
    """Verify the accountant-facing income_statement shows revenue AND
    commission expense in June for inv1 (issue_date=2026-06-30)."""
    from app.services.reports import income_statement
    cid = _STATE["company_id"]
    # Window on June 2026 only
    r = income_statement(
        cid,
        start_date=date(2026, 6, 1), end_date=date(2026, 6, 30),
    )
    # Find revenue + commission on the report
    rev_codes = {row["code"]: row["balance"] for row in r["revenue"]}
    exp_codes = {row["code"]: row["balance"] for row in r["expenses"]}
    assert "4100" in rev_codes and rev_codes["4100"] >= 999, \
        f"June revenue missing/incomplete: {rev_codes}"
    assert "5280" in exp_codes and 99 <= exp_codes["5280"] <= 101, \
        f"June commission missing/incomplete: {exp_codes}"
    return f"June: revenue 4100 {rev_codes['4100']:.0f}, commission 5280 {exp_codes['5280']:.0f}"


# ─── Run ───────────────────────────────────────────────────────────────
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
                except Exception as e:
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "company_id" in _STATE:
                    _teardown(_STATE["company_id"])
                    print(f"\n(cleaned up company #{_STATE['company_id']})")
            except Exception as e:
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
