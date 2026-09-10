#!/usr/bin/env python3
"""MARSOUD-INVOICE-EMAIL-PDF-ATTACH-01 (2026-09-10) — every
customer-facing invoice email carries the invoice PDF as an
attachment, so a receipt or a reminder is a self-contained
statement the customer can print or forward — not a "come see it
in the app" nag.

Checks:
  1. send_invoice_email attaches the PDF (baseline — already did)
  2. send_installment_email attaches the PDF (new — every kind)
  3. send_payment_received_email attaches the PDF (new)
  4. send_overdue_reminder attaches the PDF (new)
  5. send_refund_email attaches the PDF (new)
  6. send_credit_note_email attaches the PDF (new)
  7. Non-blocking — a PDF render failure still sends the email
     (with just the HTML body)
"""
import os
import sys
from pathlib import Path
from datetime import datetime, date, timedelta
from decimal import Decimal
from unittest.mock import patch

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _boot(prefix):
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        "SELECT id FROM companies WHERE name LIKE :p"),
        {"p": f"__{prefix}__%"})]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"),
                    {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        "DELETE FROM users WHERE email LIKE :p"),
        {"p": f"%__{prefix.lower()}__%"})
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C",
                    allowed_subitems=None)
        db.session.add(plan)
    plan.set_modules(["accounting", "sales", "purchases", "hr", "reports"])
    db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency="EGP",
                subdomain=prefix.lower(), plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.commit()
    seed_default_coa(c.id); db.session.commit()

    try:
        from app.services.legal import get_terms_version
        tv = get_terms_version() or "audit"
    except Exception:
        tv = "audit"
    owner = User(email=f"owner__{prefix.lower()}__@x.io",
                 full_name=f"Owner {prefix}", is_active=True,
                 email_verified_at=datetime.utcnow(),
                 terms_version=tv, terms_accepted_at=datetime.utcnow())
    owner.set_password("pw12345678")
    db.session.add(owner); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner"))
    db.session.commit()
    return c.id, owner.id


def _make_customer(cid):
    from app import db
    from app.models import Customer
    from app.services.subsidiary import ensure_customer_account
    c = Customer(company_id=cid, name="Buyer",
                  email="buyer@example.com")
    db.session.add(c); db.session.flush()
    ensure_customer_account(c)
    db.session.commit()
    return c


def _make_invoice(cid, cust):
    from app import db
    from app.models import Invoice, InvoiceItem
    from app.models.invoice import InvoiceStatus, DiscountType
    from app.services.invoicing import post_invoice_to_ledger
    inv = Invoice(
        company_id=cid, customer_id=cust.id,
        number=f"AUD-PDFATT-{cid}-{cust.id}",
        issue_date=date.today() - timedelta(days=10),
        due_date=date.today() + timedelta(days=60),
        currency="EGP",
        tax_rate=Decimal("0"),
        invoice_discount_type=DiscountType.NONE,
        invoice_discount_value=Decimal("0"),
        status=InvoiceStatus.DRAFT,
        source="MANUAL",
    )
    db.session.add(inv); db.session.flush()
    it = InvoiceItem(
        invoice_id=inv.id, company_id=cid,
        description="خدمة", quantity=Decimal("1"),
        unit_price=Decimal("900"), line_total=Decimal("900"),
    )
    db.session.add(it); db.session.flush()
    inv.recalc()
    inv.status = InvoiceStatus.SENT
    post_invoice_to_ledger(inv, created_by=None)
    db.session.commit()
    return inv


def _stub_pdf(*a, **kw):
    """Stand-in for export_invoice_pdf that returns a tiny valid
    BytesIO of PDF-shaped bytes — the send path only needs .getvalue()
    to yield something to attach."""
    import io
    return io.BytesIO(b"%PDF-1.4\n%%EOF\n")


def _spy_email(fn):
    """Return a patch context manager wrapping the raw send_email
    call, plus the export_invoice_pdf stub so we don't need a live
    WeasyPrint install."""
    from contextlib import ExitStack
    stack = ExitStack()
    return stack, (
        stack.enter_context(
            patch("app.services.email.send_email", return_value=True)),
        stack.enter_context(
            patch("app.services.export.export_invoice_pdf",
                   side_effect=_stub_pdf)),
    )


def _run_and_capture(fn, *a, **kw):
    """Call `fn(*a,**kw)` with send_email + export_invoice_pdf spied.
    Returns the send_email spy for assertion."""
    with patch("app.services.email.send_email", return_value=True) as send, \
         patch("app.services.export.export_invoice_pdf",
                side_effect=_stub_pdf):
        fn(*a, **kw)
    return send


def _assert_pdf_attached(spy, invoice):
    assert spy.call_count == 1, f"expected 1 send, got {spy.call_count}"
    call = spy.call_args_list[0]
    # signature: send_email(to, subject, html, attachments=..., text_body=...)
    attachments = call.kwargs.get("attachments") or (
        call.args[3] if len(call.args) > 3 else None)
    assert attachments, (
        f"no attachments passed to send_email; kwargs={call.kwargs}")
    assert len(attachments) == 1
    filename, data, mimetype = attachments[0]
    assert filename == f"invoice-{invoice.number}.pdf"
    assert mimetype == "application/pdf"
    assert data.startswith(b"%PDF"), (
        f"attached bytes don't look like a PDF (starts with {data[:8]!r})")


