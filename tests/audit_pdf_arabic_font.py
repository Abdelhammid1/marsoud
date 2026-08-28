#!/usr/bin/env python3
"""MARSOUD-PDF-P0 (Abdelhamid 2026-08-28) — customer-facing PDFs
must ship in the intended Arabic font.

Design audit finding P0-02 (see the 2026-08-28 report): the three
customer-facing PDF templates — pdfs/invoice.html, pdfs/payslip.html,
party_ledger/print.html — declared `font-family: 'Tajawal', 'Cairo', …`
but neither TTF is on disk. Only Amiri-Regular.ttf + Amiri-Bold.ttf are
present in app/static/fonts/. WeasyPrint (and the Chromium tempfile
render on party_ledger) both silently fell back to whichever Arabic
font the host had, so two identical invoices from two hosts looked
visibly different.

The fix wires Amiri in as a data-URI `@font-face` block, injected by
`app.services.export._amiri_font_face_css()`. This audit is the
regression net for that class of bug — every time a WeasyPrint template
loses its Amiri wiring in future, one of these checks will fail loudly
instead of shipping to a customer.

Checks:
  1. export_invoice_pdf: PDF valid, Amiri embedded in the bytes.
  2. export_payslip_pdf: PDF valid, Amiri embedded in the bytes.
  3. party_ledger/print.html: template renders through WeasyPrint
     with the injected @font-face carrying Amiri into the PDF.

WeasyPrint (and ReportLab, on the legacy fallback path) both emit the
font-family name into the PDF's font-descriptor stream when they
successfully resolve @font-face → so `b"Amiri" in pdf_bytes` is the
tightest cheap assertion available without a full PDF parser. The
existing tests/audit_journal_export_arabic.py uses the same idiom.
"""
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all()
    db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__PDFFONT_%__'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'pdffont-%@x.test'"))


def _seed_company(suffix, want_hr_plan=False):
    """Fresh __PDFFONT_{suffix}__ company + owner + seeded COA. Same shape
    as tests/audit_payroll_multi_month.py:_seed_owner but namespaced so
    teardown can find it by LIKE '__PDFFONT_%__'."""
    from app.models import Company, User, UserStatus, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    plan = None
    if want_hr_plan:
        for candidate in Plan.query.filter_by(is_active=True).all():
            if "hr" in (candidate.modules or []):
                plan = candidate
                break
    if plan is None:
        plan = Plan.query.filter_by(is_active=True).first()
    c = Company(
        name=f"__PDFFONT_{suffix}__", base_currency="EGP",
        subdomain=f"pdffont-{suffix.lower()}",
        subscription_started_at=datetime.utcnow(),
        subscription_expires_at=datetime(2999, 1, 1),
        intended_plan_id=plan.id if plan else None,
        plan_id=plan.id if plan else None,
    )
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(
        email=f"pdffont-{suffix.lower()}@x.test",
        password_hash=generate_password_hash(
            "TestPass123!", method="pbkdf2:sha256"),
        full_name=f"pdffont-{suffix}", is_active=True,
        status=UserStatus.ACTIVE.value,
        email_verified_at=datetime.utcnow(),
        terms_version="TEST",
    )
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()
    return c, u


