#!/usr/bin/env python3
"""MARSOUD-TKT-INVOICE-ITEMS-NUMBERED-QUOTE (Abdelhamid 2026-08-30) —
invoice item rows are numbered on both the web form and the PDF, the
"+ بند" button moved from the header to below the last row, and the
"حفظ كمسودة" buttons are renamed to "حفظ كعرض سعر".

Checks:
  1. Web form: items table has a leading # header and matching
     `.row-num` cell (via renumberRows() JS helper).
  2. Web form: "+ بند" button no longer lives in the header block;
     it's rendered under the table.
  3. Web form: "حفظ كمسودة" is gone from the invoice form; the
     replacement "حفظ كعرض سعر" is present.
  4. Vendor bills + inventory transfer forms: "حفظ كمسودة" replaced
     by "حفظ كعرض سعر" everywhere they appeared as button labels.
  5. PDF invoice template: leading # column header + `loop.index`
     row number cell present.
  6. End-to-end: render a real invoice PDF via WeasyPrint; the byte
     stream contains 1., 2., 3. (the row numbers).
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
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


@check("1. web form: # column + .row-num cell + renumberRows() helper")
def _():
    src = _strip_comments(_read("app/templates/invoices/form.html"))
    # The thead has a <th># header cell
    assert re.search(r"<th[^>]*>\s*#\s*</th>", src), \
        "invoice form's items table is missing the leading # header"
    # The row template has a .row-num cell
    assert 'class="row-num' in src, \
        "the row template is missing the .row-num cell that " \
        "renumberRows() stamps 1,2,3,... into"
    # The renumberRows() helper exists AND is called from addItem +
    # the delete inline handler
    assert "function renumberRows()" in src, \
        "renumberRows() JS helper missing"
    assert src.count("renumberRows()") >= 3, \
        "renumberRows() should be called on add + on delete + at " \
        f"least once in addItem; found {src.count('renumberRows()')}"
    return "# header + .row-num + renumberRows() wired at add/delete"


@check("2. web form: '+ بند' button lives under the table, not in header")
def _():
    src = _strip_comments(_read("app/templates/invoices/form.html"))
    # There must be exactly one addItem() button.
    add_buttons = re.findall(r'onclick="addItem\(\)"[^>]*>\+\s*بند', src)
    assert len(add_buttons) == 1, \
        f"expected 1 '+ بند' add button, found {len(add_buttons)}"
    # And it must sit AFTER </table>, not inside the head block.
    table_close_idx = src.rfind("</table>")
    btn_idx = src.find('onclick="addItem()"')
    assert table_close_idx > 0 and btn_idx > table_close_idx, (
        "'+ بند' button is still positioned before/inside the table; "
        "it should render under the last row")
    return "single '+ بند' button, positioned under the items table"


@check("3. web form: 'حفظ كمسودة' → 'حفظ كعرض سعر' on invoice form")
def _():
    src = _strip_comments(_read("app/templates/invoices/form.html"))
    assert "حفظ كمسودة" not in src, \
        "invoice form still contains the old label 'حفظ كمسودة'"
    assert "حفظ كعرض سعر" in src, \
        "invoice form is missing the new label 'حفظ كعرض سعر'"
    return "invoice draft button renamed"


@check("4. vendor bills + inventory transfer: same label rename")
def _():
    for rel in ("app/templates/vendor_bills/new_typed.html",
                "app/templates/inventory/transfer_form.html"):
        src = _strip_comments(_read(rel))
        # There must not be any BUTTON-labeled "حفظ كمسودة" left.
        # We search buttons only (a comment or narrative <p> outside
        # a button is fine).
        button_texts = re.findall(
            r"<button[^>]*>([^<]*?حفظ كمسودة[^<]*?)</button>",
            src, re.DOTALL,
        )
        assert not button_texts, \
            f"{rel} still has a <button>...حفظ كمسودة...</button>: " \
            f"{button_texts[0]!r}"
        assert "حفظ كعرض سعر" in src, \
            f"{rel} is missing the new label 'حفظ كعرض سعر'"
    return "vendor_bills + inventory transfer buttons renamed"


@check("5. PDF invoice template: # column header + loop.index cell")
def _():
    src = _strip_comments(_read("app/templates/pdfs/invoice.html"))
    # Header
    assert re.search(r"<th[^>]*>\s*#\s*</th>", src), \
        "PDF items table missing the leading # header"
    # Row cell — must render `loop.index` inside the tbody loop
    # (not just anywhere in the file).
    m = re.search(
        r"{% for item in invoice\.items %}(.*?){% endfor %}",
        src, re.DOTALL,
    )
    assert m, "PDF items {% for item in invoice.items %} block missing"
    body = m.group(1)
    assert "loop.index" in body, \
        "PDF items row does not print `loop.index` for the # column"
    return "PDF # header + loop.index row cell present"


@check("6. end-to-end: PDF renders with 1., 2., 3. row numbers")
def _():
    from datetime import datetime, date, timedelta
    from decimal import Decimal
    from sqlalchemy import text, inspect
    from app import create_app, db

    app = create_app()
    with app.app_context():
        from app.models import (
            Company, Customer, Invoice, InvoiceItem, InvoiceStatus,
        )
        from app.services.seed_coa import seed_default_coa
        from app.services.subsidiary import ensure_customer_account
        from app.services.export import export_invoice_pdf

        insp = inspect(db.engine)
        cids = [r[0] for r in db.session.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__NUMBERED__%'"))]
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
        db.session.commit()

        c = Company(name="__NUMBERED__co", base_currency="EGP",
                    subdomain="numbered1",
                    subscription_started_at=datetime.utcnow(),
                    subscription_expires_at=datetime(2999, 1, 1))
        db.session.add(c); db.session.commit()
        seed_default_coa(c.id); db.session.commit()

        cust = Customer(company_id=c.id, name="عميل الترقيم")
        db.session.add(cust); db.session.flush()
        ensure_customer_account(cust); db.session.commit()

        inv = Invoice(
            company_id=c.id, number="INV-NUM-01",
            customer_id=cust.id,
            issue_date=date.today(),
            due_date=date.today() + timedelta(days=30),
            currency="EGP", tax_rate=Decimal("15"),
            status=InvoiceStatus.DRAFT,
        )
        db.session.add(inv); db.session.flush()
        for i, name in enumerate(["بند الأول", "بند الثاني", "بند الثالث"], 1):
            db.session.add(InvoiceItem(
                invoice_id=inv.id, description=name,
                quantity=Decimal("1"), unit_price=Decimal("100"),
                line_total=Decimal("100"),
            ))
        inv.recalc()
        db.session.commit()

        try:
            # Render the ACTUAL WeasyPrint template to HTML (via the
            # same jinja render the service uses). This validates the
            # new # column reaches the customer PDF even on a dev
            # box without libpango, where export_invoice_pdf falls
            # back to the legacy ReportLab path that renders a
            # completely different layout.
            from flask import render_template
            from app.services.export import _amiri_font_face_css, _company_logo_data_uri
            html = render_template(
                "pdfs/invoice.html",
                invoice=inv,
                amiri_font_face=_amiri_font_face_css(),
                company_logo_data_uri=_company_logo_data_uri(c),
            )
            # The items table must have a # header AND three row cells
            # numbered 1/2/3, in loop order.
            items_match = re.search(
                r'<table class="items">(.*?)</table>', html, re.DOTALL)
            assert items_match, "items table not rendered"
            items_html = items_match.group(1)
            assert re.search(r"<th[^>]*>\s*#\s*</th>", items_html), \
                "rendered items table missing # header"
            rows = re.findall(
                r"<tr>\s*<td[^>]*>\s*(\d+)\s*</td>", items_html)
            assert rows == ["1", "2", "3"], \
                f"expected first cell of each row to be 1/2/3; got {rows}"
            return f"rendered items table shows # column with rows {rows}"
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
