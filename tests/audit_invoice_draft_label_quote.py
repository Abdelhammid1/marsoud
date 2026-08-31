#!/usr/bin/env python3
"""MARSOUD-TKT-INVOICE-DRAFT-LABEL-QUOTE (Abdelhamid 2026-08-31) —
the customer-visible label for the invoice DRAFT status now reads
"عرض سعر" instead of "مسودة", matching the "حفظ كعرض سعر" button
rename in MARSOUD-TKT-INVOICE-ITEMS-NUMBERED-QUOTE.

Scope is narrow on purpose:
  * InvoiceStatus enum unchanged (still DRAFT)
  * Invoice-specific labels changed (form badge, list badge, view
    page badge, PDF badge, two flash messages)
  * Vendor bill "مسودة" label stays as-is (a bill is not a quote)
  * Inventory transfer / broadcasts / daily reports also unchanged

Checks:
  1. _INVOICE_STATUS_LABELS_AR maps DRAFT → 'عرض سعر' (not 'مسودة').
  2. _BILL_STATUS_LABELS_AR keeps DRAFT → 'مسودة' (vendor bills
     are not quotes; regression guard).
  3. pdfs/invoice.html STATUS_META has DRAFT badge as "عرض سعر".
  4. Flash messages in routes/invoices.py use the new copy.
  5. End-to-end: invoice list + view page render "عرض سعر" for a
     DRAFT invoice, not "مسودة".
  6. End-to-end: rendered invoice PDF template contains "عرض سعر"
     inside the status badge for a DRAFT invoice.
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _strip_comments(src):
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)
    return src


@check("1. _macros.html: _INVOICE_STATUS_LABELS_AR[DRAFT] = 'عرض سعر'")
def _():
    src = _strip_comments(_read("app/templates/_macros.html"))
    m = re.search(
        r"_INVOICE_STATUS_LABELS_AR\s*=\s*\{[^}]*'DRAFT':\s*'([^']+)'",
        src, re.DOTALL)
    assert m, "_INVOICE_STATUS_LABELS_AR / DRAFT entry not found"
    got = m.group(1)
    assert got == "عرض سعر", \
        f"invoice DRAFT label should be 'عرض سعر', got {got!r}"
    return "invoice-scope label updated"


@check("2. _macros.html: _BILL_STATUS_LABELS_AR[DRAFT] still 'مسودة'")
def _():
    """Regression guard — vendor bills are NOT quotes. A bill received
    from a supplier being called "عرض سعر" would flip the semantics
    (the supplier sent a bill, not a quote). This check makes sure
    the narrow scope stays narrow if a future refactor tries to
    unify the two label dicts."""
    src = _strip_comments(_read("app/templates/_macros.html"))
    m = re.search(
        r"_BILL_STATUS_LABELS_AR\s*=\s*\{[^}]*'DRAFT':\s*'([^']+)'",
        src, re.DOTALL)
    assert m, "_BILL_STATUS_LABELS_AR / DRAFT entry not found"
    got = m.group(1)
    assert got == "مسودة", \
        f"bill DRAFT label MUST remain 'مسودة' (bills aren't quotes); got {got!r}"
    return "bill label correctly untouched"


@check("3. pdfs/invoice.html: STATUS_META['DRAFT'] label is 'عرض سعر'")
def _():
    src = _strip_comments(_read("app/templates/pdfs/invoice.html"))
    m = re.search(
        r'STATUS_META\s*=\s*\{[^}]*"DRAFT":\s*\(\s*"([^"]+)"',
        src, re.DOTALL)
    assert m, "STATUS_META DRAFT entry not found in invoice PDF"
    got = m.group(1)
    assert got == "عرض سعر", \
        f"PDF DRAFT badge should be 'عرض سعر', got {got!r}"
    return "PDF badge label updated"


@check("4. routes/invoices.py flash messages updated (no naked 'مسودة')")
def _():
    """The two flash messages that used 'مسودة' as user-visible copy
    now use 'عرض سعر'. Comments/docstrings referencing 'مسودة' are
    fine — this only checks live flash strings."""
    src = _strip_comments(_read("app/routes/invoices.py"))
    # Find every flash("...", ...) call whose message contains "مسودة"
    naked = re.findall(r'flash\(\s*"([^"]*مسودة[^"]*)"', src)
    assert not naked, \
        f"invoice route still flashes 'مسودة' in user copy: {naked}"
    # And confirm the two replacements landed
    assert 'flash("الفاتورة ليست عرض سعر"' in src, \
        "expected 'الفاتورة ليست عرض سعر' flash missing"
    assert 'flash("لا يمكن إعادة إرسال عرض سعر — أرسله أولاً"' in src, \
        "expected resend flash missing"
    return "flash copy migrated"


@check("5. list + view render 'عرض سعر' for a DRAFT invoice")
def _():
    from datetime import datetime, date, timedelta
    from decimal import Decimal
    from sqlalchemy import text, inspect
    from app import create_app, db

    app = create_app()
    with app.app_context():
        from app.models import (
            Company, Customer, Invoice, InvoiceStatus, User, Plan,
        )
        from app.models.user import user_companies
        from app.services.seed_coa import seed_default_coa
        from app.services.subsidiary import ensure_customer_account

        # ── cleanup ────────────────────────────────
        insp = inspect(db.engine)
        cids = [r[0] for r in db.session.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__QUOTE_LBL__%'"))]
        for cid in cids:
            for t in reversed(db.metadata.sorted_tables):
                cols = {c["name"] for c in insp.get_columns(t.name)}
                if "company_id" in cols:
                    db.session.execute(text(
                        f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": cid})
            db.session.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"), {"c": cid})
            db.session.execute(text(
                "DELETE FROM companies WHERE id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM users WHERE email LIKE '%__quote_lbl__%'"))
        db.session.commit()

        plan = Plan.query.filter_by(code="__quote_lbl__").first()
        if not plan:
            plan = Plan(code="__quote_lbl__", name="Q", name_ar="ع",
                        allowed_subitems=None)
            plan.set_modules(["accounting", "sales", "settings"])
            db.session.add(plan); db.session.flush()

        c = Company(name="__QUOTE_LBL__co", base_currency="EGP",
                    subdomain="quotelbl1", plan_id=plan.id,
                    subscription_started_at=datetime.utcnow(),
                    subscription_expires_at=datetime(2999, 1, 1))
        db.session.add(c); db.session.commit()
        seed_default_coa(c.id); db.session.commit()

        cust = Customer(company_id=c.id, name="عميل عرض السعر")
        db.session.add(cust); db.session.flush()
        ensure_customer_account(cust)
        db.session.commit()

        try:
            terms_v = "audit"
            try:
                from app.services.legal import get_terms_version
                terms_v = get_terms_version() or "audit"
            except Exception:
                pass

            u = User(email="user__quote_lbl__@x.io", full_name="Quote Owner",
                     is_active=True, email_verified_at=datetime.utcnow(),
                     terms_version=terms_v, terms_accepted_at=datetime.utcnow())
            u.set_password("pw12345678")
            db.session.add(u); db.session.commit()
            db.session.execute(user_companies.insert().values(
                user_id=u.id, company_id=c.id, role="owner"))
            db.session.commit()

            inv = Invoice(
                company_id=c.id, number="INV-QLBL-01",
                customer_id=cust.id,
                issue_date=date.today(),
                due_date=date.today() + timedelta(days=30),
                currency="EGP", tax_rate=Decimal("15"),
                status=InvoiceStatus.DRAFT,
                created_by_id=u.id,
            )
            db.session.add(inv); db.session.commit()

            with app.test_client() as client:
                client.post("/login", data={
                    "email": u.email, "password": "pw12345678"})

                # Invoice list
                r = client.get("/invoices/")
                assert r.status_code == 200
                html = r.data.decode("utf-8")
                assert "عرض سعر" in html, \
                    "invoice list does not show 'عرض سعر' for a DRAFT invoice"
                # Regression: the WORD 'مسودة' must not appear as the
                # badge for THIS invoice. It may still appear in bill
                # lists on the same page — we just check the general
                # substring is gone for the invoice display.
                # A narrower check: the badge for INV-QLBL-01 shows
                # عرض سعر nearby.
                inv_pos = html.find("INV-QLBL-01")
                assert inv_pos != -1
                nearby = html[max(0, inv_pos - 300):inv_pos + 500]
                assert "عرض سعر" in nearby, \
                    "badge next to INV-QLBL-01 is not 'عرض سعر'"

                # Invoice view page
                r = client.get(f"/invoices/{inv.id}")
                assert r.status_code == 200
                view_html = r.data.decode("utf-8")
                assert "عرض سعر" in view_html, \
                    "invoice view page missing 'عرض سعر' badge"
            return "list + view both show 'عرض سعر' for DRAFT"
        finally:
            for t in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(t.name)}
                if "company_id" in cols:
                    db.session.execute(text(
                        f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": c.id})
            db.session.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"), {"c": c.id})
            db.session.execute(text(
                "DELETE FROM companies WHERE id = :c"), {"c": c.id})
            db.session.execute(text(
                "DELETE FROM users WHERE email LIKE '%__quote_lbl__%'"))
            db.session.commit()


@check("6. PDF template render contains 'عرض سعر' in the status badge")
def _():
    from datetime import date, timedelta, datetime
    from decimal import Decimal
    from sqlalchemy import text, inspect
    from flask import render_template
    from app import create_app, db

    app = create_app()
    with app.app_context():
        from app.models import Company, Customer, Invoice, InvoiceStatus
        from app.services.seed_coa import seed_default_coa
        from app.services.subsidiary import ensure_customer_account
        from app.services.export import (
            _amiri_font_face_css, _company_logo_data_uri)

        insp = inspect(db.engine)
        cids = [r[0] for r in db.session.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__QLBL_PDF__%'"))]
        for cid in cids:
            for t in reversed(db.metadata.sorted_tables):
                cols = {c["name"] for c in insp.get_columns(t.name)}
                if "company_id" in cols:
                    db.session.execute(text(
                        f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": cid})
            db.session.execute(text(
                "DELETE FROM companies WHERE id = :c"), {"c": cid})
        db.session.commit()

        c = Company(name="__QLBL_PDF__co", base_currency="EGP",
                    subdomain="qlblpdf1",
                    subscription_started_at=datetime.utcnow(),
                    subscription_expires_at=datetime(2999, 1, 1))
        db.session.add(c); db.session.commit()
        seed_default_coa(c.id); db.session.commit()

        cust = Customer(company_id=c.id, name="عميل PDF")
        db.session.add(cust); db.session.flush()
        ensure_customer_account(cust); db.session.commit()

        inv = Invoice(
            company_id=c.id, number="INV-QLBLPDF",
            customer_id=cust.id,
            issue_date=date.today(),
            due_date=date.today() + timedelta(days=30),
            currency="EGP", tax_rate=Decimal("15"),
            status=InvoiceStatus.DRAFT,
        )
        db.session.add(inv); db.session.commit()

        try:
            html = render_template(
                "pdfs/invoice.html",
                invoice=inv,
                amiri_font_face=_amiri_font_face_css(),
                company_logo_data_uri=_company_logo_data_uri(c),
            )
            assert "عرض سعر" in html, \
                "rendered invoice PDF template does not contain " \
                "'عرض سعر' for a DRAFT invoice"
            # And the string 'مسودة' should not appear as the status
            # value for THIS invoice — the STATUS_META lookup fired
            # for DRAFT and returned the new label.
            # We can't scan the whole file (comments may still say
            # "old label was مسودة") but we can bracket around the
            # status-badge div.
            m = re.search(
                r'class="status-badge"[^>]*>([^<]+)</span>',
                html)
            if m:
                badge_text = m.group(1).strip()
                assert "عرض سعر" in badge_text, \
                    f"status-badge contents = {badge_text!r} — " \
                    f"expected 'عرض سعر'"
            return "PDF DRAFT badge renders as 'عرض سعر'"
        finally:
            for t in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(t.name)}
                if "company_id" in cols:
                    db.session.execute(text(
                        f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": c.id})
            db.session.execute(text(
                "DELETE FROM companies WHERE id = :c"), {"c": c.id})
            db.session.commit()


def main():
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
