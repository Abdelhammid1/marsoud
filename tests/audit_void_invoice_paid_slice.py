#!/usr/bin/env python3
"""MARSOUD-VOID-PAID-SLICE-01 (Abdelhamid 2026-09-20) — void of a
partially-paid sales invoice must split the reversal JE between
Cash (for the actually-paid slice) and AR (for the unpaid balance),
never over-credit Cash by the whole invoice.total.

Pre-fix, `issue_refund(FULL)` did:
    Dr 4300 subtotal + Dr 2120 tax → Cr 1110 invoice.total
plus `invoice.paid_amount -= invoice.total` (which made it negative
when paid < total).

Post-fix, the same call produces:
    Dr 4300 subtotal + Dr 2120 tax → Cr 1110 paid + Cr AR(customer) (total - paid)
plus `invoice.paid_amount = 0.0`.

Six checks lifted from the ticket ACs:
  1. Unpaid FULL void → NO cash line, one AR reversal line.
  2. Fully-paid FULL void → one cash line for total, NO AR line.
  3. Partial-paid single-payment FULL void → both lines.
  4. Multi-payment partial-paid (VB-0088 pattern) → same shape,
     amounts add up correctly.
  5. Balance guard: Σ debits == Σ credits for every void JE.
  6. `invoice.paid_amount` is never negative across any of the above.
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
        # journal_lines has no `company_id` column — the generic loop
        # below would skip it and leave orphan lines that later
        # (re-)attach to reused entry_id values in the next run,
        # producing bogus test results.  Clean them explicitly first.
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
            "DELETE FROM users WHERE email LIKE 'vps-%@x.test'"))


def _setup():
    from app.models import Company, User, user_companies, Customer
    from werkzeug.security import generate_password_hash

    for name in ("__VOID_PAID_SLICE__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__VOID_PAID_SLICE__", base_currency="SAR",
                 vat_rate=15)
    db.session.add(a); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(a.id)

    u = User(email="vps-owner@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="vps-owner")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=a.id, role="owner"))
    customer = Customer(
        company_id=a.id, name="VPS-Customer",
        email="vpsc@x.test", phone="0500000000",
    )
    db.session.add(customer); db.session.commit()
    _STATE.update(a_id=a.id, owner_id=u.id, customer_id=customer.id)


def _fresh_invoice(unit_price):
    """Post + send a fresh SENT invoice with one line item at
    `unit_price`.  Returns the Invoice ORM object."""
    from app.models import Invoice, InvoiceItem, InvoiceStatus
    from app.services.invoicing import post_invoice_to_ledger
    from app.services.numbering import next_number
    number = next_number(_STATE["a_id"], "INVOICE")
    inv = Invoice(
        company_id=_STATE["a_id"],
        number=number,
        customer_id=_STATE["customer_id"],
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="SAR", tax_rate=15,
        status=InvoiceStatus.DRAFT,
        created_by_id=_STATE["owner_id"],
    )
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


def _pay(invoice, amount):
    from app.services.invoicing import record_payment
    record_payment(invoice, float(amount), method="cash",
                    created_by=_STATE["owner_id"])


def _void(invoice):
    # Test the actual user-facing path: the "delete" button on
    # /invoices/<id> calls void_invoice(), which in turn calls
    # issue_refund(FULL) and then stamps status=VOIDED.
    from app.services.invoicing import void_invoice
    void_invoice(invoice, reason="test-void",
                  actor_id=_STATE["owner_id"])


def _cash_account_id():
    from app.models import Account
    acc = Account.query.filter_by(
        company_id=_STATE["a_id"], code="1110").first()
    assert acc, "cash account 1110 missing"
    return acc.id


def _ar_account_id_for(invoice):
    from app.services.subsidiary import party_ar_account
    return party_ar_account(invoice).id


def _void_je(invoice):
    """Return the ONE refund/void JournalEntry attached to this
    invoice's Refund record (there is exactly one)."""
    from app.models import Refund, JournalEntry
    r = Refund.query.filter_by(invoice_id=invoice.id).order_by(
        Refund.id.desc()).first()
    assert r is not None, f"no Refund row for invoice {invoice.number}"
    je = db.session.get(JournalEntry, r.journal_entry_id)
    assert je is not None, "refund's JE missing"
    return je


def _credit_lines(je, account_id):
    """Return (list of credit amounts) for `je` on `account_id`."""
    return [float(ln.credit)
             for ln in je.lines
             if ln.account_id == account_id and float(ln.credit) > 0]


