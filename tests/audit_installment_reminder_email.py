#!/usr/bin/env python3
"""MARSOUD-INVOICE-INSTALLMENTS-DISPLAY-01 follow-up (2026-09-08) —
smoke test that the existing installment-reminder pipeline still
fires an email on the day the first installment's due_date arrives.

This isn't a new feature — the reminder cron was shipped in
MARSOUD-INSTALLMENT-PLAN-01 (2026-07-25). But the accountant asked
"when the first installment time comes send an email", so we prove
here that the pipeline still works end-to-end after the
create-form + PDF changes.

Checks:
  1. Reminder fires on the exact day the first installment is due
     (send_email called with the right subject).
  2. Reminder is idempotent — a second cron run on the same day
     does NOT re-send (guarded by InstallmentReminderSent).
  3. Nothing fires for a PAID installment.
  4. Nothing fires when the customer has no email address.
  5. Nothing fires when Invoice.send_reminders is False (opt-out).
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


def _make_customer_with_email(cid, email="buyer@example.com"):
    from app import db
    from app.models import Customer
    from app.services.subsidiary import ensure_customer_account
    c = Customer(company_id=cid, name="Buyer", email=email)
    db.session.add(c); db.session.flush()
    ensure_customer_account(c)
    db.session.commit()
    return c


def _make_invoice_with_installment_due_today(cid, cust,
                                                *, send_reminders=True):
    """Set up an invoice + 3-installment plan where installment #1 is
    due today, so the reminder pipeline fires immediately."""
    from app import db
    from app.models import Invoice, InvoiceItem
    from app.models.invoice import InvoiceStatus, DiscountType
    from app.services.invoicing import post_invoice_to_ledger
    from app.services.installments import create_installment_plan
    inv = Invoice(
        company_id=cid, customer_id=cust.id,
        number=f"AUD-REM-{cid}-{cust.id}",
        issue_date=date.today() - timedelta(days=10),
        due_date=date.today() + timedelta(days=60),
        currency="EGP",
        tax_rate=Decimal("0"),
        invoice_discount_type=DiscountType.NONE,
        invoice_discount_value=Decimal("0"),
        status=InvoiceStatus.DRAFT,
        source="MANUAL",
        send_reminders=send_reminders,
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
    # Plan: 3 × 300, first row due TODAY.
    create_installment_plan(inv, [
        {"amount": 300, "due_date": date.today()},
        {"amount": 300, "due_date": date.today() + timedelta(days=30)},
        {"amount": 300, "due_date": date.today() + timedelta(days=60)},
    ], actor_id=None)
    db.session.commit()
    return inv


@check("1. Reminder fires the day the first installment is due")
def _():
    from app import create_app
    from app.services.reminders import process_installment_reminders
    app = create_app()
    with app.app_context():
        cid, oid = _boot("REM1")
        cust = _make_customer_with_email(cid)
        inv = _make_invoice_with_installment_due_today(cid, cust)
        # Intercept the actual mail-send at the boundary — we care
        # that ONE call goes out, with the right subject, to the
        # customer's email.
        with patch("app.services.email.send_email",
                    return_value=True) as spy:
            counts = process_installment_reminders()
        assert spy.call_count >= 1, counts
        mine = [c for c in spy.call_args_list
                 if c.args[0] == cust.email and inv.number in c.args[1]]
        assert len(mine) == 1, (
            f"expected exactly 1 email for {inv.number} → {cust.email}, "
            f"got {len(mine)} among {spy.call_count} calls")
        to, subject, _html = mine[0].args
        assert "القسط" in subject
        # MARSOUD-INVOICE-INSTALLMENTS-DISPLAY-01 follow-up — day-of-due
        # wording should be "مستحق اليوم", not "تجاوز موعد الاستحقاق"
        # (the latter implies past due, which is inaccurate at t=0).
        assert "مستحق اليوم" in subject, subject
        return f"1 email fired: subject «{subject[:60]}…» → {to}"


@check("2. Idempotent — second run on same day does NOT re-send")
def _():
    from app import create_app
    from app.services.reminders import process_installment_reminders
    app = create_app()
    with app.app_context():
        cid, oid = _boot("REM2")
        cust = _make_customer_with_email(cid)
        inv = _make_invoice_with_installment_due_today(cid, cust)
        with patch("app.services.email.send_email",
                    return_value=True) as spy:
            process_installment_reminders()
            first_tick_calls = spy.call_count
            process_installment_reminders()   # second tick, same day
            process_installment_reminders()   # third tick, same day
        # The 2nd + 3rd ticks must not add ANY new emails — every
        # candidate had its row written by tick 1, guarded by the
        # InstallmentReminderSent unique index.
        assert spy.call_count == first_tick_calls, (
            f"cron re-ran; ticks 2/3 sent "
            f"{spy.call_count - first_tick_calls} extra emails")
        # And OUR invoice fired exactly once during the whole window.
        mine = [c for c in spy.call_args_list
                 if c.args[0] == cust.email and inv.number in c.args[1]]
        assert len(mine) == 1
        return "3 cron ticks → still exactly 1 email for our invoice"


@check("3. No email for a PAID installment")
def _():
    from app import create_app, db
    from app.services.reminders import process_installment_reminders
    from app.models.invoice_installment import (
        InvoiceInstallment, INSTALLMENT_PAID,
    )
    app = create_app()
    with app.app_context():
        cid, oid = _boot("REM3")
        cust = _make_customer_with_email(cid)
        inv = _make_invoice_with_installment_due_today(cid, cust)
        # Mark the first installment PAID before the cron runs.
        first = (InvoiceInstallment.query
                  .filter_by(invoice_id=inv.id, sequence_no=1).first())
        first.status = INSTALLMENT_PAID
        db.session.commit()
        with patch("app.services.email.send_email",
                    return_value=True) as spy:
            process_installment_reminders()
        # None of the other two installments are due today, so 0.
        assert spy.call_count == 0, spy.call_args_list
        return "PAID installment skipped; 0 emails sent"


@check("4. No email when customer has no email address")
def _():
    from app import create_app
    from app.services.reminders import process_installment_reminders
    app = create_app()
    with app.app_context():
        cid, oid = _boot("REM4")
        # No email on the customer.
        cust = _make_customer_with_email(cid, email=None)
        _make_invoice_with_installment_due_today(cid, cust)
        with patch("app.services.email.send_email",
                    return_value=True) as spy:
            process_installment_reminders()
        assert spy.call_count == 0, spy.call_args_list
        return "customer without email → 0 emails sent"


@check("5. No email when invoice.send_reminders=False (opt-out)")
def _():
    from app import create_app
    from app.services.reminders import process_installment_reminders
    app = create_app()
    with app.app_context():
        cid, oid = _boot("REM5")
        cust = _make_customer_with_email(cid)
        _make_invoice_with_installment_due_today(
            cid, cust, send_reminders=False)
        with patch("app.services.email.send_email",
                    return_value=True) as spy:
            process_installment_reminders()
        assert spy.call_count == 0, spy.call_args_list
        return "opted-out invoice → 0 emails sent"


@check("6. plan_created email — fires the moment /invoices/new "
        "ships a plan; body includes the timeline")
def _():
    """MARSOUD-INVOICE-INSTALLMENT-EMAILS-01 — the "invoice created +
    here's the schedule" email the accountant asked for."""
    from app import create_app
    from app.services.email import send_installment_email
    from app.models import Invoice
    from app.models.invoice_installment import InvoiceInstallment
    app = create_app()
    with app.app_context():
        cid, oid = _boot("REM6")
        cust = _make_customer_with_email(cid)
        inv = _make_invoice_with_installment_due_today(cid, cust)
        with patch("app.services.email.send_email",
                    return_value=True) as spy:
            send_installment_email(inv, kind="plan_created")
        assert spy.call_count == 1
        to, subject, html = spy.call_args_list[0].args
        assert to == cust.email
        assert "تم إنشاء الفاتورة" in subject
        assert inv.number in subject
        # Body must carry each installment's due date + amount.
        assert "خطة الأقساط" in html
        installments = InvoiceInstallment.query.filter_by(
            invoice_id=inv.id).all()
        for inst in installments:
            assert inst.due_date.strftime('%Y-%m-%d') in html, (
                f"missing due date {inst.due_date}")
        return f"1 plan_created email, subject «{subject[:60]}…»"


