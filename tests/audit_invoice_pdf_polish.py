#!/usr/bin/env python3
"""MARSOUD-INVOICE-PDF-POLISH-01 (2026-09-10) — invoice PDF polish:
   1. Payment channels with empty/None values must NOT render "None"
   2. Payment channels with a real value must still render
   3. Installments block renders the Tabby-style timeline (circle
      status + amount pill)
   4. companies.form.html value= attrs no longer emit the string
      "None" — fixed at the root; audit guards against a regression.
"""
import os
import sys
from pathlib import Path
from datetime import datetime, date, timedelta
from decimal import Decimal
from types import SimpleNamespace

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _co(**kw):
    """Fake Company-like object for template rendering. Passes what
    the PDF/view/email templates ask for."""
    d = dict(
        name="شلبي فبرس", display_name="شلبي فبرس",
        official_name=None, brand_name=None,
        commercial_register_no=None, tax_number=None,
        address=None, logo_url=None, logo=None,
        base_currency="EGP",
        bank_name=None, bank_account_holder=None,
        bank_account_number=None, iban=None,
        instapay_handle=None, ewallet_number=None,
        ewallet_provider=None,
    )
    d.update(kw)
    return SimpleNamespace(**d)


def _cust():
    return SimpleNamespace(name="Abdelhamid Shalabi",
                            email="buyer@example.com")


def _installment(seq, amount, days_from_today, status="PENDING"):
    return SimpleNamespace(
        id=seq, sequence_no=seq,
        amount=Decimal(str(amount)),
        due_date=date.today() + timedelta(days=days_from_today),
        status=status, paid_at=None,
    )


def _inv(company, *, installments=(), down=None):
    status_obj = SimpleNamespace(value="SENT")
    inv = SimpleNamespace(
        number="INV-0051",
        currency="EGP",
        issue_date=date.today() - timedelta(days=1),
        due_date=date.today() + timedelta(days=30),
        subtotal=Decimal("1000"),
        total=Decimal("1000"),
        paid_amount=Decimal("0"),
        tax_rate=Decimal("0"),
        tax_amount=Decimal("0"),
        invoice_discount_amount=None,
        down_payment_amount=Decimal(str(down)) if down else None,
        status=status_obj,
        installments=list(installments),
        customer=_cust(),
        company=company,
        internal_notes=None,
    )
    inv.balance = float(inv.total) - float(inv.paid_amount)
    return inv


def _render_pdf(inv, *, paid=0, bal=None, status_color="#059669"):
    """Render pdfs/invoice.html with the same context the WeasyPrint
    exporter uses."""
    from app import create_app
    app = create_app()
    with app.app_context():
        from flask import render_template
        return render_template(
            "pdfs/invoice.html", invoice=inv,
            paid_amt=paid,
            bal=(bal if bal is not None else inv.balance),
            status_key=inv.status.value,
            status_color=status_color,
            amount=lambda x: f"{float(x or 0):.2f}",
            ar_date=lambda d: d.strftime("%Y-%m-%d") if d else "",
        )


@check("1. Empty payment channels never render the string 'None'")
def _():
    inv = _inv(_co())   # every channel is Python None
    html = _render_pdf(inv)
    # The exact string ": None" would only appear if a truthy check
    # let a Python None through and got stringified by Jinja.
    assert "IBAN: None" not in html, "IBAN None leaked"
    assert "InstaPay: None" not in html, "InstaPay None leaked"
    assert "محفظة إلكترونية (None)" not in html, "e-wallet provider None leaked"
    # And the section headers must NOT render for empty tenants.
    assert "بيانات حسابي البنكي" not in html, "bank block leaked"
    return "no 'None' in PDF for a company with empty payment channels"


@check("2. Poisoned-string 'None' values also skip the render "
        "(defense in depth for existing bad rows)")
