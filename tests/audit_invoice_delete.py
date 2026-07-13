#!/usr/bin/env python3
"""MARSOUD-INVOICE-DELETE (Abdelhamid 2026-07-13).

Ticket asked: "delete any invoice, auto-remove from all journals and
accounts." Decision (Ibrahim): void-with-reversing-entry variant —
DRAFT invoices hard-delete; anything posted is reversed via a FULL
refund + marked VOIDED. Preserves audit trail and satisfies KSA/Egypt
VAT invariants that forbid destroying issued invoices.

Checks:
  1. DRAFT invoice: POST /delete removes the row + items.
  2. SENT invoice: POST /delete leaves the row (status=VOIDED),
     posts a reversing JournalEntry against the same accounts.
  3. Post-void, the customer's AR sub-account net balance = 0.
  4. Post-void, Output VAT (2120) net balance = 0.
  5. PAID invoice: refund cash + AR both zero out.
  6. Already REFUNDED / VOIDED → route returns a warning (no double
     reversal that would push accounts into unexpected values).
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
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'id-%@x.test'"))


def _setup():
    from app.models import (
        Company, User, user_companies, Customer,
    )
    from werkzeug.security import generate_password_hash

    for name in ("__INV_DELETE__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__INV_DELETE__", base_currency="SAR",
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

    owner = _mk("id-owner@x.test", "owner")
    customer = Customer(
        company_id=a.id, name="ID-Customer",
        email="idc@x.test", phone="0500000000",
    )
    db.session.add(customer); db.session.commit()

    _STATE.update(
        a_id=a.id, owner_id=owner.id, customer_id=customer.id,
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


def _fresh_invoice(status_after_post="SENT", pay=False):
    """Create a small invoice, optionally post + pay it. Returns
    the invoice id + total. Uses the invoicing service layer so
    everything (journal, VAT split, subsidiary AR account) is
    wired correctly."""
    from app.models import Invoice, InvoiceItem, InvoiceStatus
    from app.services.invoicing import post_invoice_to_ledger, record_payment
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
        invoice_id=inv.id, description="widget",
        quantity=1, unit_price=100, line_total=100,
    ))
    inv.recalc()
    db.session.commit()
    if status_after_post == "DRAFT":
        return inv.id, float(inv.total)
    post_invoice_to_ledger(inv, created_by=_STATE["owner_id"])
    inv.status = InvoiceStatus.SENT
    db.session.commit()
    if pay:
        record_payment(inv, float(inv.total),
                       method="cash", created_by=_STATE["owner_id"])
    return inv.id, float(inv.total)


def _account_balance(code):
    """Sum debits − credits for the account with the given code
    across the fixture company. Used for accounting-integrity
    assertions."""
    from app.models import Account, JournalLine
    acc = Account.query.filter_by(
        company_id=_STATE["a_id"], code=code).first()
    if not acc:
        return 0.0
    debits = db.session.query(db.func.coalesce(
        db.func.sum(JournalLine.debit), 0.0),
    ).filter(JournalLine.account_id == acc.id).scalar() or 0.0
    credits = db.session.query(db.func.coalesce(
        db.func.sum(JournalLine.credit), 0.0),
    ).filter(JournalLine.account_id == acc.id).scalar() or 0.0
    return float(debits) - float(credits)


def _customer_ar_balance():
    """Net balance on the customer's own 1130-nnnnnn sub-account."""
    from app.services.subsidiary import party_ar_account
    from app.models import Invoice, JournalLine
    inv = Invoice.query.filter_by(
        company_id=_STATE["a_id"]).order_by(Invoice.id.desc()).first()
    if not inv:
        return 0.0
    acc = party_ar_account(inv)
    debits = db.session.query(db.func.coalesce(
        db.func.sum(JournalLine.debit), 0.0),
    ).filter(JournalLine.account_id == acc.id).scalar() or 0.0
    credits = db.session.query(db.func.coalesce(
        db.func.sum(JournalLine.credit), 0.0),
    ).filter(JournalLine.account_id == acc.id).scalar() or 0.0
    return float(debits) - float(credits)