@check("7. payment_received email — fires after a pay_installment "
        "call; highlights the NEXT pending row, not the just-paid one")
def _():
    from app import create_app
    from app.services.installments import pay_installment
    from app.models import PaymentMethod
    from app.models.invoice_installment import InvoiceInstallment
    app = create_app()
    with app.app_context():
        cid, oid = _boot("REM7")
        cust = _make_customer_with_email(cid)
        inv = _make_invoice_with_installment_due_today(cid, cust)
        from app import db
        pm = PaymentMethod.query.filter_by(
            company_id=cid, is_active=True).first()
        first_inst = (InvoiceInstallment.query
                       .filter_by(invoice_id=inv.id, sequence_no=1)
                       .first())
        with patch("app.services.email.send_email",
                    return_value=True) as spy:
            pay_installment(first_inst, payment_method=pm,
                             actor_id=oid)
        assert spy.call_count == 1, spy.call_args_list
        to, subject, html = spy.call_args_list[0].args
        assert to == cust.email
        assert "شكراً" in subject or "شكرا" in subject
        # The just-paid row (#1, 300) is line-through in the timeline.
        # The next pending row (#2, 300) is the highlighted CURRENT
        # row — its due date is bold. Just guard the paid row shows
        # struck-through so the customer sees the visual close-out.
        assert "line-through" in html, "paid row must show strikethrough"
        return f"1 payment_received email, subject «{subject[:60]}…»"


@check("8. Reminder templates use the Tabby-style timeline — every "
        "installment row appears in the email body")
def _():
    from app import create_app
    from app.services.reminders import process_installment_reminders
    from app.models.invoice_installment import InvoiceInstallment
    app = create_app()
    with app.app_context():
        cid, oid = _boot("REM8")
        cust = _make_customer_with_email(cid)
        inv = _make_invoice_with_installment_due_today(cid, cust)
        with patch("app.services.email.send_email",
                    return_value=True) as spy:
            process_installment_reminders()
        # Prior checks may have left other due-today installments in
        # the DB (each with its own PENDING first row); the cron
        # fires one email per due candidate, so match by invoice
        # number instead of insisting on a call count of 1.
        assert spy.call_count >= 1, spy.call_args_list
        target_html = None
        for call in spy.call_args_list:
            _to, subject, html = call.args
            if inv.number in subject:
                target_html = html
                break
        assert target_html is not None, (
            f"no reminder email for {inv.number} "
            f"among {spy.call_count} calls")
        installments = InvoiceInstallment.query.filter_by(
            invoice_id=inv.id).all()
        for inst in installments:
            assert inst.due_date.strftime('%Y-%m-%d') in target_html
        assert "خطة الأقساط" in target_html
        return "reminder body renders the full timeline"


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
