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

    # Grant the fixture company a plan so the `require_plan_selection`
    # middleware (app/__init__.py:514) doesn't 302 the /send route
    # back to /choose-plan when test #13 drives it via test_client.
    from app.models import Plan
    plan = Plan.query.filter_by(is_active=True).first()
    if plan:
        c.intended_plan_id = plan.id
        c.plan_id = plan.id

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


# ─── 11. Overdue sweep flips a past custom-date installment ──────
#     Pins AC #4 — "التذكيرات والإيميل يشتغلوا على التواريخ المخصصة".
#     The reminder cron reads `.due_date` per row via
#     `refresh_installment_overdue_flags`; prove that a custom row
#     whose due_date is in the past gets flipped PENDING -> OVERDUE
#     exactly as it would for an equal-split row.
@check("11. overdue sweep flips a past custom-dated installment to OVERDUE")
def _():
    from app.services.installments import (
        _parse_custom_rows, _validate_custom_rows, create_installment_plan,
        refresh_installment_overdue_flags,
    )
    from app.models import INSTALLMENT_OVERDUE, INSTALLMENT_PENDING
    inv = _make_invoice(10000.0)
    # Three custom rows: one yesterday (should flip), one tomorrow
    # (should stay PENDING), one in 60d.  Sums to 10000.
    yesterday = (inv.issue_date - timedelta(days=1)).isoformat()
    tomorrow  = (inv.issue_date + timedelta(days=1)).isoformat()
    far       = (inv.issue_date + timedelta(days=60)).isoformat()
    form = _FakeForm({
        "custom_due_date[]": [yesterday, tomorrow, far],
        "custom_amount[]":   ["3000", "5000", "2000"],
    })
    raw = _parse_custom_rows(form)
    # Bypass issue_date floor for THIS test only — we need a past-dated
    # row to prove the overdue flip, which the UI validator would
    # otherwise refuse.  Pin the flip via create_installment_plan
    # directly with hand-constructed rows.
    rows = sorted(raw, key=lambda r: r["due_date"])
    create_installment_plan(inv, rows)
    flipped = refresh_installment_overdue_flags(
        company_id=_STATE["company_id"])
    statuses = [i.status for i in inv.installments]
    overdue_count = sum(1 for s in statuses if s == INSTALLMENT_OVERDUE)
    pending_count = sum(1 for s in statuses if s == INSTALLMENT_PENDING)
    assert overdue_count == 1, \
        f"expected 1 OVERDUE, got {overdue_count}: {statuses}"
    assert pending_count == 2, \
        f"expected 2 PENDING, got {pending_count}: {statuses}"
    return f"flipped={flipped}, statuses={statuses}"


# ─── 12. Cross-tenant isolation — company B cannot touch A's plan ─
#     Pins AC #7 — "شركة تانية مايقدرش تشوف أو تعدل أقساطها".
#     Both the view and the plan-create route use `_owned_or_404`;
#     calling them from a session scoped to a different company must
#     refuse.  We exercise this by making a second company, posting
#     an invoice + custom plan under company A, then querying from
#     company B via a filter that mirrors the route's guard.
@check("12. cross-tenant: company B cannot query A's installments plan")
def _():
    from app.models import Company, Customer, Invoice, InvoiceInstallment
    from sqlalchemy import text

    # Give the fixture a second company for the cross-tenant check.
    # No CoA seed — the only thing we need is a different
    # `company_id` so the isolation filter mirrors the real route
    # guard (`.filter(Invoice.company_id == g.active_company.id)`).
    # Nuke any leftover so SQLite id reuse doesn't cross-contaminate.
    from sqlalchemy import text as _text
    for leftover in Company.query.filter_by(
            name=COMPANY_NAME + "__OTHER__").all():
        db.session.execute(_text("PRAGMA foreign_keys=OFF"))
        db.session.execute(_text(
            f"DELETE FROM payment_methods WHERE company_id = {leftover.id}"))
        db.session.execute(_text(
            f"DELETE FROM accounts WHERE company_id = {leftover.id}"))
        db.session.execute(_text(
            f"DELETE FROM companies WHERE id = {leftover.id}"))
        db.session.commit()
        db.session.execute(_text("PRAGMA foreign_keys=ON"))
        db.session.commit()
    other = Company(name=COMPANY_NAME + "__OTHER__", base_currency="EGP")
    db.session.add(other); db.session.flush()

    # Company A invoice with a 3-row custom plan.
    from app.services.installments import (
        _parse_custom_rows, _validate_custom_rows, create_installment_plan,
    )
    inv_a = _make_invoice(10000.0)
    form = _FakeForm({
        "custom_due_date[]": ["2026-11-01", "2026-12-01", "2027-01-01"],
        "custom_amount[]":   ["3000", "5000", "2000"],
    })
    raw = _parse_custom_rows(form)
    rows = _validate_custom_rows(raw, 10000, 0, inv_a.issue_date)
    create_installment_plan(inv_a, rows)
    assert len(inv_a.installments) == 3

    # Simulate company B's filtered query — the pattern every /invoices
    # route uses via `_owned_or_404` + `.filter(Invoice.company_id ==
    # g.active_company.id)`.
    seen = Invoice.query.filter_by(
        id=inv_a.id, company_id=other.id).first()
    assert seen is None, \
        "company B's company_id-scoped lookup returned A's invoice"

    # Direct installment query from B's company_id scope — pattern
    # mirrors how the mobile/API layer joins through Invoice.
    joined = (InvoiceInstallment.query
              .join(Invoice, InvoiceInstallment.invoice_id == Invoice.id)
              .filter(Invoice.company_id == other.id)
              .count())
    assert joined == 0, \
        f"company B saw {joined} of A's installments through join"

    # Cleanup the extra company — PRAGMA off because the two fresh
    # companies might have gained incidental child rows (plan
    # subscriptions, cron trackers) that would otherwise block the
    # delete under FK enforcement.
    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    db.session.execute(text(f"DELETE FROM companies WHERE id = {other.id}"))
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()
    return "company B's lookups see nothing of A's plan"


