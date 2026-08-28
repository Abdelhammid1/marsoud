#!/usr/bin/env python3
"""MARSOUD-TKT-PDFS-02-FIN-REPORTS (Abdelhamid 2026-08-29) — the three
financial-statement PDFs (Balance Sheet, Income Statement, Cash Flow)
must render via the shared WeasyPrint shell.

Continuing MARSOUD-TKT-PDFS-01-JOURNAL's ratchet: all PDFs match the
invoice design, no exceptions. This ticket migrated the three
financial-statement PDFs from ReportLab to WeasyPrint templates
extending pdfs/_shell.html + a new companion pdfs/_report_macros.html
(account_group + outcome_block macros).

Checks:
  1. pdfs/_report_macros.html defines account_group + outcome_block.
  2. pdfs/balance_sheet.html extends _shell.html + imports the macros
     + carries the three section titles + balance-check.
  3. pdfs/income_statement.html same shape + Net Profit block.
  4. pdfs/cash_flow.html same shape + 3 activities + Net Change block.
  5. export_balance_sheet_pdf is WeasyPrint-first with legacy fallback.
  6. export_income_statement_pdf same.
  7. export_cash_flow `fmt == "pdf"` branch is WeasyPrint-first with
     legacy fallback; Excel branch untouched.
  8. End-to-end smoke: bootstrap a fresh company + one balanced JE,
     render all three PDFs via the service functions, assert `%PDF`
     magic + `b"Amiri"` embedded font for each. Falls through to
     ReportLab legacy on hosts without libpango — ReportLab also
     embeds Amiri via pdfmetrics.registerFont at export.py:44.
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
    """Same idiom as audit_pdf_journal_weasyprint.py — strip Jinja,
    JS-line, then CSS comments so retirement-doc notes in the source
    don't false-positive substring checks."""
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


@check("1. pdfs/_report_macros.html defines account_group + outcome_block")
def _():
    src = _read("app/templates/pdfs/_report_macros.html")
    assert "{% macro account_group(" in src, \
        "_report_macros.html missing account_group macro"
    assert "{% macro outcome_block(" in src, \
        "_report_macros.html missing outcome_block macro"
    return "both macros defined"


@check("2. pdfs/balance_sheet.html extends shell + BS sections")
def _():
    src = _strip_comments(_read("app/templates/pdfs/balance_sheet.html"))
    assert 'extends "pdfs/_shell.html"' in src, \
        "balance_sheet.html does not extend pdfs/_shell.html"
    assert 'import "pdfs/_report_macros.html"' in src, \
        "balance_sheet.html does not import _report_macros.html"
    for arabic in ("الميزانية العمومية", "الأصول", "الالتزامات",
                   "حقوق الملكية", "إجمالي الأصول"):
        assert arabic in src, \
            f"balance_sheet.html missing Arabic label: {arabic!r}"
    assert "rpt.outcome_block" in src, \
        "balance_sheet.html does not emit the balance-check outcome block"
    return "extends shell + 3 sections + balance check"


@check("3. pdfs/income_statement.html extends shell + IS sections")
def _():
    src = _strip_comments(_read("app/templates/pdfs/income_statement.html"))
    assert 'extends "pdfs/_shell.html"' in src
    assert 'import "pdfs/_report_macros.html"' in src
    for arabic in ("قائمة الدخل", "الإيرادات", "المصروفات",
                   "إجمالي الإيرادات", "إجمالي المصروفات"):
        assert arabic in src, \
            f"income_statement.html missing Arabic label: {arabic!r}"
    assert "rpt.outcome_block" in src, \
        "income_statement.html does not emit the Net Profit outcome block"
    return "extends shell + 2 sections + Net Profit block"