@check("1. export_invoice_pdf: PDF valid, Amiri embedded")
def _():
    from app.models import Customer, Invoice, InvoiceItem, InvoiceStatus
    from app.services.export import export_invoice_pdf
    _teardown()
    c, _ = _seed_company("INV")
    cust = Customer(
        company_id=c.id, name="عميل الاختبار",
        email="cust@x.test", phone="+201000000000",
    )
    db.session.add(cust); db.session.flush()
    inv = Invoice(
        company_id=c.id, customer_id=cust.id, number="PDFFONT-INV-1",
        issue_date=date.today(), due_date=date.today() + timedelta(days=30),
        currency="EGP", status=InvoiceStatus.SENT,
        subtotal=Decimal("100.00"), tax_rate=Decimal("15"),
        tax_amount=Decimal("15.00"), total=Decimal("115.00"),
    )
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="خدمة اختبار الخط العربي",
        quantity=Decimal("1"), unit_price=Decimal("100.00"),
        line_total=Decimal("100.00"),
    ))
    db.session.commit()
    # Refresh so relationships (invoice.company, invoice.customer,
    # invoice.items) are populated the same way the route sees them.
    db.session.refresh(inv)
    buf = export_invoice_pdf(inv)
    data = buf.read()
    assert data.startswith(b"%PDF"), \
        f"not a PDF (first 8 bytes: {data[:8]!r})"
    assert len(data) > 500, \
        f"PDF suspiciously tiny ({len(data)} bytes)"
    # WeasyPrint embeds the font-family name into the font-descriptor
    # stream when it resolves @font-face; ReportLab's legacy fallback
    # registers Amiri as its Arabic face (see export.py:44). Either
    # path must carry the name into the output — if not, the PDF is
    # rendering in a host fallback font.
    assert b"Amiri" in data, \
        "Amiri font not embedded — the @font-face injection did not " \
        "reach the render, or WeasyPrint failed to resolve the data URI."
    return f"PDF {len(data)} bytes, Amiri embedded"


@check("2. export_payslip_pdf: PDF valid, Amiri embedded")
def _():
    from app.models import Employee, Department
    from app.services.payroll import run_payroll
    from app.services.export import export_payslip_pdf
    _teardown()
    c, _ = _seed_company("PAY", want_hr_plan=True)
    dept = Department(company_id=c.id, name="قسم الاختبار")
    db.session.add(dept); db.session.flush()
    emp = Employee(
        company_id=c.id, department_id=dept.id,
        name="موظف الاختبار", employee_number="EMP-P0-1",
        basic_salary=Decimal("5000.00"), start_date=date.today(),
    )
    db.session.add(emp); db.session.flush()
    db.session.commit()
    today = date.today()
    run = run_payroll(
        company_id=c.id, year=today.year, month=today.month,
        line_inputs={emp.id: {"amount_paid": 0}},
        send_emails=False,
    )
    db.session.commit()
    # Refresh employee so `.department` and `.company` are loaded the
    # way the export helper's template access expects.
    db.session.refresh(emp)
    line = run.lines[0]
    buf = export_payslip_pdf(employee=emp, line=line, run=run)
    data = buf.read()
    assert data.startswith(b"%PDF"), \
        f"not a PDF (first 8 bytes: {data[:8]!r})"
    assert len(data) > 500, \
        f"PDF suspiciously tiny ({len(data)} bytes)"
    assert b"Amiri" in data, \
        "Amiri font not embedded on payslip PDF — check that " \
        "pdfs/payslip.html injects {{ amiri_font_face|safe }} " \
        "BEFORE its own <style> tag."
    return f"PDF {len(data)} bytes, Amiri embedded"


