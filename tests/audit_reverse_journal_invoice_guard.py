#!/usr/bin/env python3
"""MARSOUD-LEDGER-REVERSE-INVOICE-GUARD-01 — refuse to reverse a JE
whose source_type is `"invoice"` or `"refund"` from the generic
/journals reverse path.

Before this ticket, `reverse_journal()` in `app/services/ledger.py`
refused to reverse a reversal and refused to double-reverse the same
entry, but did not refuse an invoice-linked JE.  Any operator with
`journals.reverse` permission could open a voided invoice's JE, hit
"عكس" from the general /journals page, and the ledger would flip
cleanly while `Invoice.status` stayed VOIDED — invoice + ledger in
contradictory states, chainable to accumulate permanent bad balances
on the customer sub-account and on 4300 مردودات المبيعات.

This audit posts three JEs (source_type="invoice", "refund", None)
against a fresh company and pins:

  * Reversing the source_type="invoice" JE from reverse_journal()
    raises LedgerError with the Arabic redirect message.
  * Reversing the source_type="refund" JE raises the same.
  * Reversing an unrelated JE (source_type=None, i.e. a manual
    /journals/new entry) still succeeds — the guard did not over-fire.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__REVERSE_INVOICE_GUARD_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import Company, Account, JournalEntry, JournalLine
    from app.services.ledger import post_journal

    existing = Company.query.filter_by(name=COMPANY_NAME).first()
    if existing:
        _teardown_company(existing.id)

    # Sweep orphan JournalLines from prior aborted runs so the fresh
    # company's freshly-assigned account IDs don't accidentally collide
    # with an old row and pollute the pin.
    orphan_ids = [r.id for r in JournalLine.query.filter(
        JournalLine.entry_id.notin_(db.session.query(JournalEntry.id))
    ).all()]
    if orphan_ids:
        JournalLine.query.filter(
            JournalLine.id.in_(orphan_ids)
        ).delete(synchronize_session=False)
        db.session.commit()

    c = Company(name=COMPANY_NAME, base_currency="EGP")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)

    cash = Account.query.filter_by(company_id=c.id, code="1110").one()
    payable = Account.query.filter_by(company_id=c.id, code="2210").one()

    def _post(source_type, description):
        return post_journal(
            company_id=c.id,
            entry_date=date.today(),
            description=description,
            lines=[
                {"account_id": cash.id,    "debit": 100.0, "credit": 0.0},
                {"account_id": payable.id, "debit": 0.0,   "credit": 100.0},
            ],
            source_type=source_type,
            source_id=1 if source_type else None,
        )

    _STATE["company_id"] = c.id
    _STATE["invoice_je_id"] = _post("invoice", "invoice fixture").id
    _STATE["refund_je_id"]  = _post("refund",  "refund fixture").id
    _STATE["manual_je_id"]  = _post(None,      "manual fixture").id
    db.session.commit()


def _teardown_company(company_id):
    from app.models import Company, JournalEntry, JournalLine
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    entry_ids = [r.id for r in JournalEntry.query.filter_by(
        company_id=company_id).all()]
    if entry_ids:
        JournalLine.query.filter(
            JournalLine.entry_id.in_(entry_ids)
        ).delete(synchronize_session=False)
    for table in reversed(db.metadata.sorted_tables):
        if "company_id" in {c["name"] for c in insp.get_columns(table.name)}:
            db.session.execute(
                table.delete().where(table.c.company_id == company_id)
            )
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()


# ─── Guard fires on invoice-linked JEs ───────────────────────────
@check("1. reverse_journal refuses source_type='invoice' with redirect msg")
def _():
    from app.services.ledger import reverse_journal, LedgerError
    try:
        reverse_journal(_STATE["invoice_je_id"])
    except LedgerError as e:
        msg = str(e)
        assert "فاتورة" in msg, f"error missing 'فاتورة': {msg!r}"
        assert "شاشة" in msg, f"error missing 'شاشة' redirect hint: {msg!r}"
        return f"refused: {msg[:60]}"
    raise AssertionError(
        "reverse_journal accepted an invoice-linked JE — guard broken!")


@check("2. reverse_journal refuses source_type='refund' with same msg")
def _():
    from app.services.ledger import reverse_journal, LedgerError
    try:
        reverse_journal(_STATE["refund_je_id"])
    except LedgerError as e:
        msg = str(e)
        assert "فاتورة" in msg, f"error missing 'فاتورة': {msg!r}"
        return f"refused: {msg[:60]}"
    raise AssertionError(
        "reverse_journal accepted a refund JE — guard broken!")


# ─── Guard does NOT over-fire on unrelated JEs ───────────────────
@check("3. reverse_journal still succeeds on source_type=None (manual /journals)")
def _():
    from app.services.ledger import reverse_journal
    from app.models import JournalEntry
    new_entry = reverse_journal(_STATE["manual_je_id"])
    assert new_entry is not None, "reverse_journal returned None on manual JE"
    assert new_entry.is_reversal, "returned entry not marked is_reversal"
    assert new_entry.reversal_of == _STATE["manual_je_id"], \
        f"reversal_of {new_entry.reversal_of} != {_STATE['manual_je_id']}"
    return f"reversal entry #{new_entry.id} posted"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:
                    print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "company_id" in _STATE:
                    _teardown_company(_STATE["company_id"])
                    print(f"\n(cleaned up fixture company "
                          f"#{_STATE['company_id']})")
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
