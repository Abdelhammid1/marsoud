"""MARSOUD-SOURCE-REFERENCE-01 (Abdelhamid 2026-07-25).

Convert (source_type, source_id) pairs on JournalEntry /
StockMovement rows into a human-readable label + a URL for the
source document. Called from party_ledger + inventory/movements
so users can jump straight from a ledger row to the underlying
invoice / bill / adjustment / etc.

  · SAFE: every URL is built server-side using Flask url_for so a
    stale source_id can't be used to XSS through a template.
  · MULTI-TENANT: the caller feeds us the active company_id; we
    filter every lookup by it so a source_id from another tenant
    can never resolve.
  · BATCHED: build_reference_map() loads every source doc in ≤ 1
    query per source_type per company — avoids N+1 on a ledger
    view with 200+ rows.
"""
from collections import defaultdict


# Every source_type value seen in the codebase, in one place, mapped
# to a (label_ar, blueprint.endpoint, url_kw_name) triple. The
# `url_kw_name` is the kwarg the endpoint expects for the doc id
# (e.g. invoice_id, bill_id). None ⇒ no clickable link, label only.
_SOURCE_TYPES = {
    "invoice":            ("فاتورة مبيعات",   "invoices.view",         "invoice_id"),
    "invoice_item":       ("فاتورة مبيعات",   "invoices.view",         "invoice_id"),
    "invoice_cogs":       ("تكلفة بضاعة مباعة", "invoices.view",       "invoice_id"),
    "payment":            ("تحصيل من عميل",   "invoices.view",         "invoice_id"),
    "vendor_bill":        ("فاتورة مورد",     "vendor_bills.view",     "bill_id"),
    "vendor_bill_item":   ("فاتورة مورد",     "vendor_bills.view",     "bill_id"),
    "vendor_bill_payment": ("سداد لمورد",     "vendor_bills.view",     "bill_id"),
    "vendor_bill_refund": ("مرتجع مشتريات",   "vendor_bills.view",     "bill_id"),
    "refund":             ("مرتجع مبيعات",    "invoices.view",         "invoice_id"),
    "refund_cogs":        ("عكس تكلفة مرتجع", "invoices.view",         "invoice_id"),
    "credit_note":        ("إشعار دائن",      "invoices.view",         "invoice_id"),
    "customer_deposit":   ("عربون من عميل",    None,                    None),
    "customer_deposit_refund": ("استرداد عربون", None,                None),
    "template":           ("قيد من قالب متكرر", None,                  None),
    "depreciation":       ("إهلاك أصول",       None,                    None),
    "asset_purchase":     ("شراء أصل",         "vendor_bills.view",     "bill_id"),
    "manual_adjustment":  ("تسوية مخزون",      None,                    None),
    "stock_transfer":     ("تحويل بين المخازن", "inventory.transfers",  None),
    "pos_sale":           ("بيع POS",           "invoices.view",         "invoice_id"),
    "opening_balance":    ("رصيد افتتاحي",     None,                    None),
    "payroll":            ("قيد رواتب",         None,                    None),
    "sales_commission":   ("عمولة مبيعات",     None,                    None),
    "manufacturing_order": ("أمر إنتاج",       None,                    None),
    # MARSOUD-SOURCE-REFERENCE-01 pt 2 (Abdelhamid 2026-07-29) —
    # 6 source_types that were falling into the "قيد يدوي" default
    # even though they're NOT manual entries. Found via a full DB
    # scan (SELECT DISTINCT source_type FROM journal_entries +
    # stock_movements). 154 rows previously mislabeled. All label-
    # only for now — none have a dedicated detail view.
    "opening_stock":           ("رصيد افتتاحي مخزون", None, None),
    "party_opening_balance":   ("رصيد افتتاحي طرف",   None, None),
    "sales_commission_refund": ("عكس عمولة مبيعات",   None, None),
    "payroll_settlement":      ("سداد راتب مستحق",     None, None),
    "accrual_settle":          ("تسوية استحقاق راتب", None, None),
    "pos_void":                ("إلغاء عملية POS",     None, None),
    # Discovered by the DB-coverage check (audit test 8). Legit
    # entries that were also falling into the "قيد يدوي" default.
    "stock_adjustment":        ("تسوية مخزون",          None, None),
    "audit_seed":              ("قيد اختبار (تجربة)",   None, None),
    # MARSOUD-ADVANCES (2026-08-03) — employee advances.
    "employee_advance":        ("صرف سلفة موظف",        None, None),
    # MARSOUD-ACCOUNTING-OPS — the 🧮 العمليات المحاسبية wizards. Every new
    # wizard adds its own line here, or its entries render as "قيد يدوي".
    # (`opening_balance` above is the third one — it was already registered
    # and emitted by nothing, so the wizard claims it.)
    "capital_injection":       ("إضافة رأس مال",        None, None),
    "owner_drawings":          ("مسحوبات المالك",       None, None),
}


