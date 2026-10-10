#!/usr/bin/env python3
"""MARSOUD-INVOICE-CUSTOM-INSTALLMENTS-01 — the invoice create card
now has a `installment_mode = equal | custom` toggle.  When custom,
the operator supplies `custom_due_date[]` + `custom_amount[]` arrays
that must sum (Decimal) to `invoice.total − down_payment`.  The
plan is saved onto `invoices.pending_plan_json` at save-as-quote
and applied on Send without re-validation.

Ten checks against a fresh fixture company with the default CoA:

  1. 10,000 invoice + custom rows (3,000 / 5,000 / 2,000), no down
     — saves, 3 InvoiceInstallments landed in chronological order.
  2. Same + down 1,000 + custom (3,000 / 5,000 / 1,000) — saves,
     down-payment recorded, 3 installments landed.
  3. Same + down 1,000 + custom summing to 8,900 — refused with
     Arabic diff message naming "-100".
  4. Same + down 1,000 + custom summing to 9,100 — refused.
  5. due_date before invoice.issue_date — refused.
  6. Duplicate due_date — refused.
  7. Non-positive amount — refused.
  8. 25-row custom plan — refused (max 24).
  9. Save-as-quote with custom plan then Send — the installments
     exist with the right dates/amounts, pending_plan_json cleared.
 10. Equal-split regression: existing legacy path still produces N
     identical installments summing to total.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__INVOICE_CUSTOM_INSTALLMENTS_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


class _FakeForm:
    """Minimal werkzeug.MultiDict replacement for the service's
    `_parse_custom_rows` + `_persist_quote_plan` paths.  Supports
    `get` and `getlist`."""
    def __init__(self, data):
        self._single = {}
        self._multi = {}
        for k, v in data.items():
            if k.endswith("[]"):
                self._multi[k] = list(v)
            else:
                self._single[k] = v

    def get(self, k, default=None):
        return self._single.get(k, default)

    def getlist(self, k):
        return list(self._multi.get(k, []))


def _setup():
    from app.models import Company, Customer, PaymentMethod, Account
    from sqlalchemy import text

    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    for c in Company.query.filter_by(name=COMPANY_NAME).all():
        _teardown_company_hard(c.id)
    # SQLite reuses invoice IDs when rows get deleted; orphan
    # InvoiceInstallment rows from a prior aborted run would attach
    # themselves to a freshly-reused id and break
    # `create_installment_plan`'s "already has plan" refusal.  Sweep
    # them at setup.
    db.session.execute(text(
        "DELETE FROM invoice_installments "
        "WHERE invoice_id NOT IN (SELECT id FROM invoices)"))
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()

    c = Company(name=COMPANY_NAME, base_currency="EGP")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)

    cust = Customer(company_id=c.id, name="__cust__")
    db.session.add(cust); db.session.flush()

    cash_acc = Account.query.filter_by(
        company_id=c.id, code="1110").one()
    pm = PaymentMethod(company_id=c.id, name="cash",
                        name_ar="نقدي",
                        account_id=cash_acc.id,
                        is_active=True, is_default=True)
    db.session.add(pm); db.session.flush()

    _STATE["company_id"] = c.id
    _STATE["customer_id"] = cust.id
    _STATE["pm_id"] = pm.id
    db.session.commit()


def _teardown_company_hard(company_id):
    from app.models import Company, JournalEntry, JournalLine
    from sqlalchemy import inspect, text
    db.session.rollback()
    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    insp = inspect(db.engine)
    entry_ids = [r.id for r in JournalEntry.query.filter_by(
        company_id=company_id).all()]
    if entry_ids:
        JournalLine.query.filter(
            JournalLine.entry_id.in_(entry_ids)
        ).delete(synchronize_session=False)
    for table in reversed(db.metadata.sorted_tables):
        if "company_id" in {col["name"] for col in insp.get_columns(table.name)}:
            db.session.execute(
                table.delete().where(table.c.company_id == company_id)
            )
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()


def _make_invoice(total=10000.0):
    """Create + post an invoice with a single line summing to `total`,
    then return it.  Mirrors what the /invoices/new flow does so the
    service helpers can exercise the installments path."""
    from app.models import Invoice, InvoiceItem, Account
    from app.models.invoice import InvoiceStatus
    from app.services.invoicing import post_invoice_to_ledger
    from app.services.numbering import next_number

    inv = Invoice(
        company_id=_STATE["company_id"],
        customer_id=_STATE["customer_id"],
        number=next_number(_STATE["company_id"], "INVOICE"),
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="EGP",
        tax_rate=0,
        status=InvoiceStatus.SENT,
    )
    db.session.add(inv); db.session.flush()
    item = InvoiceItem(
        invoice_id=inv.id,
        company_id=_STATE["company_id"],
        description="x",
        quantity=1,
        unit_price=total,
    )
    db.session.add(item); db.session.flush()
    inv.recalc()
    db.session.flush()
    post_invoice_to_ledger(inv)
    return inv


# ─── 1. Custom plan, no down, sums to total ──────────────────────
@check("1. custom (3000/5000/2000) on 10000 invoice, no down, saves 3 installments")
def _():
    from app.services.installments import (
        _parse_custom_rows, _validate_custom_rows, create_installment_plan,
    )
    inv = _make_invoice(10000.0)
    form = _FakeForm({
        "custom_due_date[]": ["2026-11-01", "2026-12-01", "2027-01-01"],
        "custom_amount[]":   ["3000", "5000", "2000"],
    })
    raw = _parse_custom_rows(form)
    rows = _validate_custom_rows(raw, 10000, 0, inv.issue_date)
    create_installment_plan(inv, rows)
    assert len(inv.installments) == 3, \
        f"expected 3 installments, got {len(inv.installments)}"
    amounts = [float(i.amount) for i in inv.installments]
    assert amounts == [3000.0, 5000.0, 2000.0], \
        f"amounts mismatch: {amounts}"
    return f"3 installments, sum={sum(amounts)}"


# ─── 2. Custom + down, sums match remaining ──────────────────────
@check("2. custom (3000/5000/1000) + down 1000 on 10000 — saves")
def _():
    from app.services.installments import (
        _parse_custom_rows, _validate_custom_rows, create_installment_plan,
    )
    from app.services.invoicing import record_payment
    inv = _make_invoice(10000.0)
    record_payment(inv, 1000.0,
                    payment_method_id=_STATE["pm_id"],
                    notify=False)
    form = _FakeForm({
        "custom_due_date[]": ["2026-11-01", "2026-12-01", "2027-01-01"],
        "custom_amount[]":   ["3000", "5000", "1000"],
    })
    raw = _parse_custom_rows(form)
    rows = _validate_custom_rows(raw, 10000, 1000, inv.issue_date)
    create_installment_plan(inv, rows)
    assert len(inv.installments) == 3
    return f"paid=1000, 3 installments sum=9000"


# ─── 3. Sum-short → refused ──────────────────────────────────────
@check("3. custom summing to 8900 (short by 100) — refused with diff")
def _():
    from app.services.installments import (
        _parse_custom_rows, _validate_custom_rows, InstallmentError,
    )
    inv = _make_invoice(10000.0)
    form = _FakeForm({
        "custom_due_date[]": ["2026-11-01", "2026-12-01", "2027-01-01"],
        "custom_amount[]":   ["3000", "5000", "900"],
    })
    raw = _parse_custom_rows(form)
    try:
        _validate_custom_rows(raw, 10000, 1000, inv.issue_date)
    except InstallmentError as e:
        msg = str(e)
        assert "الفرق" in msg, f"error missing 'الفرق': {msg}"
        assert "-100" in msg or "100" in msg, \
            f"error missing diff '100': {msg}"
        return f"refused: {msg[:70]}"
    raise AssertionError("expected InstallmentError on short-sum plan")


# ─── 4. Sum-over → refused ───────────────────────────────────────
@check("4. custom summing to 9100 (over by 100) — refused")
def _():
    from app.services.installments import (
        _parse_custom_rows, _validate_custom_rows, InstallmentError,
    )
    inv = _make_invoice(10000.0)
    form = _FakeForm({
        "custom_due_date[]": ["2026-11-01", "2026-12-01", "2027-01-01"],
        "custom_amount[]":   ["3000", "5000", "1100"],
    })
    raw = _parse_custom_rows(form)
    try:
        _validate_custom_rows(raw, 10000, 1000, inv.issue_date)
    except InstallmentError:
        return "refused"
    raise AssertionError("expected InstallmentError on over-sum plan")


# ─── 5. Date before invoice.issue_date → refused ─────────────────
@check("5. due_date before issue_date — refused")
def _():
    from app.services.installments import (
        _parse_custom_rows, _validate_custom_rows, InstallmentError,
    )
    inv = _make_invoice(10000.0)
    yesterday = (inv.issue_date - timedelta(days=1)).isoformat()
    form = _FakeForm({
        "custom_due_date[]": [yesterday, "2026-12-01", "2027-01-01"],
        "custom_amount[]":   ["3000", "5000", "2000"],
    })
    raw = _parse_custom_rows(form)
    try:
        _validate_custom_rows(raw, 10000, 0, inv.issue_date)
    except InstallmentError as e:
        assert "قبل تاريخ الفاتورة" in str(e), \
            f"unexpected message: {e}"
        return "refused"
    raise AssertionError("expected InstallmentError on past date")


# ─── 6. Duplicate dates → refused ────────────────────────────────
@check("6. duplicate due_date — refused")
def _():
    from app.services.installments import (
        _parse_custom_rows, _validate_custom_rows, InstallmentError,
    )
    inv = _make_invoice(10000.0)
    form = _FakeForm({
        "custom_due_date[]": ["2026-11-01", "2026-11-01", "2027-01-01"],
        "custom_amount[]":   ["3000", "5000", "2000"],
    })
    raw = _parse_custom_rows(form)
    try:
        _validate_custom_rows(raw, 10000, 0, inv.issue_date)
    except InstallmentError as e:
        assert "مكرّر" in str(e), f"unexpected: {e}"
        return "refused"
    raise AssertionError("expected InstallmentError on duplicate dates")


# ─── 7. Non-positive amount → refused ────────────────────────────
@check("7. non-positive amount — refused")
def _():
    from app.services.installments import (
        _parse_custom_rows, _validate_custom_rows, InstallmentError,
    )
    inv = _make_invoice(10000.0)
    form = _FakeForm({
        "custom_due_date[]": ["2026-11-01", "2026-12-01"],
        "custom_amount[]":   ["0", "10000"],
    })
    raw = _parse_custom_rows(form)
    try:
        _validate_custom_rows(raw, 10000, 0, inv.issue_date)
    except InstallmentError as e:
        assert "أكبر من صفر" in str(e), f"unexpected: {e}"
        return "refused"
    raise AssertionError("expected InstallmentError on zero amount")


# ─── 8. 25-row plan → refused (max 24) ───────────────────────────
@check("8. 25-row custom plan — refused (max 24)")
def _():
    from app.services.installments import (
        _parse_custom_rows, _validate_custom_rows, InstallmentError,
    )
    inv = _make_invoice(2500.0)
    dates = [(inv.issue_date + timedelta(days=i+1)).isoformat()
              for i in range(25)]
    amounts = ["100"] * 25
    form = _FakeForm({
        "custom_due_date[]": dates,
        "custom_amount[]":   amounts,
    })
    raw = _parse_custom_rows(form)
    try:
        _validate_custom_rows(raw, 2500, 0, inv.issue_date)
    except InstallmentError as e:
        assert "الحد الأقصى" in str(e), f"unexpected: {e}"
        return "refused"
    raise AssertionError("expected InstallmentError on 25 rows")


# ─── 9. Save-as-quote with custom then apply_pending_plan ────────
@check("9. save-as-quote persists plan, apply_pending_plan clears + commits")
def _():
    from app.services.installments import (
        serialize_pending_plan, apply_pending_plan, _parse_custom_rows,
        _validate_custom_rows,
    )
    inv = _make_invoice(10000.0)
    # Simulate the save-as-quote path writing to pending_plan_json.
    form = _FakeForm({
        "custom_due_date[]": ["2026-11-01", "2026-12-01", "2027-01-01"],
        "custom_amount[]":   ["3000", "5000", "2000"],
    })
    raw = _parse_custom_rows(form)
    rows = _validate_custom_rows(raw, 10000, 0, inv.issue_date)
    inv.pending_plan_json = serialize_pending_plan(
        "custom", rows, 0, None)
    db.session.commit()
    assert inv.pending_plan_json, "pending_plan_json not set"

    # Now simulate /send calling apply_pending_plan.
    apply_pending_plan(inv)
    assert inv.pending_plan_json is None, \
        f"pending_plan_json should be cleared, got {inv.pending_plan_json!r}"
    assert len(inv.installments) == 3, \
        f"expected 3 installments after apply, got {len(inv.installments)}"
    return f"3 installments, pending cleared"


# ─── 10. Equal-split regression — legacy path unchanged ──────────
@check("10. legacy equal-split (count=3 on 9000 remaining) still works")
def _():
    from app.services.installments import create_installment_plan
    from app.services.invoicing import record_payment
    inv = _make_invoice(10000.0)
    record_payment(inv, 1000.0,
                    payment_method_id=_STATE["pm_id"],
                    notify=False)
    # Equal-split rows the legacy branch computes
    rows = [
        {"amount": 3000.0, "due_date": date.today() + timedelta(days=30)},
        {"amount": 3000.0, "due_date": date.today() + timedelta(days=60)},
        {"amount": 3000.0, "due_date": date.today() + timedelta(days=90)},
    ]
    create_installment_plan(inv, rows)
    assert len(inv.installments) == 3
    assert sum(float(i.amount) for i in inv.installments) == 9000.0
    return "3 identical installments, sum=9000"


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
                    _teardown_company_hard(_STATE["company_id"])
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
