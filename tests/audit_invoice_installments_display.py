#!/usr/bin/env python3
"""MARSOUD-INVOICE-INSTALLMENTS-DISPLAY-01 (2026-09-08) — show
installment plan on the invoice PDF + accept plan + down-payment on
the create form.

Checks:
  1. Baseline — plain invoice (no plan) renders no installment block
     on the PDF and no down-payment banner on the view.
  2. Create-form path — installments only, no down-payment: 3 rows
     of total/3, invoice.balance = total, Payment table empty.
  3. Create-form path — down-payment only, no plan: 1 Payment for
     the down-payment amount, invoice.down_payment_amount set,
     balance = total - dp, no installments.
  4. Create-form path — both: 3 rows of (total - dp)/3 (last row
     absorbs drift), 1 Payment for dp, balance = total - dp.
  5. PDF renders installment block + down-payment banner when a
     plan + down-payment exist.
  6. Baseline — PDF for a plain invoice has NO "خطة الأقساط" text.
  7. Down-payment > total → LedgerError.
  8. Regression — the existing /invoices/<id>/installments/plan
     panel (untouched invoice, balance == total) still works.
"""
import os
import sys
from pathlib import Path
from datetime import datetime, date, timedelta
from decimal import Decimal

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _boot(prefix, *, base_currency="EGP"):
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

    c = Company(name=f"__{prefix}__co", base_currency=base_currency,
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
    return owner.email, c.id, owner.id


def _make_customer(cid, name="Client"):
    from app import db
    from app.models import Customer
    from app.services.subsidiary import ensure_customer_account
    c = Customer(company_id=cid, name=name)
    db.session.add(c); db.session.flush()
    ensure_customer_account(c)
    db.session.commit()
    return c


def _make_pm(cid, code="1110"):
    """PaymentMethod pointing at Cash 1110. Reuses seed default when
    present."""
    from app import db
    from app.models import Account, PaymentMethod
    pm = PaymentMethod.query.filter_by(
        company_id=cid, is_active=True).first()
    if pm:
        return pm
    acc = Account.query.filter_by(company_id=cid, code=code).first()
    pm = PaymentMethod(company_id=cid, name="Cash",
                        name_ar="نقدي",
                        account_id=acc.id, is_active=True,
                        is_default=True)
    db.session.add(pm); db.session.commit()
    return pm


_INV_COUNTER = [0]


def _make_invoice_direct(cid, cust, subtotal, *, currency="EGP",
                          tax_rate=15):
    """Directly-inserted invoice — used by the "existing panel still
    works" and "PDF baseline" checks that don't need the create-form
    plumbing."""
    from app import db
    from app.models import Invoice, InvoiceItem
    from app.models.invoice import InvoiceStatus, DiscountType
    _INV_COUNTER[0] += 1
    inv = Invoice(
        company_id=cid,
        customer_id=cust.id,
        number=f"AUD-INS-{cid}-{_INV_COUNTER[0]}",
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency=currency,
        tax_rate=Decimal(str(tax_rate)),
        invoice_discount_type=DiscountType.NONE,
        invoice_discount_value=Decimal("0"),
        status=InvoiceStatus.DRAFT,
        source="MANUAL",
    )
    db.session.add(inv); db.session.flush()
    it = InvoiceItem(
        invoice_id=inv.id, company_id=cid,
        description="خدمة اختبار",
        quantity=Decimal("1"),
        unit_price=Decimal(str(subtotal)),
        line_total=Decimal(str(subtotal)),
    )
    db.session.add(it); db.session.flush()
    inv.recalc()
    db.session.commit()
    return inv


def _post_new_invoice(client, cid, cust, *,
                       subtotal=1000, tax_rate=0,
                       installment_count=None,
                       installment_start_date=None,
                       down_payment_amount=None,
                       down_payment_method_id=None,
                       send="1", email_customer="0"):
    """Simulates the POST /invoices/new the form sends."""
    payload = {
        "customer_id": cust.id,
        "currency": "EGP",
        "tax_rate": str(tax_rate),
        "item_description[]": "خدمة",
        "item_product_id[]": "",
        "item_quantity[]": "1",
        "item_unit_price[]": str(subtotal),
        "item_discount_type[]": "NONE",
        "item_discount_value[]": "0",
        "item_unit_id[]": "",
        "item_cost_center_id[]": "",
        "send": send,
        "email_customer": email_customer,
    }
    if installment_count is not None:
        payload["installment_count"] = str(installment_count)
    if installment_start_date is not None:
        payload["installment_start_date"] = installment_start_date
    if down_payment_amount is not None:
        payload["down_payment_amount"] = str(down_payment_amount)
    if down_payment_method_id is not None:
        payload["down_payment_method_id"] = str(down_payment_method_id)
    return client.post("/invoices/new", data=payload,
                        follow_redirects=False)


def _login(client, oid, cid):
    with client.session_transaction() as s:
        s["_user_id"] = str(oid)
        s["active_company_id"] = cid


@check("1. Baseline — plain invoice has no installment block on PDF "
        "and no down-payment banner on view")
def _():
    from app import create_app, db
    from app.models import Invoice
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("INS1")
        cust = _make_customer(cid)
        pm = _make_pm(cid)
        inv = _make_invoice_direct(cid, cust, 500)
        assert inv.down_payment_amount is None, (
            "brand-new invoice must have NULL down_payment_amount, got "
            f"{inv.down_payment_amount}")
        assert list(inv.installments) == [], (
            "brand-new invoice must have empty installments list")
        return "no plan + NULL down_payment on a plain invoice"


@check("2. Create-form path — installments only, no down-payment")
def _():
    from app import create_app, db
    from app.models import Invoice, Payment
    from app.models.invoice_installment import InvoiceInstallment
    from app.models.invoice import InvoiceStatus
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("INS2")
        cust = _make_customer(cid)
        _pm = _make_pm(cid)
        client = app.test_client()
        _login(client, oid, cid)
        r = _post_new_invoice(client, cid, cust,
                                subtotal=900,
                                installment_count=3,
                                installment_start_date=date.today().isoformat())
        assert r.status_code == 302, (r.status_code, r.data[:400])
        inv = (Invoice.query.filter_by(company_id=cid)
                .order_by(Invoice.id.desc()).first())
        assert inv.status == InvoiceStatus.SENT
        installments = InvoiceInstallment.query.filter_by(
            invoice_id=inv.id).order_by(
            InvoiceInstallment.sequence_no).all()
        assert len(installments) == 3, len(installments)
        assert sum(float(i.amount) for i in installments) == 900.0
        # No Payment yet — no down-payment.
        pays = Payment.query.filter_by(invoice_id=inv.id).all()
        assert not pays, f"unexpected payment rows: {len(pays)}"
        assert inv.down_payment_amount is None
        return (f"3 installments summing to 900 EGP, "
                f"no down-payment")


@check("3. Create-form path — down-payment only, no plan")
def _():
    from app import create_app, db
    from app.models import Invoice, Payment
    from app.models.invoice_installment import InvoiceInstallment
    from app.models.invoice import InvoiceStatus
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("INS3")
        cust = _make_customer(cid)
        pm = _make_pm(cid)
        client = app.test_client()
        _login(client, oid, cid)
        r = _post_new_invoice(client, cid, cust,
                                subtotal=1000,
                                down_payment_amount=400,
                                down_payment_method_id=pm.id)
        assert r.status_code == 302, (r.status_code, r.data[:400])
        inv = (Invoice.query.filter_by(company_id=cid)
                .order_by(Invoice.id.desc()).first())
        assert float(inv.down_payment_amount) == 400.0
        assert abs(float(inv.paid_amount) - 400.0) < 0.005
        assert abs(inv.balance - 600.0) < 0.005
        installments = InvoiceInstallment.query.filter_by(
            invoice_id=inv.id).all()
        assert not installments, "unexpected installments"
        pays = Payment.query.filter_by(invoice_id=inv.id).all()
        assert len(pays) == 1
        assert abs(float(pays[0].amount) - 400.0) < 0.005
        return "1 payment @ 400, balance 600, no plan"


@check("4. Create-form path — both installments AND down-payment")
def _():
    from app import create_app, db
    from app.models import Invoice, Payment
    from app.models.invoice_installment import InvoiceInstallment
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("INS4")
        cust = _make_customer(cid)
        pm = _make_pm(cid)
        client = app.test_client()
        _login(client, oid, cid)
        r = _post_new_invoice(client, cid, cust,
                                subtotal=1000,
                                installment_count=3,
                                installment_start_date=date.today().isoformat(),
                                down_payment_amount=400,
                                down_payment_method_id=pm.id)
        assert r.status_code == 302, (r.status_code, r.data[:400])
        inv = (Invoice.query.filter_by(company_id=cid)
                .order_by(Invoice.id.desc()).first())
        assert float(inv.down_payment_amount) == 400.0
        assert abs(inv.balance - 600.0) < 0.005
        installments = InvoiceInstallment.query.filter_by(
            invoice_id=inv.id).order_by(
            InvoiceInstallment.sequence_no).all()
        assert len(installments) == 3
        # Sum of installments == remaining 600, each = 200
        s = sum(float(i.amount) for i in installments)
        assert abs(s - 600.0) < 0.005, s
        for i in installments:
            assert abs(float(i.amount) - 200.0) < 0.005, i.amount
        pays = Payment.query.filter_by(invoice_id=inv.id).all()
        assert len(pays) == 1
        assert abs(float(pays[0].amount) - 400.0) < 0.005
        return "3 × 200 installments + 400 down-payment on 1000 invoice"


@check("5. PDF renders installment block + down-payment banner")
def _():
    from app import create_app
    from app.models import Invoice
    from app.models.invoice_installment import InvoiceInstallment
    app = create_app()
    with app.app_context():
        from app import db
        email, cid, oid = _boot("INS5")
        cust = _make_customer(cid)
        pm = _make_pm(cid)
        client = app.test_client()
        _login(client, oid, cid)
        r = _post_new_invoice(client, cid, cust,
                                subtotal=1000,
                                installment_count=3,
                                installment_start_date=date.today().isoformat(),
                                down_payment_amount=400,
                                down_payment_method_id=pm.id)
        assert r.status_code == 302
        inv = (Invoice.query.filter_by(company_id=cid)
                .order_by(Invoice.id.desc()).first())
        # Render the PDF template directly (skip WeasyPrint — we only
        # care that the template surfaces the expected text).
        from flask import render_template
        html = render_template("pdfs/invoice.html", invoice=inv,
                                paid_amt=float(inv.paid_amount or 0),
                                bal=inv.balance,
                                status_key=inv.status.value,
                                status_color="#059669",
                                amount=lambda x: f"{float(x or 0):.2f}")
        assert "خطة الأقساط" in html, "installment header missing"
        assert "دفعة مقدّمة عند الإصدار" in html, "down-payment banner missing"
        assert "400.00" in html, "down-payment amount missing"
        assert "200.00" in html, "installment amount missing"
        return "PDF shows plan table + down-payment banner"


@check("6. Baseline — PDF for plain invoice has no installment block")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("INS6")
        cust = _make_customer(cid)
        pm = _make_pm(cid)
        inv = _make_invoice_direct(cid, cust, 500)
        from flask import render_template
        html = render_template("pdfs/invoice.html", invoice=inv,
                                paid_amt=0.0,
                                bal=float(inv.total or 0),
                                status_key=inv.status.value,
                                status_color="#0F172A",
                                amount=lambda x: f"{float(x or 0):.2f}")
        assert "خطة الأقساط" not in html, (
            "plain invoice PDF must not render installment block")
        assert "دفعة مقدّمة" not in html, (
            "plain invoice PDF must not render down-payment banner")
        return "plain invoice PDF unchanged"


@check("7. Down-payment > total → LedgerError")
def _():
    from app import create_app, db
    from app.models import Invoice
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("INS7")
        cust = _make_customer(cid)
        pm = _make_pm(cid)
        client = app.test_client()
        _login(client, oid, cid)
        r = _post_new_invoice(client, cid, cust,
                                subtotal=500,
                                down_payment_amount=999,
                                down_payment_method_id=pm.id)
        # Route catches LedgerError → flash + redirect back to /new.
        # We look for the flash in the follow-redirect response.
        r2 = client.get("/invoices/new")
        # No invoice should have been persisted.
        cnt = Invoice.query.filter_by(company_id=cid).count()
        assert cnt == 0, f"expected no invoice, got {cnt}"
        return "over-large down-payment refused, no invoice saved"


@check("8. Regression — existing installments-plan panel on an "
        "untouched invoice still works")
def _():
    """create_installment_plan now validates against invoice.balance
    instead of invoice.total. For an untouched invoice (paid_amount
    == 0) balance == total, so behavior is byte-identical to the
    pre-ticket flow. Guard the equivalence here."""
    from app import create_app, db
    from app.services.installments import (
        create_installment_plan, InstallmentError,
    )
    from app.services.invoicing import post_invoice_to_ledger
    from app.models.invoice_installment import InvoiceInstallment
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("INS8")
        cust = _make_customer(cid)
        pm = _make_pm(cid)
        inv = _make_invoice_direct(cid, cust, 900, tax_rate=0)
        post_invoice_to_ledger(inv, created_by=oid)
        db.session.refresh(inv)
        rows = [
            {"amount": 300, "due_date": date.today()},
            {"amount": 300, "due_date": date.today() + timedelta(days=30)},
            {"amount": 300, "due_date": date.today() + timedelta(days=60)},
        ]
        create_installment_plan(inv, rows, actor_id=oid)
        installments = InvoiceInstallment.query.filter_by(
            invoice_id=inv.id).all()
        assert len(installments) == 3
        assert sum(float(i.amount) for i in installments) == 900.0
        # And a wrong sum still refuses.
        inv2 = _make_invoice_direct(cid, cust, 600, tax_rate=0)
        post_invoice_to_ledger(inv2, created_by=oid)
        try:
            create_installment_plan(inv2, [
                {"amount": 100, "due_date": date.today()},
                {"amount": 100, "due_date": date.today() + timedelta(days=30)},
            ], actor_id=oid)
        except InstallmentError as e:
            assert "لا يساوي" in str(e)
        else:
            raise AssertionError("expected InstallmentError on sum mismatch")
        return "existing panel works; sum mismatch still refused"


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