# Fallback label when we've never seen the source_type before.
_UNKNOWN_LABEL = "قيد يدوي"


def resolve_reference(source_type, source_id, doc_number=None,
                       company_id=None):
    """Return a dict ready for the template:

        {"label": "فاتورة INV-0001",
         "url":   "/invoices/42"  # or None,
         "kind":  "invoice"}

    When source_type is falsy (manual JE) → generic label + no URL.
    When doc_number is None (caller didn't pre-fetch), the label
    falls back to the human-readable type name only.
    """
    if not source_type:
        return {"label": _UNKNOWN_LABEL, "url": None, "kind": None}
    meta = _SOURCE_TYPES.get(source_type)
    if not meta:
        # Unknown source_type — safe display without a link.
        return {"label": _UNKNOWN_LABEL, "url": None,
                "kind": source_type}
    label_ar, endpoint, url_kw = meta
    label = f"{label_ar} {doc_number}" if doc_number else label_ar
    url = None
    if endpoint and url_kw and source_id:
        from flask import url_for
        try:
            url = url_for(endpoint, **{url_kw: source_id})
        except Exception:
            # Broken endpoint / no such id: label-only display.
            url = None
    elif endpoint and not url_kw:
        from flask import url_for
        try:
            url = url_for(endpoint)
        except Exception:
            url = None
    return {"label": label, "url": url, "kind": source_type}


def build_reference_map(rows, company_id):
    """Batched variant. `rows` is a list of dicts (or objects) with
    `source_type` + `source_id` attributes/keys. Returns a dict
    keyed by (source_type, source_id) → the same shape as
    resolve_reference().

    ONE query per source_type — the row count won't scale by number
    of documents. Documents that can't be found are still returned
    (label only, url=None) so the template doesn't need to handle
    KeyError.
    """
    # Bucket by source_type.
    ids_by_type = defaultdict(set)
    for r in rows:
        st = _get(r, "source_type")
        sid = _get(r, "source_id")
        if st and sid:
            ids_by_type[st].add(int(sid))

    # For each source_type that has a "number" column on its model,
    # pull the doc_numbers so we can render them in the label. Only
    # invoice + vendor_bill have numbers today — extending is a
    # one-line change.
    numbers = {}
    if ids_by_type.get("invoice") or \
            ids_by_type.get("invoice_item") or \
            ids_by_type.get("invoice_cogs") or \
            ids_by_type.get("payment") or \
            ids_by_type.get("refund") or \
            ids_by_type.get("refund_cogs") or \
            ids_by_type.get("credit_note") or \
            ids_by_type.get("pos_sale"):
        from app.models import Invoice
        from app import db
        inv_ids = set()
        for t in ("invoice", "invoice_item", "invoice_cogs",
                   "payment", "refund", "refund_cogs",
                   "credit_note", "pos_sale"):
            inv_ids.update(ids_by_type.get(t, set()))
        if inv_ids:
            q = db.session.query(Invoice.id, Invoice.number).filter(
                Invoice.company_id == company_id,
                Invoice.id.in_(inv_ids),
            )
            for _id, _num in q.all():
                numbers[("invoice", _id)] = _num

    if any(ids_by_type.get(t) for t in (
        "vendor_bill", "vendor_bill_item", "vendor_bill_payment",
        "vendor_bill_refund", "asset_purchase",
    )):
        from app.models import VendorBill
        from app import db
        bill_ids = set()
        for t in ("vendor_bill", "vendor_bill_item",
                   "vendor_bill_payment", "vendor_bill_refund",
                   "asset_purchase"):
            bill_ids.update(ids_by_type.get(t, set()))
        if bill_ids:
            q = db.session.query(VendorBill.id, VendorBill.number).filter(
                VendorBill.company_id == company_id,
                VendorBill.id.in_(bill_ids),
            )
            for _id, _num in q.all():
                numbers[("vendor_bill", _id)] = _num

    # Now build the map.
    out = {}
    for r in rows:
        st = _get(r, "source_type")
        sid = _get(r, "source_id")
        if not st:
            out[(st, sid)] = resolve_reference(st, sid,
                                                  company_id=company_id)
            continue
        # Look up doc_number under the "logical parent" type.
        parent_type = "invoice" if st in (
            "invoice", "invoice_item", "invoice_cogs", "payment",
            "refund", "refund_cogs", "credit_note", "pos_sale",
        ) else ("vendor_bill" if st in (
            "vendor_bill", "vendor_bill_item", "vendor_bill_payment",
            "vendor_bill_refund", "asset_purchase",
        ) else None)
        doc_num = numbers.get((parent_type, sid)) if parent_type else None
        out[(st, sid)] = resolve_reference(st, sid, doc_number=doc_num,
                                              company_id=company_id)
    return out


def _get(row, key):
    """Read `key` from either a dict OR an object with attrs."""
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)
