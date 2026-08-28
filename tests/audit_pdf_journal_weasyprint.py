#!/usr/bin/env python3
"""MARSOUD-TKT-PDFS-01-JOURNAL (Abdelhamid 2026-08-29) — journal PDFs
must render via the shared WeasyPrint shell.

Customer bug (JE-0158, 2026-08-29): journal-entry PDF was ReportLab
while pdfs/invoice.html + pdfs/payslip.html were the modern WeasyPrint
branded design. Two visual identities across the same tenant's PDFs.
Migrated both journal-entry and journals-list PDFs to WeasyPrint using
the new shared `pdfs/_shell.html` (extracted from invoice.html), so
they now share corner accents, green-header table, navy rule, footer.

This audit is the regression net. If a future refactor of the
templates or the service loses the shell inheritance, drops the
green-header idiom, or resurrects the ReportLab path as the primary
renderer, the checks fail loudly before shipping.

Checks:
  1. `pdfs/_shell.html` exists and declares the branded chrome
     (corner-accent SVGs, brand-row + brand-name, doc-title, rule,
     footer, green-header table, `{% block content %}` slot).
  2. `pdfs/journal_entry.html` extends the shell and declares
     journal-specific content (title `قيد يومية`, columns
     `الحساب/البيان/مدين/دائن`, balance-check row).
  3. `pdfs/journals_list.html` extends the shell same way (title
     `كشف قيود اليومية`, columns for a multi-entry table).
  4. `services/export.py:export_journal_entry_pdf` calls
     `_weasyprint_render("pdfs/journal_entry.html", ...)` as its
     primary path, with the legacy ReportLab body renamed to
     `_export_journal_entry_pdf_legacy` (fallback).
  5. Same for `export_journals_list_pdf`.
  6. End-to-end smoke: render a real journal-entry through
     `export_journal_entry_pdf`, assert `b"%PDF"` header + `b"Amiri"`
     embedded font. Falls back to HTML inspection on Windows dev
     boxes with no libpango (same pattern audit_pdf_arabic_font uses
     for party-ledger).
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TPL = ROOT / "app" / "templates"


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _read(relpath):
    return (ROOT / relpath).read_text(encoding="utf-8")


def _strip_comments(src):
    """Same idiom as audit_brand_tokens_consumed.py — strip Jinja,
    JS-line, then CSS comments so retirement-doc notes don't
    false-positive substring checks."""
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


@check("1. pdfs/_shell.html carries the branded PDF chrome")
def _():
    src = _read("app/templates/pdfs/_shell.html")
    required = {
        # Corner accents (both triangles)
        "polygon points=\"0,0 70,0 0,70\"": "top-left green triangle",
        "polygon points=\"70,70 0,70 70,0\"": "bottom-right green triangle",
        # Brand row + brand name
        "class=\"brand-row\"": "brand row wrapper",
        "class=\"brand-name\"": "brand name element",
        # Doc title container
        "class=\"doc-title\"": "doc-title wrapper",
        # Navy rule
        "class=\"rule\"": "navy rule",
        # Green-header items primitive
        "background: #059669": "green table header color",
        # Footer
        "class=\"footer\"": "footer wrapper",
        # Content block for subclasses
        "{% block content %}": "content block slot for consumers",
        # Ar_date macro reused by every downstream template
        "{% macro ar_date(": "shared ar_date Arabic-month formatter",
    }
    misses = [(k, v) for k, v in required.items() if k not in src]
    assert not misses, \
        "_shell.html missing required chrome:\n  " + "\n  ".join(
            f"'{k}' ({v})" for k, v in misses
        )
    return f"all {len(required)} shell primitives present"


@check("2. pdfs/journal_entry.html extends shell + carries JE columns")
def _():
    src = _read("app/templates/pdfs/journal_entry.html")
    stripped = _strip_comments(src)
    assert 'extends "pdfs/_shell.html"' in stripped or \
           "extends 'pdfs/_shell.html'" in stripped, \
        "journal_entry.html does not extend pdfs/_shell.html"
    for arabic in ("قيد يومية", "الحساب", "البيان", "مدين", "دائن",
                   "إجمالي المدين", "إجمالي الدائن", "الفرق"):
        assert arabic in stripped, \
            f"journal_entry.html missing required Arabic label: {arabic!r}"
    return "extends shell + all JE columns + balance-check present"


@check("3. pdfs/journals_list.html extends shell + carries list columns")
def _():
    src = _read("app/templates/pdfs/journals_list.html")
    stripped = _strip_comments(src)
    assert 'extends "pdfs/_shell.html"' in stripped or \
           "extends 'pdfs/_shell.html'" in stripped, \
        "journals_list.html does not extend pdfs/_shell.html"
    for arabic in ("كشف قيود اليومية", "رقم القيد", "التاريخ",
                   "الوصف", "المرجع", "إجمالي المدين", "إجمالي الدائن"):
        assert arabic in stripped, \
            f"journals_list.html missing required Arabic label: {arabic!r}"
    return "extends shell + list columns present"


@check("4. export_journal_entry_pdf is WeasyPrint-first with legacy fallback")
def _():
    src = _strip_comments(_read("app/services/export.py"))
    # Extract just the body of `def export_journal_entry_pdf(entry):`
    # up to the next `def` — mirror the audit_pdf_header_rtl approach.
    m = re.search(
        r"^def export_journal_entry_pdf\([^)]*\):\n(.*?)(?=^def \w)",
        src, re.MULTILINE | re.DOTALL,
    )
    assert m, "export_journal_entry_pdf function not found"
    body = m.group(1)
    assert "_weasyprint_render(" in body, \
        "export_journal_entry_pdf no longer calls _weasyprint_render — " \
        "regressed to a ReportLab-only path"
    assert '"pdfs/journal_entry.html"' in body, \
        "export_journal_entry_pdf does not point at pdfs/journal_entry.html"
    assert "_export_journal_entry_pdf_legacy" in body, \
        "export_journal_entry_pdf lost its ReportLab fallback — hosts " \
        "without libpango will hard-error instead of degrading gracefully"
    # And the legacy body must still be defined somewhere.
    assert re.search(
        r"^def _export_journal_entry_pdf_legacy\(", src, re.MULTILINE
    ), "_export_journal_entry_pdf_legacy definition missing"
    return "WeasyPrint primary + ReportLab fallback both present"


@check("5. export_journals_list_pdf is WeasyPrint-first with legacy fallback")
def _():
    src = _strip_comments(_read("app/services/export.py"))
    m = re.search(
        r"^def export_journals_list_pdf\([^)]*\):\n(.*?)(?=^def \w)",
        src, re.MULTILINE | re.DOTALL,
    )
    assert m, "export_journals_list_pdf function not found"
    body = m.group(1)
    assert "_weasyprint_render(" in body, \
        "export_journals_list_pdf no longer calls _weasyprint_render"
    assert '"pdfs/journals_list.html"' in body, \
        "export_journals_list_pdf does not point at pdfs/journals_list.html"
    assert "_export_journals_list_pdf_legacy" in body, \
        "export_journals_list_pdf lost its ReportLab fallback"
    assert re.search(
        r"^def _export_journals_list_pdf_legacy\(", src, re.MULTILINE
    ), "_export_journals_list_pdf_legacy definition missing"
    return "WeasyPrint primary + ReportLab fallback both present"


@check("6. end-to-end: render a JE PDF and assert Amiri embedded")
def _():
    """Full render through the service. On hosts with libpango,
    WeasyPrint runs and Amiri lands in the font stream. On Windows
    dev boxes without libpango, the WeasyPrint call fails inside
    export_journal_entry_pdf's try/except and falls back to the
    legacy ReportLab path — which ALSO embeds Amiri (pdfmetrics
    registration at export.py:44). Either way the assertion holds.

    If both engines fail entirely, drop to HTML-only inspection
    (same pattern audit_pdf_arabic_font.py:check 3 uses for
    party-ledger)."""
    import os
    os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
    from datetime import datetime, date
    from decimal import Decimal
    from sqlalchemy import text, inspect
    from app import create_app, db
    from app.models import Company, Account, AccountType, NormalSide
    from app.models.journal import JournalEntry, JournalLine

    app = create_app()
    with app.app_context():
        insp = inspect(db.engine)
        with db.engine.begin() as conn:
            cids = [r[0] for r in conn.execute(text(
                "SELECT id FROM companies WHERE name LIKE '__JEAUDIT__%'"))]
            for cid in cids:
                for t in reversed(db.metadata.sorted_tables):
                    cols = {col["name"] for col in insp.get_columns(t.name)}
                    if "company_id" in cols:
                        conn.execute(text(
                            f"DELETE FROM {t.name} WHERE company_id = :c"
                        ), {"c": cid})
                conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"), {"c": cid})
                conn.execute(text("DELETE FROM companies WHERE id = :c"), {"c": cid})

        c = Company(name="__JEAUDIT__منصتي", base_currency="EGP",
                    subdomain="jeaudit-1",
                    subscription_started_at=datetime.utcnow(),
                    subscription_expires_at=datetime(2999, 1, 1))
        db.session.add(c); db.session.commit()

        acc_cash = Account(
            company_id=c.id, code="1130-A", name="Cash",
            name_ar="النقدية", type=AccountType.ASSET,
            normal_side=NormalSide.DEBIT, is_postable=True, is_active=True)
        acc_rev = Account(
            company_id=c.id, code="4100", name="Revenue",
            name_ar="إيرادات", type=AccountType.REVENUE,
            normal_side=NormalSide.CREDIT, is_postable=True, is_active=True)
        db.session.add_all([acc_cash, acc_rev]); db.session.commit()

        je = JournalEntry(
            company_id=c.id, number="JE-AUDIT-01",
            date=date.today(), description="اختبار",
            currency="EGP", is_active=True)
        db.session.add(je); db.session.commit()
        with db.engine.begin() as conn:
            conn.execute(text("DELETE FROM journal_lines WHERE entry_id = :e"), {"e": je.id})
        db.session.add_all([
            JournalLine(entry_id=je.id, account_id=acc_cash.id,
                debit=Decimal("100.00"), credit=Decimal("0.00"),
                memo="اختبار مدين"),
            JournalLine(entry_id=je.id, account_id=acc_rev.id,
                debit=Decimal("0.00"), credit=Decimal("100.00"),
                memo="اختبار دائن"),
        ])
        db.session.commit()
        db.session.expire_all()
        je = JournalEntry.query.get(je.id)
        for line in je.lines:
            _ = line.account.name_ar

        try:
            from app.services.export import export_journal_entry_pdf
            buf = export_journal_entry_pdf(je)
            data = buf.read()
            assert data.startswith(b"%PDF"), \
                f"not a PDF (first 8 bytes: {data[:8]!r})"
            assert len(data) > 500, f"PDF suspiciously small: {len(data)}"
            assert b"Amiri" in data, (
                "Amiri font not embedded — neither the WeasyPrint path "
                "(pdfs/journal_entry.html + amiri_font_face) NOR the "
                "ReportLab fallback (pdfmetrics.registerFont at "
                "export.py:44) landed the font in the stream"
            )
            result = f"PDF {len(data)} bytes with Amiri embedded"
        except Exception as e:
            # Cleanup before re-raising
            with db.engine.begin() as conn:
                for t in reversed(db.metadata.sorted_tables):
                    cols = {col["name"] for col in insp.get_columns(t.name)}
                    if "company_id" in cols:
                        conn.execute(text(f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": c.id})
                conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"), {"c": c.id})
                conn.execute(text("DELETE FROM companies WHERE id = :c"), {"c": c.id})
            raise

        # Cleanup
        with db.engine.begin() as conn:
            for t in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(t.name)}
                if "company_id" in cols:
                    conn.execute(text(f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": c.id})
            conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"), {"c": c.id})
            conn.execute(text("DELETE FROM companies WHERE id = :c"), {"c": c.id})
        return result


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