def _():
    # This simulates the historic bug: the DB has literal strings
    # "None" instead of Python None, because the form template used
    # to spit `value="{{ company.iban if company else '' }}"` and
    # save round-tripped it back in as data.
    poisoned = _co(iban="None", instapay_handle="None",
                    ewallet_number="None", ewallet_provider="None")
    inv = _inv(poisoned)
    html = _render_pdf(inv)
    assert "IBAN: None" not in html, (
        "'None' string leaked into PDF — the _ok macro is broken")
    assert "InstaPay" not in html or ">None<" not in html, (
        "InstaPay block still rendered for the 'None' ghost value")
    return "'None'-string DB rows render as empty"


@check("3. Real payment channel values still render correctly")
def _():
    real = _co(bank_name="بنك مصر",
               bank_account_holder="Marsoud Ltd",
               bank_account_number="1234567890",
               iban="EG380002001234567890",
               instapay_handle="marsoud@instapay",
               ewallet_number="01012345678",
               ewallet_provider="فودافون كاش")
    inv = _inv(real)
    html = _render_pdf(inv)
    assert "بنك مصر" in html
    assert "EG380002001234567890" in html
    assert "marsoud@instapay" in html
    assert "01012345678" in html
    assert "فودافون كاش" in html
    return "populated channels render as expected"


@check("4. Installments PDF block matches the email's Tabby-style — "
        "hollow circle for pending, filled green for paid, red for overdue")
def _():
    installments = [
        _installment(1, 300, -30, status="PAID"),
        _installment(2, 300, 0, status="PENDING"),
        _installment(3, 300, -5, status="OVERDUE"),
    ]
    inv = _inv(_co(), installments=installments)
    html = _render_pdf(inv)
    # Header + timeline shape
    assert "خطة الأقساط" in html
    # Paid row uses green + strike-through
    assert "#10B981" in html, "PAID row missing green colour"
    assert "line-through" in html, "PAID row missing strike-through"
    # Overdue uses red
    assert "#DC2626" in html or "#B91C1C" in html, "OVERDUE colour missing"
    # Circles rendered
    assert "border-radius:50%" in html, "no circle in timeline"
    # Each installment's due date shows up
    for inst in installments:
        assert inst.due_date.strftime('%Y-%m-%d') in html
    return "3 rows w/ status circles + colour coding"


@check("5. Down-payment banner renders inside the installment card "
        "when down_payment_amount > 0")
def _():
    installments = [
        _installment(1, 200, 30),
        _installment(2, 200, 60),
        _installment(3, 200, 90),
    ]
    inv = _inv(_co(), installments=installments, down=400)
    html = _render_pdf(inv)
    assert "دفعة مقدّمة عند الإصدار" in html
    assert "400.00" in html
    return "down-payment green banner renders"


@check("6. Plain invoice with no plan renders no installments block")
def _():
    inv = _inv(_co())
    html = _render_pdf(inv)
    assert "خطة الأقساط" not in html, (
        "installments block leaked on a plain invoice")
    return "plain invoice PDF stays clean"


@check("7. companies/form.html value= attrs are guarded against None "
        "(the ROOT of the bug)")
def _():
    """Guards against a future refactor of the form template that
    would re-introduce the "None" ghost. Reads the source; a plain
    `if company else ''` on any payment-channel field is a
    regression."""
    p = ROOT / "app" / "templates" / "companies" / "form.html"
    txt = p.read_text(encoding="utf-8")
    for field in ("bank_name", "bank_account_holder",
                  "bank_account_number", "iban",
                  "instapay_handle", "ewallet_number",
                  "ewallet_provider"):
        # The safe pattern is `{{ (company.X or '') if company else '' }}`
        good = f"(company.{field} or '')"
        bad_bare = f"company.{field} if company else"
        if good not in txt:
            raise AssertionError(
                f"form.html {field} is not `or ''`-guarded")
        if bad_bare in txt:
            raise AssertionError(
                f"form.html {field} still uses the bare Jinja-None "
                f"pattern")
    return "all 7 channel fields use the `or ''` guard in form.html"


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
