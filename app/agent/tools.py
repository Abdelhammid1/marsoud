"""Tools exposed to the AI accountant agent (Claude tool use)."""
from datetime import datetime, date, timedelta
from app import db
from app.models import (
    Account, Customer, Vendor, Invoice, InvoiceItem, InvoiceStatus,
    Employee, FixedAsset, JournalEntry, Company,
)
from app.services.ledger import post_journal, get_account_by_code, LedgerError
from app.services.invoicing import post_invoice_to_ledger, record_payment
from app.services.reports import balance_sheet, income_statement, cash_flow, aging_report, dashboard_metrics


# MARSOUD-AGENT-CONTEXT-01 (2026-08-06) — every "today" the agent
# tools compute for a filter default MUST be today in the COMPANY's
# timezone, not the server's. Otherwise a call at 01:30 Riyadh
# (22:30 UTC the day before) returns yesterday's invoices under the
# label "اليوم". The context block in the prompt now names today
# correctly; these helpers make sure the tool defaults match.
def _today(company=None):
    """today() in the company's timezone if company is passed, else
    server-local. Always pass company from execute_tool()."""
    if company is None:
        return date.today()
    from app.services.time import today_in_company_tz
    return today_in_company_tz(company)


TOOL_SCHEMAS = [
    {
        "name": "list_accounts",
        "description": "اعرض شجرة الحسابات (Chart of Accounts) للشركة الحالية. استخدمها للبحث عن account_id قبل إنشاء قيد.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "نص للبحث في اسم أو كود الحساب (اختياري)"}
            },
        },
    },
    {
        "name": "list_customers",
        "description": "اعرض كل العملاء في الشركة الحالية مع أرصدتهم.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_customer",
        "description": "أضف عميل جديد.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "tax_number": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "create_journal_entry",
        "description": "سجّل قيد محاسبي مزدوج. مجموع المدين يجب أن يساوي مجموع الدائن.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "وصف القيد"},
                "entry_date": {"type": "string", "description": "تاريخ القيد بصيغة YYYY-MM-DD (افتراضي اليوم)"},
                "reference": {"type": "string", "description": "رقم مرجعي (اختياري)"},
                "lines": {
                    "type": "array",
                    "description": "سطور القيد، كل سطر فيه account_id ومبلغ مدين أو دائن",
                    "items": {
                        "type": "object",
                        "properties": {
                            "account_id": {"type": "integer"},
                            "debit": {"type": "number"},
                            "credit": {"type": "number"},
                            "memo": {"type": "string"},
                        },
                        "required": ["account_id"],
                    },
                },
            },
            "required": ["description", "lines"],
        },
    },
    {
        "name": "create_invoice",
        "description": "أنشئ فاتورة جديدة للعميل وأرسلها (تسجل قيد محاسبي تلقائياً).",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "integer"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "quantity": {"type": "number"},
                            "unit_price": {"type": "number"},
                        },
                        "required": ["description", "quantity", "unit_price"],
                    },
                },
                "due_days": {"type": "integer", "description": "أيام الاستحقاق من اليوم (افتراضي 30)"},
                "tax_rate": {"type": "number", "description": "نسبة الضريبة % (افتراضي حسب الشركة)"},
                "send": {"type": "boolean", "description": "إرسال فوري وتسجيل قيد (افتراضي true)"},
            },
            "required": ["customer_id", "items"],
        },
    },
    {
        "name": "record_invoice_payment",
        "description": "سجّل دفعة على فاتورة.",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": {"type": "integer"},
                "amount": {"type": "number"},
                "method": {"type": "string", "enum": ["cash", "bank"], "description": "افتراضي cash"},
            },
            "required": ["invoice_id", "amount"],
        },
    },
    {
        "name": "get_invoice",
        "description": "اعرض تفاصيل فاتورة بما فيها الحالة والرصيد.",
        "input_schema": {
            "type": "object",
            "properties": {"invoice_id": {"type": "integer"}},
            "required": ["invoice_id"],
        },
    },
    {
        "name": "list_invoices",
        "description": "اعرض قائمة الفواتير مع فلاتر اختيارية (فترة تاريخ، حالة، عميل)، ويرجع أيضاً عدد الفواتير وإجمالي المبالغ. استخدم هذه الأداة لأي سؤال عن عدد الفواتير أو إجمالي المبيعات في فترة معينة (مثل: كام فاتورة النهاردة، إجمالي مبيعات الأسبوع ده، الفواتير المتأخرة). ملاحظة مهمة: الفواتير المسودة (DRAFT) والملغية (CANCELLED) والمعدومة (VOIDED) مستبعدة تلقائياً من العدد والإجمالي لأنها لا تمثل مبيعات فعلية، وهذا يطابق ما تعرضه التقارير المالية. لعرضها استخدم فلتر status صراحةً أو include_all_statuses.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD (افتراضي اليوم)"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD (افتراضي اليوم)"},
                "status": {
                    "type": "string",
                    "enum": ["DRAFT", "SENT", "PARTIALLY_PAID", "PAID", "OVERDUE", "CANCELLED", "REFUNDED", "PARTIALLY_REFUNDED", "VOIDED"],
                    "description": "فلتر بالحالة (اختياري)",
                },
                "customer_id": {"type": "integer", "description": "فلتر بعميل معين (اختياري)"},
                "limit": {"type": "integer", "description": "أقصى عدد فواتير تُرجع بالتفصيل، افتراضي 20 (العدد والإجمالي يشملان كل الفواتير المطابقة بغض النظر عن هذا الحد)"},
                "include_all_statuses": {"type": "boolean", "description": "لو true، يشمل الفواتير المسودة والملغية والمعدومة في النتيجة. الافتراضي false."},
            },
        },
    },
    {
        "name": "run_report",
        "description": "شغّل تقرير مالي.",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["balance_sheet", "income_statement", "cash_flow", "aging", "dashboard"],
                },
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD (افتراضي اليوم)"},
            },
            "required": ["type"],
        },
    },
    {
        "name": "explain_concept",
        "description": "اشرح مفهوم محاسبي للمستخدم. استخدمها لو المستخدم سأل سؤال نظري.",
        "input_schema": {
            "type": "object",
            "properties": {"concept": {"type": "string"}},
            "required": ["concept"],
        },
    },
    {
        "name": "get_stock_level",
        "description": "اعرف كام عندك من منتج معين دلوقتي — يرجع الأرصدة لكل مخزن والإجمالي.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "اسم المنتج أو SKU أو باركود"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_low_stock",
        "description": "الأصناف اللي قاربت تخلص — الكمية الحالية أقل من حد الطلب.",
        "input_schema": {
            "type": "object",
            "properties": {
                "multiplier": {"type": "number", "description": "ضاعف حد الطلب (1.0 = العتبة الحقيقية، 2.0 = تحذير مبكر)"},
            },
        },
    },
    {
        "name": "get_product_profitability",
        "description": "كسبنا كام من منتج في فترة — مبيعات ناقص تكلفة البضاعة المباعة.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "اسم المنتج أو SKU"},
                "from_date": {"type": "string", "description": "YYYY-MM-DD"},
                "to_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_cashier_sales",
        "description": "مين باع إيه في يوم معين — إجمالي مبيعات كل كاشير وأوردراته.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD (افتراضي اليوم)"},
            },
        },
    },
    {
        "name": "get_top_products",
        "description": "أحسن المنتجات مبيعاً في فترة.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string"},
                "to_date": {"type": "string"},
                "limit": {"type": "integer", "description": "افتراضي 10"},
            },
        },
    },
    {
        "name": "get_open_shifts",
        "description": "مين عنده وردية مفتوحة دلوقتي + الكاش المتوقع في كل درج.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_shift_summary",
        "description": "ملخص وردية معينة — أوردرات، صافي، الكاش المتوقع، الفرق.",
        "input_schema": {
            "type": "object",
            "properties": {"shift_id": {"type": "integer"}},
            "required": ["shift_id"],
        },
    },
    {
        "name": "transfer_history",
        "description": "تاريخ تحويلات صنف بين المخازن.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "SKU أو اسم المنتج"},
                "limit": {"type": "integer", "description": "افتراضي 20"},
            },
            "required": ["query"],
        },
    },
]


