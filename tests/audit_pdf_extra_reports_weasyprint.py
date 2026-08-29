#!/usr/bin/env python3
"""MARSOUD-TKT-PDFS-03-EXTRA-REPORTS (Abdelhamid 2026-08-29) — VAT
Return + Expenses Summary + Income Summary + Product Profitability
PDFs must render via the shared WeasyPrint shell.

Continues the "all PDFs match the invoice design" migration. Tickets
1 + 2 shipped the shared `pdfs/_shell.html` + `pdfs/_report_macros.html`
foundation and migrated JE + BS + IS + CF. This ticket adds the four
list/summary-style reports (VAT / expenses / income / profitability).

Checks:
  1. All 4 templates extend _shell.html + import _report_macros.html.
  2. vat_return.html carries the two VAT rows + NET outcome_block.
  3. expenses_summary.html carries the 4-column table + total block.
  4. income_summary.html reuses account_group + total block.
  5. profitability.html carries the 7-column table + total row +
     margin-tone outcome block.
  6. export_vat_report PDF branch WeasyPrint-first + legacy fallback.
  7. export_expenses_summary + export_income_summary PDF branches same.
  8. export_profitability_pdf WeasyPrint-first + legacy fallback.
  9. End-to-end: bootstrap a fresh company + fixtures, render all 4
     PDFs via the public service functions, assert %PDF + Amiri.
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
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


def _extract_body(src, func_name):
    m = re.search(
        r"^def " + re.escape(func_name) + r"\([^)]*\):\n(.*?)(?=^def \w)",
        src, re.MULTILINE | re.DOTALL,
    )
    assert m, f"{func_name} not found in services/export.py"
    return m.group(1)


@check("1. all 4 templates extend _shell.html + import _report_macros.html")
def _():
    for path in ("vat_return", "expenses_summary", "income_summary", "profitability"):
        src = _strip_comments(_read(f"app/templates/pdfs/{path}.html"))
        assert 'extends "pdfs/_shell.html"' in src, \
            f"{path}.html does not extend pdfs/_shell.html"
        assert 'import "pdfs/_report_macros.html"' in src, \
            f"{path}.html does not import _report_macros.html"
    return "all 4 templates wired to the shell + macros"


@check("2. vat_return.html: 2 VAT rows + NET outcome_block")
def _():
    src = _strip_comments(_read("app/templates/pdfs/vat_return.html"))
    for arabic in ("إقرار ضريبة القيمة المضافة",
                   "ضريبة محصّلة على المبيعات",
                   "ضريبة مدفوعة للموردين"):
        assert arabic in src, f"vat_return.html missing: {arabic!r}"
    assert "rpt.outcome_block" in src, \
        "vat_return.html does not emit the NET outcome_block"
    # Sign-based label switch — both variants must be present
    for label in ("صافي المستحق للحكومة", "صافي المسترد من الحكومة"):
        assert label in src, f"vat_return.html missing sign label: {label!r}"
    return "VAT template complete"


@check("3. expenses_summary.html: 4-column table + total block")
def _():
    src = _strip_comments(_read("app/templates/pdfs/expenses_summary.html"))
    for arabic in ("ملخص المصروفات", "الكود", "الحساب",
                   "عدد القيود", "المبلغ"):
        assert arabic in src, f"expenses_summary.html missing: {arabic!r}"
    assert "rpt.outcome_block" in src, \
        "expenses_summary.html does not emit total outcome_block"
    return "expenses summary template complete"


@check("4. income_summary.html: account_group + total block")
def _():
    src = _strip_comments(_read("app/templates/pdfs/income_summary.html"))
    assert "ملخص الإيرادات" in src
    assert "rpt.account_group" in src, \
        "income_summary.html should reuse the account_group macro"
    assert "rpt.outcome_block" in src, \
        "income_summary.html does not emit total outcome_block"
    return "income summary template complete"


@check("5. profitability.html: 7-column table + total + margin block")
def _():
    src = _strip_comments(_read("app/templates/pdfs/profitability.html"))
    for arabic in ("ربحية المنتجات", "SKU", "المنتج", "الكمية",
                   "المبيعات", "تكلفة البضاعة", "مجمل الربح",
                   "الهامش"):
        assert arabic in src, f"profitability.html missing: {arabic!r}"
    assert "rpt.outcome_block" in src, \
        "profitability.html does not emit total outcome_block"
    # Column count check — the header row has all 7 columns
    assert src.count("<th") >= 7, \
        "profitability.html items table has fewer than 7 header cells"
    return "profitability template complete"


@check("6. export_vat_report PDF branch WeasyPrint-first + legacy fallback")
def _():
    src = _strip_comments(_read("app/services/export.py"))
    body = _extract_body(src, "export_vat_report")
    assert 'fmt == "pdf"' in body
    assert "_weasyprint_render(" in body
    assert '"pdfs/vat_return.html"' in body
    assert "_export_vat_report_pdf_legacy" in body
    assert re.search(r"^def _export_vat_report_pdf_legacy\(", src, re.MULTILINE)
    return "VAT report: WeasyPrint primary + ReportLab fallback"


@check("7. export_expenses_summary + export_income_summary PDF branches wired")
def _():
    src = _strip_comments(_read("app/services/export.py"))
    for fn, template, legacy in [
        ("export_expenses_summary", "pdfs/expenses_summary.html",
         "_export_expenses_summary_pdf_legacy"),
        ("export_income_summary", "pdfs/income_summary.html",
         "_export_income_summary_pdf_legacy"),
    ]:
        body = _extract_body(src, fn)
        assert 'fmt == "pdf"' in body, f"{fn} lost fmt branching"
        assert "_weasyprint_render(" in body, f"{fn} no longer WeasyPrint"
        assert f'"{template}"' in body, f"{fn} does not point at {template}"
        assert legacy in body, f"{fn} lost its legacy fallback"
        assert re.search(rf"^def {re.escape(legacy)}\(", src, re.MULTILINE), \
            f"{legacy} definition missing"
    return "both summary services WeasyPrint-first"


@check("8. export_profitability_pdf WeasyPrint-first + legacy fallback")
def _():
    src = _strip_comments(_read("app/services/export.py"))
    body = _extract_body(src, "export_profitability_pdf")
    assert "_weasyprint_render(" in body
    assert '"pdfs/profitability.html"' in body
    assert "_export_profitability_pdf_legacy" in body
    assert re.search(r"^def _export_profitability_pdf_legacy\(", src, re.MULTILINE)
    return "profitability: WeasyPrint primary + ReportLab fallback"


@check("9. end-to-end: render all 4 PDFs + Amiri embedded each")
def _():
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
            export_vat_report, export_expenses_summary,
            export_income_summary, export_profitability_pdf,
        )
        insp = inspect(db.engine)
        cids = [r[0] for r in db.session.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__EXTRAUDIT__%'"))]
        for cid in cids:
            for t in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(t.name)}
                if "company_id" in cols:
                    db.session.execute(text(f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": cid})
            db.session.execute(text("DELETE FROM user_companies WHERE company_id = :c"), {"c": cid})
            db.session.execute(text("DELETE FROM companies WHERE id = :c"), {"c": cid})
        db.session.commit()

        c = Company(name="__EXTRAUDIT__منصتي", base_currency="EGP",
                    subdomain="extra-1",
                    subscription_started_at=datetime.utcnow(),
                    subscription_expires_at=datetime(2999, 1, 1))
        db.session.add(c); db.session.commit()
        seed_default_coa(c.id)
        db.session.commit()

        # Post a JE so the summary reports have something to show.
        cash = Account.query.filter_by(company_id=c.id, code="1110").first()
        rev = Account.query.filter_by(
            company_id=c.id, type=AccountType.REVENUE, is_postable=True).first()
        je = JournalEntry(company_id=c.id, number="JE-EXTRA-1",
            date=date(2026, 9, 15), description="اختبار",
            currency="EGP", is_active=True)
        db.session.add(je); db.session.flush()
        db.session.execute(text("DELETE FROM journal_lines WHERE entry_id = :e"), {"e": je.id})
        db.session.add_all([
            JournalLine(entry_id=je.id, account_id=cash.id,
                debit=Decimal("100"), credit=Decimal("0"), memo="مقبوض"),
            JournalLine(entry_id=je.id, account_id=rev.id,
                debit=Decimal("0"), credit=Decimal("100"), memo="مبيعات"),
        ])
        db.session.commit()

        try:
            results = []
            for label, call in [
                ("vat_return",
                 lambda: export_vat_report(
                     c, fmt="pdf", start=date(2026, 9, 1), end=date(2026, 9, 30))),
                ("expenses_summary",
                 lambda: export_expenses_summary(
                     c, fmt="pdf", start=date(2026, 9, 1), end=date(2026, 9, 30))),
                ("income_summary",
                 lambda: export_income_summary(
                     c, fmt="pdf", start=date(2026, 9, 1), end=date(2026, 9, 30))),
                ("profitability",
                 lambda: export_profitability_pdf(
                     c, start=date(2026, 9, 1), end=date(2026, 9, 30))),
            ]:
                out = call()
                buf = out[0] if isinstance(out, tuple) else out
                data = buf.read()
                assert data.startswith(b"%PDF"), \
                    f"{label}: not a PDF (first 8 bytes: {data[:8]!r})"
                assert len(data) > 500, \
                    f"{label}: PDF suspiciously small ({len(data)} bytes)"
                assert b"Amiri" in data, (
                    f"{label}: Amiri font not embedded — neither WeasyPrint "
                    f"nor ReportLab landed the font"
                )
                results.append(f"{label} {len(data)}B")
        finally:
            for t in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(t.name)}
                if "company_id" in cols:
                    db.session.execute(text(f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": c.id})
            db.session.execute(text("DELETE FROM user_companies WHERE company_id = :c"), {"c": c.id})
            db.session.execute(text("DELETE FROM companies WHERE id = :c"), {"c": c.id})
            db.session.commit()

        return "all 4 PDFs valid + Amiri embedded: " + ", ".join(results)


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
