"""MARSOUD-SOURCE-REFERENCE-01 (Abdelhamid 2026-07-25).

Convert (source_type, source_id) pairs on JournalEntry /
StockMovement rows into a human-readable label + a URL for the
source document. Called from party_ledger + inventory/movements +
the account ledger so users can jump straight from a ledger row to
the underlying invoice / bill / adjustment / etc.

THE ONLY MAP. MARSOUD-SOURCE-LABEL-UNIFY (2026-08-04) — there used
to be a second one, SOURCE_LABELS_AR in app/routes/accounts.py, and
every new source_type got registered here and forgotten there. It
happened three times: 6 types (154 mislabeled rows), then
stock_adjustment/audit_seed, then capital_injection /
owner_drawings / employee_advance, which the account ledger printed
as raw English in an RTL Arabic table. Registering a new type in
`_SOURCE_TYPES` below is now the whole job — the account ledger,
party statement and stock movements all read from here. Do not add
a second map; tests/audit_source_label_unification.py fails if one
appears.

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
    # MARSOUD-ASSET-DISPOSAL-01 (2026-08-07) — source_id is the
    # asset.id (not a bill), so the label links back to the asset
    # view where the disposal panel becomes a read-only banner.
    "asset_disposal":     ("شطب أصل",          "assets.view",           "asset_id"),
    "manual_adjustment":  ("تسوية مخزون",      None,                    None),
    "stock_transfer":     ("تحويل بين المخازن", "inventory.transfers",  None),
    "pos_sale":           ("بيع من نقطة البيع", "invoices.view",         "invoice_id"),
    "opening_balance":    ("رصيد افتتاحي",     None,                    None),
    "payroll":            ("قيد رواتب",         "payroll.index",         None),
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
    # MARSOUD-SOURCE-LABEL-UNIFY — "POS" is Latin text in front of an
    # Arabic-speaking user, which is what the ticket's «مفيش أي نص
    # إنجليزي» rules out. Both POS labels now say نقطة البيع, so the
    # coverage audit can demand pure Arabic instead of carrying an
    # exemption list.
    "pos_void":                ("إلغاء عملية بيع",     None, None),
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
    # MARSOUD-OPS-FOUNDATION (2026-08-05)
    "money_transfer":          ("تحويل بين الحسابات",    None, None),
    "open_item":               ("إثبات التزام",          None, None),
    "open_item_settle":        ("سداد التزام",           None, None),
    "owner_drawings":          ("مسحوبات المالك",       None, None),
    # MARSOUD-SOURCE-LABEL-UNIFY (2026-08-04) — folding
    # accounts.SOURCE_LABELS_AR in here surfaced source_types that were
    # written by services but registered in NEITHER map, so they showed
    # as raw English on the account ledger and "قيد يدوي" everywhere
    # else. Found by scanning every post_journal / apply_stock_movement
    # call site, not by scanning one DB.
    # source_id is the bill id (services/vendor_bills.py:358), same as
    # vendor_bill_payment — so it links to the bill, not to a list.
    "vendor_payment":          ("سداد لمورد",           "vendor_bills.view", "bill_id"),
    "work_order":              ("أمر إنتاج",            None, None),
    "work_order_consumption":  ("صرف مكونات إنتاج",     None, None),
    "work_order_receipt":      ("استلام إنتاج تام",     None, None),
    # Legacy keys that only ever lived in accounts.SOURCE_LABELS_AR.
    # Nothing writes them today, but historical rows may carry them and
    # dropping the label would be a regression on old ledgers.
    "asset":                   ("أصل ثابت",             None, None),
    "stock_receipt":           ("استلام مخزون",         None, None),
    # MARSOUD-OPS-HUB-EXPANSION-01 (2026-08-08) — 18 new wizards.
    # Registered at ship-time so no entry ever falls back to "قيد
    # يدوي" in the journal viewer. Every wizard's source_type is
    # the operation key with underscores. All label-only for now —
    # none have a dedicated detail page yet.
    "loan_short_receive":      ("استلام قرض قصير الأجل", None, None),
    "loan_long_receive":       ("استلام قرض طويل الأجل", None, None),
    "loan_installment_paid":   ("سداد قسط قرض",          None, None),
    "vat_net_payment":         ("سداد صافي ضريبة القيمة المضافة", None, None),
    "year_end_close":          ("إقفال نهاية السنة",     None, None),
    "legal_reserve_allocation": ("تخصيص احتياطي قانوني", None, None),
    "eosb_provision":          ("مخصص مكافأة نهاية الخدمة", None, None),
    "eosb_payment":            ("صرف مكافأة نهاية الخدمة", None, None),
    "deposit_received":        ("استلام أمانة من طرف",   None, None),
    "deposit_returned":        ("رد أمانة لطرف",         None, None),
    "note_receivable_received": ("استلام ورقة قبض",      None, None),
    "note_payable_issued":     ("إصدار ورقة دفع",        None, None),
    "bad_debt_writeoff":       ("شطب دين معدوم",         None, None),
    "cash_count_adjustment":   ("تسوية الصندوق",         None, None),
    "general_adjustment":      ("تسوية حساب عام",        None, None),
    # accrued-revenue + dividends piggyback on the existing
    # open_item / open_item_settle labels — they only differ by
    # item_kind, which is displayed inside the open_item detail
    # view. No new source_type needed for those four wizards.
}


# Fallback label when we've never seen the source_type before. Public:
# callers that render their own no-reference cell must use THIS, not a
# hardcoded copy — three copies of the string is how the maps drifted.
UNKNOWN_LABEL = "قيد يدوي"
_UNKNOWN_LABEL = UNKNOWN_LABEL  # backwards-compatible alias


# Which source_types hang off which parent document, for the batched
# doc_number lookup below. These were spelled out three times each
# inside build_reference_map — the same "two copies drift apart" bug
# this module exists to prevent, one scope down.
_INVOICE_TYPES = (
    "invoice", "invoice_item", "invoice_cogs", "payment",
    "refund", "refund_cogs", "credit_note", "pos_sale",
)
_BILL_TYPES = (
    "vendor_bill", "vendor_bill_item", "vendor_bill_payment",
    "vendor_bill_refund", "vendor_payment", "asset_purchase",
)


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
    if any(ids_by_type.get(t) for t in _INVOICE_TYPES):
        from app.models import Invoice
        from app import db
        inv_ids = set()
        for t in _INVOICE_TYPES:
            inv_ids.update(ids_by_type.get(t, set()))
        if inv_ids:
            q = db.session.query(Invoice.id, Invoice.number).filter(
                Invoice.company_id == company_id,
                Invoice.id.in_(inv_ids),
            )
            for _id, _num in q.all():
                numbers[("invoice", _id)] = _num

    if any(ids_by_type.get(t) for t in _BILL_TYPES):
        from app.models import VendorBill
        from app import db
        bill_ids = set()
        for t in _BILL_TYPES:
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
        parent_type = "invoice" if st in _INVOICE_TYPES else (
            "vendor_bill" if st in _BILL_TYPES else None)
        doc_num = numbers.get((parent_type, sid)) if parent_type else None
        out[(st, sid)] = resolve_reference(st, sid, doc_number=doc_num,
                                              company_id=company_id)
    return out


def _get(row, key):
    """Read `key` from either a dict OR an object with attrs."""
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)