def _parse_date(s, default=None, company=None):
    """Parse a date string. Missing/empty falls back to `default`, or
    to today-in-company-tz if `company` is provided, else server today.

    Old signature (s, default) still works — the third parameter is
    kwarg-only in the intent though not enforced (Python-style), and
    every existing external caller passes only the first two args."""
    if not s:
        return default or _today(company)
    if isinstance(s, date):
        return s
    return datetime.strptime(s, "%Y-%m-%d").date()


def execute_tool(name, args, company_id, user_id):
    """Dispatch a tool call. Returns a JSON-serializable result dict.

    MARSOUD-AGENT-CONTEXT-01 (2026-08-06) — resolve the Company ONCE
    at the top so every date default below uses the tenant's local
    timezone instead of server-UTC. Falling back to None (which
    produces server-today) preserves behaviour for the edge case of
    a tool call with company_id=0/None.

    MARSOUD-AGENT-SAFETY-03 (2026-08-06) — WRITE tools (create_customer,
    create_journal_entry, create_invoice, record_payment) return a
    PROPOSAL by default; the actual write happens when the user
    confirms via /agent/proposal/<id>/execute, which re-invokes this
    function with args["_confirmed_proposal_id"] set. The propose-vs-
    execute branch is entered at the top of each write tool's block.
    Read tools bypass the check entirely and run instantly. The
    require_confirmation super-admin toggle can disable the propose
    path for tenants who want the old behaviour.
    """
    company = db.session.get(Company, company_id) if company_id else None

    # MARSOUD-AGENT-SAFETY-03 — proposal short-circuit. Only for WRITE
    # tools, only when confirmation is on, only when the caller did
    # not already pass the confirmed-proposal marker.
    from app.services.agent_safety import (
        WRITE_TOOL_NAMES, require_confirmation_enabled,
        create_proposal, summarize_write_call,
    )
    if (name in WRITE_TOOL_NAMES
            and require_confirmation_enabled()
            and not args.get("_confirmed_proposal_id")):
        summary, amount = summarize_write_call(name, args, company)
        return create_proposal(
            tool_name=name, args=args,
            company_id=company_id, user_id=user_id,
            summary_ar=summary, amount_readable=amount)
    try:
        if name == "list_accounts":
            q = Account.query.filter_by(company_id=company_id, is_active=True)
            search = args.get("search", "").strip()
            if search:
                like = f"%{search}%"
                q = q.filter(db.or_(Account.code.ilike(like), Account.name.ilike(like), Account.name_ar.ilike(like)))
            accounts = q.order_by(Account.code).limit(50).all()
            return {
                "accounts": [
                    {"id": a.id, "code": a.code, "name": a.name, "name_ar": a.name_ar, "type": a.type.value, "balance": round(a.balance, 2)}
                    for a in accounts
                ]
            }

        if name == "list_customers":
            customers = Customer.query.filter_by(company_id=company_id, is_active=True).all()
            return {
                "customers": [
                    {"id": c.id, "name": c.name, "email": c.email, "phone": c.phone, "balance": round(c.balance, 2)}
                    for c in customers
                ]
            }

        if name == "create_customer":
            c = Customer(
                company_id=company_id,
                name=args["name"],
                email=args.get("email", ""),
                phone=args.get("phone", ""),
                tax_number=args.get("tax_number", ""),
            )
            db.session.add(c)
            db.session.commit()
            return {"ok": True, "customer_id": c.id, "name": c.name}

        if name == "create_journal_entry":
            entry = post_journal(
                company_id=company_id,
                description=args["description"],
                lines=args["lines"],
                entry_date=_parse_date(args.get("entry_date")),
                reference=args.get("reference"),
                created_by=user_id,
            )
            return {
                "ok": True,
                "entry_id": entry.id,
                "total_debit": entry.total_debit,
                "total_credit": entry.total_credit,
            }

        if name == "create_invoice":
            # `company` already resolved at the top of execute_tool.
            # A local `from app.models import Company` used to live
            # here — after MARSOUD-AGENT-CONTEXT-01 hoisted the same
            # import to module-level, the inner one turned every
            # reference to `Company` in this function into a local
            # (Python's scoping quirk), which UnboundLocalError'd on
            # the new hoisted resolve. Removed.
            from app.routes.invoices import _next_number

            # MARSOUD-AGENT-SAFETY-03 (2026-08-06) — validate the
            # customer belongs to THIS company. The pre-ticket code
            # used args["customer_id"] verbatim, which was the exact
            # cross-tenant hole other tools (create_customer,
            # record_payment) already guarded against. The prompt
            # cannot be trusted to only pass same-tenant IDs; the
            # model has visibility of numbers from earlier turns.
            cust = db.session.get(Customer, args.get("customer_id"))
            if not cust or cust.company_id != company_id:
                return {"error": "العميل غير موجود في هذه الشركة"}

            due_days = args.get("due_days", 30)
            # MARSOUD-INVOICE-TAX-ZERO (Batch 9 Ticket 1, 2026-08-01)
            # — respect a saved vat_rate of 0 (falsy in Python's
            # `or`). Fall through to 0 only when the column is None.
            tax_rate = args.get(
                "tax_rate",
                float(company.vat_rate
                       if company.vat_rate is not None else 0))
            invoice = Invoice(
                company_id=company_id,
                number=_next_number(company_id),
                customer_id=cust.id,
                issue_date=_today(company),
                due_date=_today(company) + timedelta(days=due_days),
                currency=company.base_currency,
                tax_rate=tax_rate,
                status=InvoiceStatus.DRAFT,
                # MARSOUD-INVOICE-CREATOR — the operator running the
                # agent is recorded as the creator.
                created_by_id=user_id,
            )
            db.session.add(invoice)
            db.session.flush()
            for it in args["items"]:
                item = InvoiceItem(
                    invoice_id=invoice.id,
                    company_id=invoice.company_id,
                    description=it["description"],
                    quantity=it["quantity"],
                    unit_price=it["unit_price"],
                )
                db.session.add(item)
            db.session.flush()
            invoice.items = InvoiceItem.query.filter_by(invoice_id=invoice.id).all()
            invoice.recalc()
            if args.get("send", True):
                invoice.status = InvoiceStatus.SENT
                post_invoice_to_ledger(invoice, created_by=user_id)
            db.session.commit()
            return {
                "ok": True,
                "invoice_id": invoice.id,
                "number": invoice.number,
                "total": float(invoice.total),
                "tax_amount": float(invoice.tax_amount),
                "status": invoice.status.value,
            }

        if name == "record_invoice_payment":
            inv = db.session.get(Invoice, args["invoice_id"])
            if not inv or inv.company_id != company_id:
                return {"error": "الفاتورة غير موجودة"}
            pmt = record_payment(inv, args["amount"], method=args.get("method", "cash"), created_by=user_id)
            return {
                "ok": True,
                "payment_id": pmt.id,
                "invoice_status": inv.status.value,
                "remaining_balance": inv.balance,
            }

        if name == "get_invoice":
            inv = db.session.get(Invoice, args["invoice_id"])
            if not inv or inv.company_id != company_id:
                return {"error": "غير موجود"}
            return {
                "invoice_id": inv.id,
                "number": inv.number,
                "customer": inv.customer.name,
                "issue_date": str(inv.issue_date),
                "due_date": str(inv.due_date),
                "subtotal": float(inv.subtotal),
                "tax_amount": float(inv.tax_amount),
                "total": float(inv.total),
                "paid_amount": float(inv.paid_amount or 0),
                "balance": inv.balance,
                "status": inv.status.value,
                "items": [
                    {"description": i.description, "quantity": float(i.quantity), "unit_price": float(i.unit_price)}
                    for i in inv.items
                ],
            }

        if name == "list_invoices":
            from app.models import InvoiceStatus as _InvStatus
            start = _parse_date(args.get("start_date"), _today(company))
            end = _parse_date(args.get("end_date"), _today(company))
            limit = args.get("limit", 20)
            q = Invoice.query.filter(
                Invoice.company_id == company_id,
                Invoice.issue_date >= start,
                Invoice.issue_date <= end,
            )
            status = args.get("status")
            excluded = []
            if status:
                q = q.filter(Invoice.status == _InvStatus(status))
            elif not args.get("include_all_statuses"):
                # MARSOUD-AGENT-INVOICES-FIX (Abdelhamid 2026-08-01) —
                # DRAFT / CANCELLED / VOIDED are not real sales: a DRAFT
                # was never posted to the ledger, and CANCELLED / VOIDED
                # had their journal entry reversed. Counting them made the
                # agent report a higher "total sales" than the income
                # statement and the AR aging report show for the same
                # period (aging started excluding VOIDED in 8b88ab7).
                # The caller can still see them by passing an explicit
                # `status` or include_all_statuses=true.
                excluded = ["DRAFT", "CANCELLED", "VOIDED"]
                q = q.filter(~Invoice.status.in_([
                    _InvStatus.DRAFT,
                    _InvStatus.CANCELLED,
                    _InvStatus.VOIDED,
                ]))
            customer_id = args.get("customer_id")
            if customer_id:
                q = q.filter(Invoice.customer_id == customer_id)
            invoices = q.order_by(Invoice.issue_date.desc(), Invoice.id.desc()).all()
            total_amount = sum(float(i.total or 0) for i in invoices)
            return {
                "count": len(invoices),
                "total_amount": round(total_amount, 2),
                "start_date": str(start),
                "end_date": str(end),
                "excluded_statuses": excluded,
                "invoices": [
                    {
                        "invoice_id": i.id,
                        "number": i.number,
                        "customer": i.customer.name if i.customer else None,
                        "issue_date": str(i.issue_date),
                        "total": float(i.total or 0),
                        "status": i.status.value,
                    }
                    for i in invoices[:limit]
                ],
            }

        if name == "run_report":
            rtype = args["type"]
            start = _parse_date(args.get("start_date"),
                                _today(company).replace(day=1))
            end = _parse_date(args.get("end_date"), _today(company))
            if rtype == "balance_sheet":
                return balance_sheet(company_id, as_of=end)
            if rtype == "income_statement":
                return income_statement(company_id, start_date=start, end_date=end)
            if rtype == "cash_flow":
                return cash_flow(company_id, start_date=start, end_date=end)
            if rtype == "aging":
                return aging_report(company_id, as_of=end)
            if rtype == "dashboard":
                return dashboard_metrics(company_id)

        if name == "explain_concept":
            # The agent itself does the explaining; this just confirms which concept.
            return {"concept": args["concept"], "instruction": "اشرح هذا المفهوم بالعربية بشكل مبسط وعملي"}

        if name == "get_stock_level":
            from app.models import ProductVariant
            from app.services.inventory import find_variant_by_barcode
            q = (args.get("query") or "").strip()
            v = find_variant_by_barcode(company_id, q)
            if not v:
                v = ProductVariant.query.filter(
                    ProductVariant.company_id == company_id,
                    db.or_(ProductVariant.sku == q,
                           ProductVariant.sku.ilike(f"%{q}%")),
                ).first()
            if not v:
                # Fall back to product name
                from app.models import Product
                p = Product.query.filter(
                    Product.company_id == company_id,
                    Product.name.ilike(f"%{q}%"),
                ).first()
                if p:
                    v = p.default_variant
            if not v:
                return {"error": f"لم يُعثر على صنف بـ '{q}'"}
            from app.models import StockBalance
            balances = StockBalance.query.filter_by(variant_id=v.id).all()
            # MARSOUD-UNIT-CONVERSION-01 — stock levels are stored in
            # the base unit; expose its name so the AI knows to say
            # "150 حبة" not just "150".
            base_unit_name = None
            if v.product and v.product.base_unit:
                base_unit_name = v.product.base_unit.unit_name
            return {
                "sku": v.sku,
                "name": v.display_name,
                "total_qty": v.total_qty,
                "qty_unit": base_unit_name,
                "total_value": v.total_value,
                "average_cost": v.average_cost,
                "by_warehouse": [
                    {"warehouse": b.warehouse.code, "qty": float(b.qty),
                     "value": float(b.value)}
                    for b in balances
                ],
            }

        if name == "list_low_stock":
            from app.services.inventory import low_stock_variants
            mult = float(args.get("multiplier") or 1.0)
            rows = low_stock_variants(company_id, mult)
            return {
                "count": len(rows),
                "items": [{
                    "sku": v.sku, "name": v.display_name,
                    "current": v.total_qty,
                    "reorder_level": float(v.reorder_level or 0),
                } for v in rows],
            }

        if name == "get_product_profitability":
            # Only ProductVariant is not at module level; the other
            # three would shadow the module imports and hit the same
            # UnboundLocalError trap other branches did.
            from app.models import ProductVariant
            q = (args.get("query") or "").strip()
            v = ProductVariant.query.filter(
                ProductVariant.company_id == company_id,
                db.or_(ProductVariant.sku == q,
                       ProductVariant.sku.ilike(f"%{q}%")),
            ).first()
            if not v:
                from app.models import Product
                p = Product.query.filter(
                    Product.company_id == company_id,
                    Product.name.ilike(f"%{q}%"),
                ).first()
                if p:
                    v = p.default_variant
            if not v:
                return {"error": f"لم يُعثر على صنف بـ '{q}'"}
            start = _parse_date(args.get("from_date"),
                                _today(company).replace(day=1))
            end = _parse_date(args.get("to_date"), _today(company))
            rows = (
                db.session.query(InvoiceItem, Invoice)
                .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
                .filter(Invoice.company_id == company_id,
                        InvoiceItem.variant_id == v.id,
                        Invoice.issue_date >= start,
                        Invoice.issue_date <= end,
                        Invoice.status != InvoiceStatus.DRAFT)
                .all()
            )
            # MARSOUD-UNIT-CONVERSION-01 — COGS is per BASE unit, so
            # multiply by base_quantity (with legacy fallback). qty_sold
            # is reported in the base unit so the AI never confuses
            # "بعت 2 كرتونة" with "بعت 2 حبة".
            def _base_qty(it):
                if it.base_quantity is not None:
                    return float(it.base_quantity or 0)
                return float(it.quantity or 0)
            qty_sold = sum(_base_qty(it) for it, _ in rows)
            revenue = sum(float(it.line_total or 0) for it, _ in rows)
            cogs = sum(_base_qty(it) * float(it.unit_cost_at_sale or 0)
                       for it, _ in rows)
            profit = revenue - cogs
            margin = (profit / revenue * 100) if revenue > 0 else 0
            base_unit_name = None
            if v.product and v.product.base_unit:
                base_unit_name = v.product.base_unit.unit_name
            return {
                "sku": v.sku, "name": v.display_name,
                "from": start.isoformat(), "to": end.isoformat(),
                "qty_sold": qty_sold,
                "qty_unit": base_unit_name,
                "revenue": revenue,
                "cogs": cogs, "gross_profit": profit,
                "gross_margin_pct": round(margin, 2),
            }

        if name == "get_cashier_sales":
            # Invoice + InvoiceStatus already hoisted at module top;
            # importing them locally here made every reference to
            # them in execute_tool local (Python scoping), which
            # UnboundLocalError'd once the top-of-function
            # `company = db.session.get(Company, ...)` was added.
            from app.models import User
            day = _parse_date(args.get("date"), _today(company))
            rows = Invoice.query.filter(
                Invoice.company_id == company_id,
                Invoice.source == "POS",
                Invoice.issue_date == day,
            ).all()
            agg = {}
            for inv in rows:
                cid_key = inv.cashier_id or 0
                a = agg.setdefault(cid_key, {
                    "cashier": inv.cashier.full_name if inv.cashier else "—",
                    "orders": 0, "voids": 0, "gross": 0,
                })
                a["orders"] += 1
                if inv.is_voided:
                    a["voids"] += 1
                else:
                    a["gross"] += float(inv.total or 0)
            return {
                "date": day.isoformat(),
                "cashiers": list(agg.values()),
            }

        if name == "get_open_shifts":
            from app.models import CashierShift
            from app.services.pos_shifts import _expected_cash_for
            open_shifts = CashierShift.query.filter_by(
                company_id=company_id, status="OPEN",
            ).all()
            # MARSOUD-TZ-01 — render datetimes in the company's TZ so
            # the AI never quotes raw UTC back to the user.
            from app.services.time import to_company_tz_str
            from app.models import Company as _Co
            _company = _Co.query.get(company_id)
            return {"shifts": [{
                "id": s.id,
                "cashier": s.cashier.full_name if s.cashier else "—",
                "opened_at": to_company_tz_str(
                    s.opened_at, _company, "%Y-%m-%d %H:%M"),
                "opening_cash": float(s.opening_cash or 0),
                "expected_cash": _expected_cash_for(s),
            } for s in open_shifts]}

        if name == "get_shift_summary":
            from app.models import CashierShift
            from app.services.pos_shifts import shift_summary, _expected_cash_for
            s = db.session.get(CashierShift, args["shift_id"])
            if not s or s.company_id != company_id:
                return {"error": "الوردية غير موجودة"}
            summary = shift_summary(s)
            return {
                "shift_id": s.id,
                "cashier": s.cashier.full_name if s.cashier else "—",
                "status": s.status,
                "opening_cash": float(s.opening_cash or 0),
                "expected_cash": float(s.expected_cash or _expected_cash_for(s)),
                "closing_cash": float(s.closing_cash) if s.closing_cash is not None else None,
                "variance": float(s.variance) if s.variance is not None else None,
                **summary,
            }

        if name == "transfer_history":
            from app.models import (
                StockTransfer, StockTransferItem, ProductVariant, Product,
            )
            q = (args.get("query") or "").strip()
            v = ProductVariant.query.filter(
                ProductVariant.company_id == company_id,
                ProductVariant.sku.ilike(f"%{q}%"),
            ).first()
            if not v:
                p = Product.query.filter(
                    Product.company_id == company_id,
                    Product.name.ilike(f"%{q}%"),
                ).first()
                if p:
                    v = p.default_variant
            if not v:
                return {"error": f"الصنف '{q}' غير موجود"}
            limit = int(args.get("limit") or 20)
            rows = (
                db.session.query(StockTransfer, StockTransferItem)
                .join(StockTransferItem,
                      StockTransferItem.transfer_id == StockTransfer.id)
                .filter(StockTransferItem.variant_id == v.id,
                        StockTransfer.company_id == company_id)
                .order_by(StockTransfer.created_at.desc())
                .limit(limit).all()
            )
            from app.services.time import to_company_tz_str
            from app.models import Company as _Co
            _company = _Co.query.get(company_id)
            return {
                "sku": v.sku, "name": v.display_name,
                "transfers": [{
                    "number": t.number,
                    "from": t.from_warehouse.code,
                    "to": t.to_warehouse.code,
                    "qty": float(it.qty or 0),
                    "status": t.status,
                    "date": to_company_tz_str(
                        t.created_at, _company, "%Y-%m-%d %H:%M"),
                } for t, it in rows],
            }

        if name == "get_top_products":
            # See the get_cashier_sales comment — same shadowing
            # trap. Invoice / InvoiceItem / InvoiceStatus are all
            # module-level imports; do not re-import them locally.
            start = _parse_date(args.get("from_date"),
                                _today(company).replace(day=1))
            end = _parse_date(args.get("to_date"), _today(company))
            limit = int(args.get("limit") or 10)
            rows = (
                db.session.query(InvoiceItem, Invoice)
                .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
                .filter(Invoice.company_id == company_id,
                        Invoice.issue_date >= start,
                        Invoice.issue_date <= end,
                        Invoice.status != InvoiceStatus.DRAFT,
                        Invoice.status != InvoiceStatus.VOIDED,
                        InvoiceItem.variant_id.isnot(None))
                .all()
            )
            # MARSOUD-UNIT-CONVERSION-01 — aggregate qty in the base
            # unit so mixed كرتونة/حبة sales roll up correctly.
            agg = {}
            for item, inv in rows:
                key = item.variant_id
                base_unit_name = None
                if item.variant and item.variant.product \
                        and item.variant.product.base_unit:
                    base_unit_name = item.variant.product.base_unit.unit_name
                a = agg.setdefault(key, {
                    "sku": item.variant.sku if item.variant else "",
                    "name": item.variant.display_name if item.variant else "",
                    "qty": 0, "qty_unit": base_unit_name,
                    "revenue": 0,
                })
                if item.base_quantity is not None:
                    a["qty"] += float(item.base_quantity or 0)
                else:
                    a["qty"] += float(item.quantity or 0)
                a["revenue"] += float(item.line_total or 0)
            top = sorted(agg.values(), key=lambda r: -r["revenue"])[:limit]
            return {
                "from": start.isoformat(), "to": end.isoformat(),
                "items": top,
            }

        return {"error": f"أداة غير معروفة: {name}"}
    except LedgerError as e:
        db.session.rollback()
        return {"error": str(e)}
    except Exception as e:
        db.session.rollback()
        return {"error": f"خطأ في تنفيذ الأداة: {e}"}