# ─── Checks ────────────────────────────────────────────────────────
@check("1. Unpaid FULL void: cash untouched, AR credited for total")
def _():
    inv = _fresh_invoice(unit_price=100)   # total = 115 (15% VAT)
    total = float(inv.total)
    _void(inv)
    je = _void_je(inv)
    cash_credits = _credit_lines(je, _cash_account_id())
    ar_credits = _credit_lines(je, _ar_account_id_for(inv))
    assert cash_credits == [], f"cash should be untouched, got {cash_credits}"
    assert len(ar_credits) == 1 and abs(ar_credits[0] - total) < 0.01, \
        f"AR credit expected {total}, got {ar_credits}"
    assert float(inv.paid_amount or 0) == 0.0
    return f"AR credited {total:.2f}, cash untouched"


@check("2. Fully-paid FULL void: cash credited for total, AR untouched")
def _():
    inv = _fresh_invoice(unit_price=200)
    total = float(inv.total)
    _pay(inv, total)
    _void(inv)
    je = _void_je(inv)
    cash_credits = _credit_lines(je, _cash_account_id())
    ar_credits = _credit_lines(je, _ar_account_id_for(inv))
    assert len(cash_credits) == 1 and abs(cash_credits[0] - total) < 0.01, \
        f"cash credit expected {total}, got {cash_credits}"
    assert ar_credits == [], f"AR should be untouched here, got {ar_credits}"
    assert float(inv.paid_amount or 0) == 0.0
    return f"cash credited {total:.2f}, AR untouched"


@check("3. Partial-paid single-payment FULL void: split cash + AR")
def _():
    inv = _fresh_invoice(unit_price=1000)
    total = float(inv.total)          # 1150
    paid = 400.0
    _pay(inv, paid)
    _void(inv)
    je = _void_je(inv)
    cash_credits = _credit_lines(je, _cash_account_id())
    ar_credits = _credit_lines(je, _ar_account_id_for(inv))
    assert len(cash_credits) == 1 and abs(cash_credits[0] - paid) < 0.01, \
        f"cash credit expected {paid}, got {cash_credits}"
    assert len(ar_credits) == 1 and abs(ar_credits[0] - (total - paid)) < 0.01, \
        f"AR credit expected {total - paid}, got {ar_credits}"
    assert float(inv.paid_amount or 0) == 0.0, \
        f"paid_amount must be 0, got {inv.paid_amount}"
    return f"cash {paid:.2f} + AR {total - paid:.2f}"


@check("4. Multi-payment partial-paid FULL void (VB-0088 pattern)")
def _():
    # 2500 total pre-VAT → invoice.total = 2875 (with 15% VAT).
    # Three payments summing to 1500 leave 1375 outstanding.
    inv = _fresh_invoice(unit_price=2500)
    total = float(inv.total)
    _pay(inv, 1200)
    _pay(inv, 250)
    _pay(inv, 50)
    paid = 1500.0
    _void(inv)
    je = _void_je(inv)
    cash_credits = _credit_lines(je, _cash_account_id())
    ar_credits = _credit_lines(je, _ar_account_id_for(inv))
    assert sum(cash_credits) - paid < 0.01, \
        f"cash credit expected {paid}, got {cash_credits}"
    assert sum(ar_credits) - (total - paid) < 0.01, \
        f"AR credit expected {total - paid}, got {ar_credits}"
    assert float(inv.paid_amount or 0) == 0.0
    return (f"cash {paid:.2f} across payments; "
            f"AR closed {total - paid:.2f}; paid_amount = 0")


@check("5. Balance guard: Σ debits == Σ credits for every void JE")
def _():
    from app.models import JournalEntry
    entries = JournalEntry.query.filter(
        JournalEntry.company_id == _STATE["a_id"],
        JournalEntry.source_type == "refund",
    ).all()
    assert entries, "no void JEs recorded"
    for je in entries:
        d = sum(float(ln.debit or 0) for ln in je.lines)
        c = sum(float(ln.credit or 0) for ln in je.lines)
        assert abs(d - c) < 0.01, \
            f"unbalanced JE #{je.id}: Dr {d} vs Cr {c}"
    return f"{len(entries)} void JEs, all balanced"


@check("6. paid_amount is never negative across all voided invoices")
def _():
    from app.models import Invoice, InvoiceStatus
    voided = Invoice.query.filter(
        Invoice.company_id == _STATE["a_id"],
        Invoice.status == InvoiceStatus.VOIDED,
    ).all()
    assert voided, "no voided invoices to inspect"
    for inv in voided:
        assert float(inv.paid_amount or 0) >= 0, \
            f"invoice {inv.number} paid_amount = {inv.paid_amount}"
    return f"{len(voided)} voided invoices, all paid_amount >= 0"


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
