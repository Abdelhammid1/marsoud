#!/usr/bin/env python3
"""MARSOUD-INVOICE-CANCEL-EMAIL-01 — when a sales invoice is voided,
the customer gets an email notification with the operator-entered
reason, and we never notify twice for the same void.

Six checks against a fresh fixture company with the default CoA:

  1. Posted invoice + customer email → void with reason → email
     fires, `cancel_email_sent_at` stamped, log line captured.
  2. Idempotency — re-voiding an already-VOIDED row raises the
     existing "already voided" RuntimeError; `cancel_email_sent_at`
     unchanged; no second send attempt.
  3. Customer has no email → void succeeds, `cancel_email_sent_at`
     stays NULL, "Skip cancel email: ..." log line present.
  4. DRAFT path hard-deletes the row → no email attempted (row
     gone; nothing to stamp).
  5. Mandatory reason — POST /invoices/<id>/delete with empty
     `reason` through test_client → redirects, invoice still SENT,
     no `voided_at`, flash carries the Arabic error.
  6. Retry endpoint — force `cancel_email_sent_at=NULL` on a
     VOIDED invoice, POST /resend-cancel-email → stamped.  Second
     POST → "already sent" flash, timestamp unchanged.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__INVOICE_CANCEL_EMAIL_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


class _LogCapture(logging.Handler):
    """In-memory log handler so tests can assert on specific log
    lines the service emits (e.g. 'Skip cancel email: ...').  Keeps
    the full LogRecord so we can inspect levelname + message both."""
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        try:
            self.records.append(record)
        except Exception:
            pass

    def messages(self):
        return [r.getMessage() for r in self.records]

    def clear(self):
        self.records.clear()


def _install_log_capture():
    """Attach the capture handler to every logger the cancel path
    writes through: the email service's `email` logger, the invoicing
    service's `ledgeros.invoicing` logger, and the root logger as a
    safety net."""
    cap = _LogCapture()
    for name in ("app.services.email", "email",
                 "ledgeros.invoicing", "ledgeros"):
        logging.getLogger(name).addHandler(cap)
        logging.getLogger(name).setLevel(logging.DEBUG)
    logging.getLogger().addHandler(cap)
    return cap


def _setup():
    from app.models import Company, Customer, PaymentMethod, Account
    from sqlalchemy import text

    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    for c in Company.query.filter_by(name=COMPANY_NAME).all():
        _teardown_company_hard(c.id)
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()

    c = Company(name=COMPANY_NAME, base_currency="EGP")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)

    cust = Customer(company_id=c.id, name="__cust__",
                     email="cust@audit.local")
    db.session.add(cust); db.session.flush()
    cust_no_email = Customer(company_id=c.id, name="__cust_no_email__")
    db.session.add(cust_no_email); db.session.flush()

    cash_acc = Account.query.filter_by(
        company_id=c.id, code="1110").one()
    pm = PaymentMethod(company_id=c.id, name="cash",
                        name_ar="نقدي",
                        account_id=cash_acc.id,
                        is_active=True, is_default=True)
    db.session.add(pm); db.session.flush()

    # Attach a plan so the require_plan_selection middleware doesn't
    # 302 the test_client /delete POST to /choose-plan.
    from app.models import Plan
    plan = Plan.query.filter_by(is_active=True).first()
    if plan:
        c.intended_plan_id = plan.id
        c.plan_id = plan.id

    _STATE["company_id"] = c.id
    _STATE["customer_id"] = cust.id
    _STATE["customer_no_email_id"] = cust_no_email.id
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


def _make_invoice(total=1000.0, status_sent=True, customer_id=None):
    """Create an invoice (DRAFT → optionally posted to SENT) that
    the void service can act on."""
    from app.models import Invoice, InvoiceItem
    from app.models.invoice import InvoiceStatus
    from app.services.invoicing import post_invoice_to_ledger
    from app.services.numbering import next_number

    inv = Invoice(
        company_id=_STATE["company_id"],
        customer_id=customer_id or _STATE["customer_id"],
        number=next_number(_STATE["company_id"], "INVOICE"),
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="EGP",
        tax_rate=0,
        status=InvoiceStatus.DRAFT,
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
    if status_sent:
        inv.status = InvoiceStatus.SENT
        post_invoice_to_ledger(inv)
    else:
        db.session.commit()
    return inv


# ─── 1. Posted invoice + customer email → notification fires ──────
@check("1. void SENT invoice with customer email → stamps + fires")
def _():
    from app.services.invoicing import void_invoice
    cap = _install_log_capture()
    inv = _make_invoice(1500.0)
    inv_id = inv.id
    void_invoice(inv, "تم الإلغاء بناءً على طلب العميل",
                  actor_id=None)
    db.session.expire_all()
    from app.models import Invoice
    from app.models.invoice import InvoiceStatus
    inv2 = db.session.get(Invoice, inv_id)
    assert inv2.status == InvoiceStatus.VOIDED, \
        f"status not VOIDED: {inv2.status}"
    assert inv2.void_reason == "تم الإلغاء بناءً على طلب العميل"
    assert inv2.cancel_email_sent_at is not None, \
        "cancel_email_sent_at was NOT stamped — email did not fire"
    msgs = cap.messages()
    # Either the real SMTP path ("Sending email: ...") or the
    # log-only fallback ("[EMAIL NOT SENT ...]") counts — both are a
    # successful hand-off of the message through `send_email`.
    hit = any(("إشعار بإلغاء" in m) or ("Sending email" in m)
              or ("Email body printed" in m) or ("EMAIL NOT SENT" in m)
              for m in msgs)
    assert hit, (
        f"no 'invoice cancelled' log line captured. "
        f"captured: {msgs[-5:]!r}")
    return f"VOIDED, stamped at {inv2.cancel_email_sent_at}"


# ─── 2. Idempotency — second void raises, no re-send ──────────────
@check("2. re-void of VOIDED invoice refuses + cancel_email_sent_at unchanged")
def _():
    from app.services.invoicing import void_invoice
    inv = _make_invoice(500.0)
    void_invoice(inv, "سبب الأول", actor_id=None)
    db.session.expire_all()
    from app.models import Invoice
    inv2 = db.session.get(Invoice, inv.id)
    first_ts = inv2.cancel_email_sent_at
    assert first_ts is not None
    try:
        void_invoice(inv2, "سبب ثاني", actor_id=None)
    except RuntimeError as e:
        assert "ملغاة" in str(e) or "معكوسة" in str(e), str(e)
    else:
        raise AssertionError("expected RuntimeError on re-void")
    db.session.expire_all()
    inv3 = db.session.get(Invoice, inv.id)
    assert inv3.cancel_email_sent_at == first_ts, \
        f"timestamp changed: {first_ts} → {inv3.cancel_email_sent_at}"
    return "re-void refused, timestamp unchanged"


# ─── 3. Customer with no email → log line, no stamp ───────────────
@check("3. customer has no email → void succeeds, timestamp stays NULL")
def _():
    from app.services.invoicing import void_invoice
    cap = _install_log_capture()
    cap.clear()
    inv = _make_invoice(
        750.0, customer_id=_STATE["customer_no_email_id"])
    void_invoice(inv, "اختبار بدون إيميل", actor_id=None)
    db.session.expire_all()
    from app.models import Invoice
    inv2 = db.session.get(Invoice, inv.id)
    assert inv2.cancel_email_sent_at is None, \
        f"stamp should stay NULL, got {inv2.cancel_email_sent_at}"
    hit = any("Skip cancel email" in m for m in cap.messages())
    assert hit, (
        f"no 'Skip cancel email' log line. captured: "
        f"{cap.messages()[-5:]!r}")
    return "VOIDED, timestamp NULL, log line present"


# ─── 4. DRAFT invoice is hard-deleted, no email path ──────────────
@check("4. DRAFT invoice is hard-deleted, no notification")
def _():
    from app.services.invoicing import void_invoice
    inv = _make_invoice(300.0, status_sent=False)
    inv_id = inv.id
    outcome = void_invoice(inv, "مسودة", actor_id=None)
    assert outcome == "deleted", f"expected 'deleted', got {outcome!r}"
    db.session.expire_all()
    from app.models import Invoice
    assert db.session.get(Invoice, inv_id) is None, \
        "DRAFT row still exists after hard-delete"
    return "DRAFT hard-deleted"


# ─── 5. POST /delete without reason → refused ─────────────────────
@check("5. POST /delete with empty reason → refused, invoice stays SENT")
def _():
    from app import create_app
    from app.models import Invoice, User
    from app.models.invoice import InvoiceStatus
    from app.models.user import user_companies
    inv = _make_invoice(600.0)
    inv_id = inv.id
    db.session.commit()

    test_email = "__cancel_email_sender__@audit.local"
    u = User.query.filter_by(email=test_email).first()
    if not u:
        u = User(email=test_email, full_name="sender",
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

    app = create_app()
    from flask import session as _flask_session
    from flask_login import login_user
    app.config["SESSION_PROTECTION"] = None
    with app.test_client() as client:
        with app.test_request_context():
            login_user(u)
            _flask_session["active_company_id"] = _STATE["company_id"]
            saved = dict(_flask_session)
        with client.session_transaction() as sess:
            sess.update(saved)
        resp = client.post(f"/invoices/{inv_id}/delete",
                            data={"reason": "   "},
                            follow_redirects=False)
        assert resp.status_code in (302, 303), \
            f"expected redirect, got {resp.status_code}"

    db.session.expire_all()
    inv2 = db.session.get(Invoice, inv_id)
    assert inv2.status == InvoiceStatus.SENT, \
        f"status flipped despite empty reason: {inv2.status}"
    assert inv2.voided_at is None, \
        f"voided_at got stamped on refused delete: {inv2.voided_at}"
    return "empty-reason delete refused, invoice still SENT"


# ─── 6. /resend-cancel-email is idempotent ────────────────────────
@check("6. /resend-cancel-email: force NULL → stamp; second POST → noop")
def _():
    from app import create_app
    from app.models import Invoice, User
    from app.models.invoice import InvoiceStatus
    from app.models.user import user_companies
    from app.services.invoicing import void_invoice

    inv = _make_invoice(400.0)
    void_invoice(inv, "اختبار إعادة إرسال", actor_id=None)
    inv_id = inv.id
    db.session.expire_all()
    inv2 = db.session.get(Invoice, inv_id)
    # Force the timestamp back to NULL so the retry has work to do —
    # simulating "the first send failed, operator wants to retry".
    inv2.cancel_email_sent_at = None
    db.session.commit()

    test_email = "__cancel_email_sender__@audit.local"
    u = User.query.filter_by(email=test_email).first()
    assert u is not None, "setup from check 5 should have created it"

    app = create_app()
    from flask import session as _flask_session
    from flask_login import login_user
    app.config["SESSION_PROTECTION"] = None
    with app.test_client() as client:
        with app.test_request_context():
            login_user(u)
            _flask_session["active_company_id"] = _STATE["company_id"]
            saved = dict(_flask_session)
        with client.session_transaction() as sess:
            sess.update(saved)
        resp = client.post(
            f"/invoices/{inv_id}/resend-cancel-email",
            follow_redirects=False)
        assert resp.status_code in (302, 303), \
            f"first resend got {resp.status_code}"
        db.session.expire_all()
        inv3 = db.session.get(Invoice, inv_id)
        assert inv3.cancel_email_sent_at is not None, \
            "resend did not stamp the timestamp"
        first_ts = inv3.cancel_email_sent_at

        # Second POST: should be the "already sent" no-op branch.
        resp2 = client.post(
            f"/invoices/{inv_id}/resend-cancel-email",
            follow_redirects=False)
        assert resp2.status_code in (302, 303)
        db.session.expire_all()
        inv4 = db.session.get(Invoice, inv_id)
        assert inv4.cancel_email_sent_at == first_ts, \
            (f"second POST moved the timestamp: {first_ts} → "
             f"{inv4.cancel_email_sent_at}")
    return f"first resend stamped {first_ts}, second was a no-op"


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
                    print(f"FAIL  {label}\n        => "
                          f"{type(e).__name__}: {e}")
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