@check("3. party_ledger/print.html: renders via WeasyPrint, Amiri embedded")
def _():
    """The route (routes/party_ledger.py:export_pdf) hands this template
    to headless Chromium in production. For a hermetic smoke test we
    render the same template through WeasyPrint instead — the concern
    the test guards is 'does the template pick up amiri_font_face and
    emit an @font-face block that puts Amiri into the PDF', which is
    engine-independent. If this passes here and the route stops working
    in production, the failure is Playwright/Chromium-side, not
    template-side."""
    from app.services.export import _weasyprint_render
    from app.services.party_ledger import party_ledger
    from app.models import (
        Vendor, VendorBill, VendorBillItem, VendorBillStatus,
        VendorBillPaymentMethod, Account,
    )
    from app.models.vendor_bill import BillLineType
    from app.services.vendor_bills import post_vendor_bill
    from app.services.subsidiary import ensure_vendor_account
    _teardown()
    c, _ = _seed_company("PL")
    vendor = Vendor(company_id=c.id, name="مورّد الاختبار")
    db.session.add(vendor); db.session.flush()
    ensure_vendor_account(vendor)
    rent = Account.query.filter_by(company_id=c.id, code="5220").first()
    bill = VendorBill(
        company_id=c.id, vendor_id=vendor.id, number="PDFFONT-BILL-1",
        issue_date=date.today(), due_date=date.today(),
        status=VendorBillStatus.DRAFT,
        payment_method=VendorBillPaymentMethod.CREDIT,
        tax_rate=Decimal("15"),
    )
    db.session.add(bill); db.session.flush()
    db.session.add(VendorBillItem(
        bill_id=bill.id, line_type=BillLineType.EXPENSE,
        account_id=rent.id, description="بند اختبار",
        quantity=Decimal("1"), unit_price=Decimal("500"),
        line_total=Decimal("500"),
    ))
    db.session.flush()
    post_vendor_bill(bill)
    db.session.commit()
    statement = party_ledger(
        c.id, "vendor", vendor.id,
        start_date=date.today() - timedelta(days=30),
        end_date=date.today() + timedelta(days=1),
    )
    # _weasyprint_render auto-injects amiri_font_face, so no explicit
    # pass here — that's part of what we're testing.
    #
    # Unlike export_invoice_pdf / export_payslip_pdf, _weasyprint_render
    # has NO ReportLab fallback: if libpango isn't installed (typical on
    # a Windows dev box) it raises OSError. We catch that ONE class of
    # environment error and drop to HTML-string inspection, which
    # validates the same concern — that the template picks up
    # `amiri_font_face` and emits an @font-face block carrying Amiri
    # into the render — without needing the C-library stack.
    # On prod (Linux + libpango) the PDF path is exercised as normal.
    try:
        buf = _weasyprint_render(
            "party_ledger/print.html",
            statement=statement, company=c,
            start_date=date.today() - timedelta(days=30),
            end_date=date.today() + timedelta(days=1),
        )
    except OSError as e:
        if "libgobject" in str(e) or "libpango" in str(e) or "libcairo" in str(e):
            # HTML-only fallback path — the party_ledger route passes
            # amiri_font_face explicitly (routes/party_ledger.py:93),
            # so we render the template with the same helper the route
            # would call and assert the injection lands.
            from flask import render_template
            from app.services.export import _amiri_font_face_css
            html = render_template(
                "party_ledger/print.html",
                statement=statement, company=c,
                start_date=date.today() - timedelta(days=30),
                end_date=date.today() + timedelta(days=1),
                amiri_font_face=_amiri_font_face_css(),
            )
            assert "@font-face" in html, \
                "amiri_font_face block never reached the template"
            assert "'Amiri'" in html, \
                "template does not declare font-family: 'Amiri' " \
                "(check line 11 of party_ledger/print.html)"
            assert "data:font/ttf;base64," in html, \
                "font is not embedded as a data: URI — Chromium " \
                "loading from OS temp dir needs the data URI form"
            return "no libpango on host — HTML inspection: " \
                   "@font-face + Amiri + data: URI all present"
        raise
    data = buf.read()
    assert data.startswith(b"%PDF"), \
        f"not a PDF (first 8 bytes: {data[:8]!r})"
    assert len(data) > 500, \
        f"PDF suspiciously tiny ({len(data)} bytes)"
    assert b"Amiri" in data, \
        "Amiri font not embedded on party-ledger PDF — the route " \
        "passes amiri_font_face explicitly and _weasyprint_render " \
        "injects it too, so neither path is reaching the template."
    return f"PDF {len(data)} bytes, Amiri embedded"


def main():
    app = create_app()
    passed = failed = 0
    for label, fn in CHECKS:
        with app.app_context():
            try:
                _teardown()
                res = fn()
                print(f"PASS  {label}  ⇒ {res}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                failed += 1
                import traceback; traceback.print_exc()
    with app.app_context():
        _teardown()
        print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