@check("4. pdfs/cash_flow.html extends shell + activities + Net Change")
def _():
    src = _strip_comments(_read("app/templates/pdfs/cash_flow.html"))
    assert 'extends "pdfs/_shell.html"' in src
    for arabic in ("قائمة التدفقات النقدية", "الأنشطة التشغيلية",
                   "الأنشطة الاستثمارية", "الأنشطة التمويلية",
                   "صافي التغير في النقد"):
        assert arabic in src, \
            f"cash_flow.html missing Arabic label: {arabic!r}"
    assert "rpt.outcome_block" in src, \
        "cash_flow.html does not emit the Net Change outcome block"
    return "extends shell + 3 activities + Net Change block"


def _extract_body(src, func_name):
    """Return the body of `def <func_name>(` up to the next top-level def."""
    m = re.search(
        r"^def " + re.escape(func_name) + r"\([^)]*\):\n(.*?)(?=^def \w)",
        src, re.MULTILINE | re.DOTALL,
    )
    assert m, f"{func_name} not found in services/export.py"
    return m.group(1)


@check("5. export_balance_sheet_pdf: WeasyPrint-first + legacy fallback")
def _():
    src = _strip_comments(_read("app/services/export.py"))
    body = _extract_body(src, "export_balance_sheet_pdf")
    assert "_weasyprint_render(" in body, \
        "export_balance_sheet_pdf no longer calls _weasyprint_render"
    assert '"pdfs/balance_sheet.html"' in body, \
        "export_balance_sheet_pdf does not point at pdfs/balance_sheet.html"
    assert "_export_balance_sheet_pdf_legacy" in body, \
        "export_balance_sheet_pdf lost its ReportLab fallback"
    assert re.search(
        r"^def _export_balance_sheet_pdf_legacy\(", src, re.MULTILINE
    ), "_export_balance_sheet_pdf_legacy definition missing"
    return "WeasyPrint primary + ReportLab fallback both present"


@check("6. export_income_statement_pdf: WeasyPrint-first + legacy fallback")
def _():
    src = _strip_comments(_read("app/services/export.py"))
    body = _extract_body(src, "export_income_statement_pdf")
    assert "_weasyprint_render(" in body
    assert '"pdfs/income_statement.html"' in body
    assert "_export_income_statement_pdf_legacy" in body
    assert re.search(
        r"^def _export_income_statement_pdf_legacy\(", src, re.MULTILINE
    )
    return "WeasyPrint primary + ReportLab fallback both present"


@check("7. export_cash_flow PDF branch: WeasyPrint + legacy fallback")
def _():
    """export_cash_flow is a multi-format entrypoint (pdf | xlsx). Only
    the pdf branch is in scope for this migration — Excel path is
    untouched and out of the design-unification concern."""
    src = _strip_comments(_read("app/services/export.py"))
    body = _extract_body(src, "export_cash_flow")
    # PDF branch must call WeasyPrint + point at cash_flow.html
    assert 'fmt == "pdf"' in body, \
        "export_cash_flow no longer distinguishes fmt=='pdf' branch"
    assert "_weasyprint_render(" in body, \
        "export_cash_flow PDF branch no longer calls _weasyprint_render"
    assert '"pdfs/cash_flow.html"' in body, \
        "export_cash_flow PDF branch does not point at pdfs/cash_flow.html"
    assert "_export_cash_flow_pdf_legacy" in body, \
        "export_cash_flow lost its ReportLab fallback"
    assert re.search(
        r"^def _export_cash_flow_pdf_legacy\(", src, re.MULTILINE
    ), "_export_cash_flow_pdf_legacy definition missing"
    # Excel branch preserved
    assert "_list_excel(" in body, \
        "export_cash_flow Excel branch was accidentally removed"
    return "PDF via WeasyPrint + Excel branch untouched"