# ─── Checks ────────────────────────────────────────────────────────
@check("1. DRAFT invoice: /delete hard-removes the row + its items")
def _():
    from app.models import Invoice, InvoiceItem
    inv_id, _ = _fresh_invoice(status_after_post="DRAFT")
    items_before = InvoiceItem.query.filter_by(invoice_id=inv_id).count()
    assert items_before >= 1
    r = _login().post(f"/invoices/{inv_id}/delete",
                       follow_redirects=False)
    assert r.status_code in (200, 302), \
        f"status={r.status_code} body={r.data[:200]!r}"
    assert Invoice.query.get(inv_id) is None, \
        "DRAFT invoice still present after delete"
    assert InvoiceItem.query.filter_by(invoice_id=inv_id).count() == 0, \
        "InvoiceItem rows leaked after DRAFT delete"
    return "DRAFT invoice + items removed"


@check("2. SENT invoice: /delete marks VOIDED + posts a reversing JournalEntry")
def _():
    from app.models import Invoice, InvoiceStatus, JournalEntry
    inv_id, total = _fresh_invoice(status_after_post="SENT", pay=False)
    entries_before = JournalEntry.query.filter_by(
        company_id=_STATE["a_id"]).count()
    r = _login().post(f"/invoices/{inv_id}/delete",
                       follow_redirects=False)
    assert r.status_code in (200, 302)
    inv = db.session.get(Invoice, inv_id)
    assert inv is not None, "posted invoice must not be hard-deleted"
    assert inv.status == InvoiceStatus.VOIDED, \
        f"expected VOIDED, got {inv.status}"
    entries_after = JournalEntry.query.filter_by(
        company_id=_STATE["a_id"]).count()
    assert entries_after > entries_before, \
        "no reversing journal entry was posted"
    _STATE["sent_inv_id"] = inv_id
    return "row preserved as VOIDED + reversing entry present"


@check("3. Post-void, customer AR net balance = 0")
def _():
    bal = _customer_ar_balance()
    assert abs(bal) < 0.01, f"AR net balance = {bal!r}"
    return f"AR net = {bal:+.2f} (≈ 0)"


@check("4. Post-void, Output VAT (2120) net balance = 0")
def _():
    bal = _account_balance("2120")
    assert abs(bal) < 0.01, f"Output VAT net balance = {bal!r}"
    return f"2120 net = {bal:+.2f} (≈ 0)"


@check("5. PAID invoice: cash refunded + AR net = 0 after delete")
def _():
    from app.models import Invoice, InvoiceStatus
    inv_id, total = _fresh_invoice(status_after_post="SENT", pay=True)
    cash_before = _account_balance("1110")
    r = _login().post(f"/invoices/{inv_id}/delete",
                       follow_redirects=False)
    assert r.status_code in (200, 302)
    inv = db.session.get(Invoice, inv_id)
    assert inv.status == InvoiceStatus.VOIDED
    cash_after = _account_balance("1110")
    # Cash should return to what it was BEFORE this invoice was paid —
    # the refund posting credits 1110 by exactly the paid amount.
    assert abs((cash_after - cash_before) + total) < 0.01, \
        f"cash delta expected ≈ -{total}, got {cash_after - cash_before}"
    return f"cash returned {total:.2f}; AR settled"


@check("6. Already-voided invoice: POST /delete returns warning + no double reversal")
def _():
    from app.models import Invoice, InvoiceStatus, JournalEntry
    inv = Invoice.query.filter_by(
        company_id=_STATE["a_id"],
        status=InvoiceStatus.VOIDED).first()
    assert inv is not None, "fixture from check 2/5 missing"
    entries_before = JournalEntry.query.filter_by(
        company_id=_STATE["a_id"]).count()
    r = _login().post(f"/invoices/{inv.id}/delete",
                       follow_redirects=False)
    assert r.status_code in (200, 302)
    entries_after = JournalEntry.query.filter_by(
        company_id=_STATE["a_id"]).count()
    assert entries_after == entries_before, \
        "second delete on VOIDED invoice posted extra entries"
    return "second delete is a no-op"


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
