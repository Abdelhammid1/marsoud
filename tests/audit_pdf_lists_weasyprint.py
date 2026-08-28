#!/usr/bin/env python3
"""MARSOUD-TKT-PDFS-05-LISTS (Abdelhamid 2026-08-29) — the 8 list-report
PDFs must render via the shared WeasyPrint shell.

Continues the "all PDFs match the invoice design" migration from
tickets 1-4. This ticket migrated: P&L Compared, AR Aging, AP Aging,
Fixed Assets, Inventory Balance, Cashier Sales, Low Stock, Stock
Movements — 8 list-report PDFs, all WeasyPrint-first with ReportLab
_list_pdf / _simple_pdf_table as fallbacks.

Checks:
  1. All 8 templates extend _shell.html + import _report_macros.html.
  2-9. Each template carries the required Arabic columns + doc title.
  10. End-to-end smoke: bootstrap company (no data — empty tables),
      render each of the 8 PDFs via the service, assert %PDF + Amiri.
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
    assert m, f"{func_name} not found"
    return m.group(1)


TEMPLATES = [
    "pl_compared", "ar_aging", "ap_aging", "fixed_assets",
    "inventory_balance", "cashier_sales", "low_stock", "stock_movements",
]


@check("1. all 8 templates extend _shell.html + import _report_macros.html")
def _():
    for name in TEMPLATES:
        src = _strip_comments(_read(f"app/templates/pdfs/{name}.html"))
        assert 'extends "pdfs/_shell.html"' in src, \
            f"{name}.html does not extend pdfs/_shell.html"
        assert 'import "pdfs/_report_macros.html"' in src, \
            f"{name}.html does not import _report_macros.html"
    return f"all {len(TEMPLATES)} list templates wired to shell + macros"


# Per-template structural checks (2-9)
TEMPLATE_STRUCTURE = {
    "pl_compared": ("قائمة الدخل — مقارنة", ["البند", "الفترة الحالية", "الفترة السابقة", "التغير"]),
    "ar_aging": ("أعمار الذمم المدينة", ["العميل", "جاري", "1-30 يوم", "31-60 يوم", "الإجمالي"]),
    "ap_aging": ("أعمار الذمم الدائنة", ["المورد", "جاري", "1-30 يوم", "31-60 يوم", "الإجمالي"]),
    "fixed_assets": ("سجل الأصول الثابتة", ["الأصل", "المورد", "تاريخ الشراء", "التكلفة", "القيمة الدفترية"]),
    "inventory_balance": ("رصيد المخزون الحالي", ["SKU", "المنتج", "المخزن", "الكمية", "متوسط التكلفة", "القيمة"]),
    "cashier_sales": ("مبيعات الكاشيرز", ["الكاشير", "الأوردرات", "الملغى", "المبيعات", "الصافي", "طرق الدفع"]),
    "low_stock": ("أصناف تحت حد الطلب", ["SKU", "المنتج", "المتاح", "حد الطلب", "متوسط التكلفة", "القيمة"]),
    "stock_movements": ("سجل حركات المخزون", ["التاريخ", "النوع", "الصنف", "المخزن", "الكمية", "الرصيد بعد", "المنفّذ"]),
}


def _make_structural_check(idx, name, doc_title, required):
    @check(f"{idx}. {name}.html: doc title + required columns")
    def _():
        src = _strip_comments(_read(f"app/templates/pdfs/{name}.html"))
        assert doc_title in src, f"{name}.html missing doc title: {doc_title!r}"
        for col in required:
            assert col in src, f"{name}.html missing column: {col!r}"
        assert "rpt.outcome_block" in src, \
            f"{name}.html does not emit an outcome_block"
        return f"{name} template complete"
    _.__name__ = f"check_{idx}_{name}"
    return _


for _idx, _name in enumerate(TEMPLATES, start=2):
    _make_structural_check(_idx, _name, *TEMPLATE_STRUCTURE[_name])


@check("10. end-to-end: render all 8 PDFs via services, Amiri embedded")
def _():
    from datetime import datetime, date
    from sqlalchemy import text, inspect
    from app import create_app, db

    app = create_app()
    with app.app_context():
        from app.models import Company
        from app.services.seed_coa import seed_default_coa
        from app.services.export import (
            export_pl_compared, export_ar_aging, export_ap_aging,
            export_fixed_assets, export_inventory_balance_pdf,
            export_cashier_sales_pdf, export_low_stock_pdf,
            export_stock_movements_pdf,
        )
        insp = inspect(db.engine)
        cids = [r[0] for r in db.session.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__LISTS_AUDIT__%'"))]
        for cid in cids:
            for t in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(t.name)}
                if "company_id" in cols:
                    db.session.execute(text(f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": cid})
            db.session.execute(text("DELETE FROM user_companies WHERE company_id = :c"), {"c": cid})
            db.session.execute(text("DELETE FROM companies WHERE id = :c"), {"c": cid})
        db.session.commit()

        c = Company(name="__LISTS_AUDIT__منصتي", base_currency="EGP",
                    subdomain="lists-audit-1",
                    subscription_started_at=datetime.utcnow(),
                    subscription_expires_at=datetime(2999, 1, 1))
        db.session.add(c); db.session.commit()
        seed_default_coa(c.id)
        db.session.commit()

        try:
            end = date(2026, 9, 30)
            start = date(2026, 9, 1)
            results = []
            for label, call in [
                ("pl_compared", lambda: export_pl_compared(c, "pdf", start, end)),
                ("ar_aging", lambda: export_ar_aging(c, "pdf", end)),
                ("ap_aging", lambda: export_ap_aging(c, "pdf", end)),
                ("fixed_assets", lambda: export_fixed_assets(c, "pdf")),
                ("inventory_balance", lambda: export_inventory_balance_pdf(c)),
                ("cashier_sales", lambda: export_cashier_sales_pdf(c, start, end)),
                ("low_stock", lambda: export_low_stock_pdf(c)),
                ("stock_movements", lambda: export_stock_movements_pdf(c, start, end)),
            ]:
                out = call()
                buf = out[0] if isinstance(out, tuple) else out
                data = buf.read()
                assert data.startswith(b"%PDF"), \
                    f"{label}: not a PDF (first 8 bytes: {data[:8]!r})"
                assert len(data) > 500, \
                    f"{label}: PDF suspiciously small ({len(data)} bytes)"
                assert b"Amiri" in data, \
                    f"{label}: Amiri font not embedded"
                results.append(f"{label} {len(data)}B")
        finally:
            for t in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(t.name)}
                if "company_id" in cols:
                    db.session.execute(text(f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": c.id})
            db.session.execute(text("DELETE FROM user_companies WHERE company_id = :c"), {"c": c.id})
            db.session.execute(text("DELETE FROM companies WHERE id = :c"), {"c": c.id})
            db.session.commit()

        return "all 8 PDFs valid + Amiri embedded: " + ", ".join(results)


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