@check("8. end-to-end: render all three reports, Amiri embedded in each")
def _():
    """Bootstrap a company with the default COA + a couple of balanced
    JEs, then call each of the three service functions and confirm
    each returns a valid PDF with Amiri embedded. Fallback path
    (ReportLab on hosts without libpango) also embeds Amiri."""
    from datetime import datetime, date
    from decimal import Decimal
    from sqlalchemy import text, inspect
    from app import create_app, db

    app = create_app()
    with app.app_context():
        from app.models import Company, Account, AccountType, NormalSide
        from app.models.journal import JournalEntry, JournalLine
        from app.services.seed_coa import seed_default_coa
        from app.services.export import (
            export_balance_sheet_pdf,
            export_income_statement_pdf,
            export_cash_flow,
        )
        insp = inspect(db.engine)
        # Clean prior fixtures on the shared DB
        cids = [r[0] for r in db.session.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__FINAUDIT__%'"))]
        for cid in cids:
            for t in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(t.name)}
                if "company_id" in cols:
                    db.session.execute(text(f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": cid})
            db.session.execute(text("DELETE FROM user_companies WHERE company_id = :c"), {"c": cid})
            db.session.execute(text("DELETE FROM companies WHERE id = :c"), {"c": cid})
        db.session.commit()

        c = Company(name="__FINAUDIT__منصتي", base_currency="EGP",
                    subdomain="finaudit-1",
                    subscription_started_at=datetime.utcnow(),
                    subscription_expires_at=datetime(2999, 1, 1))
        db.session.add(c); db.session.commit()
        seed_default_coa(c.id)
        db.session.commit()

        cash = Account.query.filter_by(company_id=c.id, code="1110").first()
        rev = Account.query.filter_by(
            company_id=c.id, type=AccountType.REVENUE, is_postable=True
        ).first()

        je = JournalEntry(company_id=c.id, number="JE-FIN-AUDIT",
            date=date(2026, 9, 15),
            description="اختبار مالي", currency="EGP", is_active=True)
        db.session.add(je); db.session.flush()
        db.session.execute(text("DELETE FROM journal_lines WHERE entry_id = :e"), {"e": je.id})
        db.session.add_all([
            JournalLine(entry_id=je.id, account_id=cash.id,
                debit=Decimal("100.00"), credit=Decimal("0.00"),
                memo="مقبوض"),
            JournalLine(entry_id=je.id, account_id=rev.id,
                debit=Decimal("0.00"), credit=Decimal("100.00"),
                memo="مبيعات"),
        ])
        db.session.commit()

        try:
            # Three renders. Each must return a valid PDF (either via
            # WeasyPrint OR the legacy ReportLab fallback), and each
            # must embed Amiri.
            results = []
            for label, call in [
                ("balance_sheet",
                 lambda: export_balance_sheet_pdf(c, as_of=date(2026, 9, 30))),
                ("income_statement",
                 lambda: export_income_statement_pdf(
                     c, start=date(2026, 9, 1), end=date(2026, 9, 30))),
                ("cash_flow",
                 lambda: export_cash_flow(
                     c, fmt="pdf", start=date(2026, 9, 1), end=date(2026, 9, 30))),
            ]:
                out = call()
                # cash_flow returns (buf, filename, mime); the other two return buf.
                buf = out[0] if isinstance(out, tuple) else out
                data = buf.read()
                assert data.startswith(b"%PDF"), \
                    f"{label}: not a PDF (first 8 bytes: {data[:8]!r})"
                assert len(data) > 500, \
                    f"{label}: PDF suspiciously small ({len(data)} bytes)"
                assert b"Amiri" in data, (
                    f"{label}: Amiri font not embedded — neither the "
                    f"WeasyPrint path (pdfs/*.html + amiri_font_face) "
                    f"NOR the ReportLab fallback (pdfmetrics.registerFont) "
                    f"landed the font in the stream."
                )
                results.append(f"{label} {len(data)}B")
        finally:
            # Cleanup
            for t in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(t.name)}
                if "company_id" in cols:
                    db.session.execute(text(f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": c.id})
            db.session.execute(text("DELETE FROM user_companies WHERE company_id = :c"), {"c": c.id})
            db.session.execute(text("DELETE FROM companies WHERE id = :c"), {"c": c.id})
            db.session.commit()

        return "all 3 PDFs valid + Amiri embedded: " + ", ".join(results)


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