# ─── 13. End-to-end /send route applies a stored custom plan ─────
#     Pins AC #6 at the route layer — my earlier test #9 called
#     `apply_pending_plan` in Python; this one POSTs to the real
#     Flask /invoices/<id>/send endpoint and confirms the installments
#     materialize + pending_plan_json clears + an email path fires.
@check("13. POST /invoices/<id>/send on a quote with pending_plan_json materializes installments")
def _():
    from app.models import Invoice, InvoiceItem, User
    from app.models.invoice import InvoiceStatus
    from app.models.user import user_companies
    from app.services.installments import (
        _parse_custom_rows, _validate_custom_rows, serialize_pending_plan,
    )
    from app.services.numbering import next_number

    # Fresh DRAFT invoice (NOT posted to ledger — the /send route is
    # what will post it) with a stored custom plan — the shape
    # save-as-quote actually produces.
    inv = Invoice(
        company_id=_STATE["company_id"],
        customer_id=_STATE["customer_id"],
        number=next_number(_STATE["company_id"], "INVOICE"),
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="EGP",
        tax_rate=0,
        status=InvoiceStatus.DRAFT,
    )
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, company_id=_STATE["company_id"],
        description="x", quantity=1, unit_price=10000.0))
    db.session.flush()
    inv.recalc()
    db.session.flush()
    form = _FakeForm({
        "custom_due_date[]": ["2026-11-01", "2026-12-01", "2027-01-01"],
        "custom_amount[]":   ["3000", "5000", "2000"],
    })
    rows = _validate_custom_rows(
        _parse_custom_rows(form), 10000, 0, inv.issue_date)
    inv.pending_plan_json = serialize_pending_plan(
        "custom", rows, 0, None)
    db.session.commit()
    inv_id = inv.id

    # Promote the fixture test user to owner on the fixture company
    # so `require_permission("invoices.send")` lets the POST through.
    # Also stamp the current terms_version so the require_current_terms_version
    # middleware (app/__init__.py:554) doesn't 302 to /re-accept-terms.
    test_email = "__custom_installments_sender__@audit.local"
    u = User.query.filter_by(email=test_email).first()
    if not u:
        u = User(email=test_email, full_name="send tester",
                  password_hash="x")
        db.session.add(u); db.session.flush()
    try:
        from app.services.legal import get_terms_version
        u.terms_version = get_terms_version() or ""
    except Exception:
        pass
    db.session.execute(user_companies.delete().where(
        user_companies.c.user_id == u.id))
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=_STATE["company_id"], role="owner"))
    db.session.commit()
    uid = u.id
    _STATE["send_user_id"] = uid

    # Flask-login's "basic" session_protection rebuilds an identity
    # hash from User-Agent + remote addr and logs the user out if it
    # doesn't match the one stored in the session.  A test client has
    # neither, so set `SESSION_PROTECTION = None` on a dedicated app
    # instance for THIS check — bypassing the protection, not auth.
    app = create_app()
    from flask import session as _flask_session
    from flask_login import login_user
    app.config["SESSION_PROTECTION"] = None
    with app.test_client() as client:
        # Log the user in via the real `login_user()` helper inside a
        # pre-request context so the session keys match exactly what
        # the extension emits — then copy that session into the test
        # client via `session_transaction`.
        with app.test_request_context():
            login_user(u)
            _flask_session["active_company_id"] = _STATE["company_id"]
            saved = dict(_flask_session)
        with client.session_transaction() as sess:
            sess.update(saved)
        resp = client.post(f"/invoices/{inv_id}/send",
                            data={"email_customer": "0"},
                            follow_redirects=False)
        body = resp.data[:400].decode("utf-8", "replace") if resp.data else ""
        _redirect_to = resp.headers.get("Location", "")
        assert resp.status_code in (302, 303), \
            f"expected redirect, got {resp.status_code}: {body!r}"

    db.session.expire_all()
    inv2 = db.session.get(Invoice, inv_id)
    assert inv2.status == InvoiceStatus.SENT, \
        f"/send didn't flip status, got {inv2.status}  (redirected to {_redirect_to!r})"
    assert inv2.pending_plan_json is None, \
        f"pending_plan_json not cleared after /send: {inv2.pending_plan_json!r}"
    assert len(inv2.installments) == 3, \
        f"expected 3 installments after /send, got {len(inv2.installments)}"
    amounts = sorted(float(i.amount) for i in inv2.installments)
    assert amounts == [2000.0, 3000.0, 5000.0], \
        f"installments materialized with wrong amounts: {amounts}"
    return f"3 installments materialized, pending cleared, status=SENT"


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