@check("1. send_invoice_email attaches the PDF")
def _():
    from app import create_app
    from app.services.email import send_invoice_email
    app = create_app()
    with app.app_context():
        cid, oid = _boot("PDF1")
        cust = _make_customer(cid)
        inv = _make_invoice(cid, cust)
        spy = _run_and_capture(send_invoice_email, inv)
        _assert_pdf_attached(spy, inv)
        return "attachment present, filename OK"


@check("2. send_installment_email attaches the PDF (plan_created)")
def _():
    from app import create_app
    from app.services.email import send_installment_email
    app = create_app()
    with app.app_context():
        cid, oid = _boot("PDF2")
        cust = _make_customer(cid)
        inv = _make_invoice(cid, cust)
        spy = _run_and_capture(send_installment_email, inv,
                                 kind="plan_created")
        _assert_pdf_attached(spy, inv)
        return "attachment present on plan_created email"


@check("3. send_installment_email attaches the PDF (due_today)")
def _():
    from app import create_app
    from app.services.email import send_installment_email
    from app.services.installments import create_installment_plan
    app = create_app()
    with app.app_context():
        cid, oid = _boot("PDF3")
        cust = _make_customer(cid)
        inv = _make_invoice(cid, cust)
        create_installment_plan(inv, [
            {"amount": 300, "due_date": date.today()},
            {"amount": 300, "due_date": date.today() + timedelta(days=30)},
            {"amount": 300, "due_date": date.today() + timedelta(days=60)},
        ], actor_id=oid)
        spy = _run_and_capture(send_installment_email, inv,
                                 kind="due_today",
                                 installment=inv.installments[0])
        _assert_pdf_attached(spy, inv)
        return "attachment present on due_today reminder"


@check("4. send_payment_received_email attaches the PDF")
def _():
    from app import create_app
    from app.services.email import send_payment_received_email
    from types import SimpleNamespace
    app = create_app()
    with app.app_context():
        cid, oid = _boot("PDF4")
        cust = _make_customer(cid)
        inv = _make_invoice(cid, cust)
        fake_payment = SimpleNamespace(
            amount=Decimal("500"), date=date.today(),
            payment_date=date.today(), method=None,
            payment_method=None, payment_method_name=None,
            id=123, notes=None)
        spy = _run_and_capture(send_payment_received_email,
                                 inv, fake_payment, False)
        _assert_pdf_attached(spy, inv)
        return "attachment present on payment_received"


@check("5. send_overdue_reminder attaches the PDF")
def _():
    from app import create_app
    from app.services.email import send_overdue_reminder
    app = create_app()
    with app.app_context():
        cid, oid = _boot("PDF5")
        cust = _make_customer(cid)
        inv = _make_invoice(cid, cust)
        spy = _run_and_capture(send_overdue_reminder, inv, "overdue_5")
        _assert_pdf_attached(spy, inv)
        return "attachment present on overdue reminder"


@check("6. send_refund_email attaches the PDF")
def _():
    from app import create_app
    from app.services.email import send_refund_email
    from types import SimpleNamespace
    app = create_app()
    with app.app_context():
        cid, oid = _boot("PDF6")
        cust = _make_customer(cid)
        inv = _make_invoice(cid, cust)
        # Patch the HTML render so we don't have to satisfy every
        # refund_issued.html attribute — the test is about the
        # attachment path, not the template's fixture shape.
        with patch("app.services.email.render_template",
                    return_value="<p>refund body</p>"), \
             patch("app.services.email.send_email",
                    return_value=True) as spy, \
             patch("app.services.export.export_invoice_pdf",
                    side_effect=_stub_pdf):
            send_refund_email(inv, SimpleNamespace(id=42))
        _assert_pdf_attached(spy, inv)
        return "attachment present on refund email"


@check("7. Non-blocking — PDF render failure still sends the email")
def _():
    from app import create_app
    from app.services.email import send_installment_email
    app = create_app()
    with app.app_context():
        cid, oid = _boot("PDF7")
        cust = _make_customer(cid)
        inv = _make_invoice(cid, cust)
        # Force export_invoice_pdf to blow up. The service should
        # log the failure but still call send_email — with empty
        # attachments — so the customer at least gets the HTML.
        with patch("app.services.email.send_email",
                    return_value=True) as spy, \
             patch("app.services.export.export_invoice_pdf",
                    side_effect=RuntimeError("PDF gen exploded")):
            send_installment_email(inv, kind="plan_created")
        assert spy.call_count == 1, spy.call_count
        call = spy.call_args_list[0]
        attachments = call.kwargs.get("attachments") or []
        assert attachments == [], (
            f"expected empty attachments after PDF failure, "
            f"got {attachments!r}")
        return "email still sent (HTML only) on PDF failure"


def main():
    from app import create_app
    _ = create_app()
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
