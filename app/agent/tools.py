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
        # MARSOUD-AGENT-TOOLS-04 (2026-08-06) — enum expanded from 5
        # to 12 report types. Every report function in
        # services/reports.py is now callable. Each option's
        # description names WHAT the report shows so the agent
        # picks the right one — the model does not read our source
        # code, so a bare enum value is useless without an Arabic
        # sentence explaining it.
        "description": (
            "شغّل تقرير مالي جاهز. اختر النوع المناسب حسب سؤال المستخدم. "
            "الأنواع المتاحة:\n"
            "· balance_sheet — الميزانية العمومية (الأصول والخصوم وحقوق الملكية) لحظة زمنية\n"
            "· income_statement — قائمة الدخل (الإيرادات مطروحاً منها المصروفات) لفترة\n"
            "· income_statement_compared — قائمة دخل مقارنة بالفترة السابقة\n"
            "· cash_flow — التدفقات النقدية (تشغيل / استثمار / تمويل)\n"
            "· income_summary — ملخص الإيرادات مصنّفة بالحسابات\n"
            "· expenses_summary — ملخص المصروفات مصنّفة بالحسابات\n"
            "· vat — تقرير ضريبة القيمة المضافة: مخرجات، مدخلات، صافي المستحق\n"
            "· ap_aging — أعمار الديون المستحقة للموردين\n"
            "· ar_aging — أعمار الديون المستحقة على العملاء\n"
            "· payroll_summary — ملخص الرواتب لشهر معيّن (يحتاج year + month)\n"
            "· fixed_assets — الأصول الثابتة، القيمة الدفترية، والإهلاك المتراكم\n"
            "· dashboard — لوحة معلومات الشركة (أرقام سريعة)"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [
                        "balance_sheet", "income_statement",
                        "income_statement_compared", "cash_flow",
                        "income_summary", "expenses_summary",
                        "vat", "ap_aging", "ar_aging",
                        "payroll_summary", "fixed_assets", "dashboard",
                    ],
                },
                "start_date": {"type": "string",
                               "description": "YYYY-MM-DD (افتراضي بداية الشهر)"},
                "end_date": {"type": "string",
                             "description": "YYYY-MM-DD (افتراضي اليوم)"},
                "year": {"type": "integer",
                         "description": "للـ payroll_summary فقط"},
                "month": {"type": "integer",
                          "description": "للـ payroll_summary فقط (1-12)"},
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

    # ─── MARSOUD-AGENT-TOOLS-04 (2026-08-06) — Phase 2 read tools ───
    {
        "name": "get_journal_entry",
        "description": (
            "اعرض قيداً محاسبياً كاملاً بأطرافه وحساباته وقيم المدين والدائن. "
            "يقبل رقم القيد (number) أو المعرّف الرقمي (entry_id)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "number": {"type": "string",
                           "description": "رقم القيد كما يظهر في الشاشة"},
                "entry_id": {"type": "integer",
                             "description": "معرّف القيد في قاعدة البيانات"},
            },
        },
    },
    {
        "name": "search_journals",
        "description": (
            "ابحث في القيود اليومية بفترة أو نص أو حساب. "
            "مفيد للأسئلة زي «القيود اللي فيها 'إيجار' الشهر ده» "
            "أو «كل القيود بين تاريخين»."),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string",
                                "description": "YYYY-MM-DD (افتراضي بداية الشهر)"},
                "end_date": {"type": "string",
                              "description": "YYYY-MM-DD (افتراضي اليوم)"},
                "text": {"type": "string",
                          "description": "نص في الوصف أو المرجع"},
                "account_code": {"type": "string",
                                  "description": "كود حساب — يرجع القيود اللي فيها هذا الحساب"},
                "limit": {"type": "integer",
                           "description": "افتراضي 20"},
            },
        },
    },
    {
        "name": "party_statement",
        "description": (
            "كشف حساب طرف (عميل أو مورد) لفترة زمنية — يعرض كل الحركات "
            "الدائنة والمدينة على حساب هذا الطرف."),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["customer", "vendor"]},
                "party_id": {"type": "integer"},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
            "required": ["kind", "party_id"],
        },
    },
    {
        "name": "list_vendors",
        "description": "اعرض الموردين النشطين في الشركة مع أرصدتهم المستحقة.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string",
                            "description": "نص للبحث في اسم المورد (اختياري)"},
            },
        },
    },

    # ─── Phase 3 — read-only modules ───
    {
        "name": "list_vendor_bills",
        "description": (
            "اعرض فواتير الموردين خلال فترة، مع الإجمالي والمتبقّي. "
            "تستبعد تلقائياً المسودات والملغاة والمعدومة (مثل list_invoices)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "status": {"type": "string",
                            "description": "POSTED / PAID / PARTIALLY_PAID / OVERDUE / DRAFT / CANCELLED"},
                "vendor_id": {"type": "integer"},
                "limit": {"type": "integer", "description": "افتراضي 20"},
                "include_all_statuses": {"type": "boolean"},
            },
        },
    },
    {
        "name": "get_vendor_bill",
        "description": "اعرض فاتورة مورد كاملة ببنودها ودفعاتها. يقبل bill_id أو number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bill_id": {"type": "integer"},
                "number": {"type": "string"},
            },
        },
    },
    {
        "name": "list_payroll_runs",
        "description": "اعرض كشوف الرواتب للشركة، مع إمكانية الفلترة بسنة و/أو شهر.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer"},
                "month": {"type": "integer",
                          "description": "1-12 (اختياري)"},
                "limit": {"type": "integer", "description": "افتراضي 20"},
            },
        },
    },
    {
        "name": "list_employee_advances",
        "description": (
            "السلف المفتوحة (المستحقة) على الموظفين مع المتبقّي على كل واحد. "
            "بالافتراضي ترجع السلف النشطة فقط؛ يمكن تمرير status لعرض المسدَّدة أو الملغاة."),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string",
                            "description": "ACTIVE (افتراضي) / SETTLED / CANCELLED"},
            },
        },
    },
    {
        "name": "list_fixed_assets",
        "description": (
            "اعرض الأصول الثابتة للشركة مع القيمة الدفترية والإهلاك المتراكم."),
        "input_schema": {
            "type": "object",
            "properties": {},
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
            # MARSOUD-AGENT-UX-06 (2026-08-06) — the chat client
            # renders this result as a "قيد جديد" card with a table
            # of lines + Excel/PDF export buttons. That needs the
            # per-line breakdown, so the tool returns lines with
            # account_code + name_ar; without them the card would
            # have to make a second round-trip to reconstruct what
            # it just posted.
            #
            # `db.session.get(Account, l.account_id)` can return
            # None if the account row was deleted after posting (a
            # data-fix or a stale dev DB with orphaned lines) —
            # fall through with None rather than crashing the
            # whole tool return.
            from app.models import JournalLine
            entry_lines = JournalLine.query.filter_by(
                entry_id=entry.id).all()

            def _line_dict(l):
                acc = (db.session.get(Account, l.account_id)
                       if l.account_id else None)
                return {
                    "account_code": acc.code if acc else None,
                    "account_name_ar": (
                        acc.name_ar if acc else None),
                    "debit": float(l.debit or 0),
                    "credit": float(l.credit or 0),
                    "memo": l.memo,
                }

            return {
                "ok": True,
                "entry_id": entry.id,
                "number": entry.number,
                "date": str(entry.date),
                "description": entry.description,
                "total_debit": entry.total_debit,
                "total_credit": entry.total_credit,
                "lines": [_line_dict(l) for l in entry_lines],
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
            # MARSOUD-AGENT-TOOLS-04 (2026-08-06) — dispatch expanded
            # to the full report catalog in services/reports.py.
            # Every branch below is a one-line delegation; no new
            # accounting logic in the agent layer.
            from app.services.reports import (
                vat_report, expenses_summary, income_summary,
                income_statement_compared, ap_aging_report,
                payroll_summary_report, fixed_assets_report,
            )
            rtype = args["type"]
            start = _parse_date(args.get("start_date"),
                                _today(company).replace(day=1))
            end = _parse_date(args.get("end_date"), _today(company))
            if rtype == "balance_sheet":
                return balance_sheet(company_id, as_of=end)
            if rtype == "income_statement":
                return income_statement(company_id, start_date=start, end_date=end)
            if rtype == "income_statement_compared":
                return income_statement_compared(
                    company_id, start_date=start, end_date=end)
            if rtype == "cash_flow":
                return cash_flow(company_id, start_date=start, end_date=end)
            if rtype == "income_summary":
                return income_summary(
                    company_id, start_date=start, end_date=end)
            if rtype == "expenses_summary":
                return expenses_summary(
                    company_id, start_date=start, end_date=end)
            if rtype == "vat":
                return vat_report(
                    company_id, start_date=start, end_date=end)
            if rtype == "ap_aging":
                return ap_aging_report(company_id, as_of=end)
            if rtype in ("aging", "ar_aging"):
                # Keep the legacy "aging" alias so old messages in
                # a running chat don't break; new callers should
                # use "ar_aging" for symmetry with "ap_aging".
                return aging_report(company_id, as_of=end)
            if rtype == "payroll_summary":
                yr = args.get("year") or _today(company).year
                mo = args.get("month") or _today(company).month
                return payroll_summary_report(
                    company_id, year=int(yr), month=int(mo))
            if rtype == "fixed_assets":
                return fixed_assets_report(company_id)
            if rtype == "dashboard":
                return dashboard_metrics(company_id)
            return {"error": f"نوع تقرير غير معروف: {rtype}"}

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

        # ─── MARSOUD-AGENT-TOOLS-04 (2026-08-06) — Phase 2 read tools ───

        if name == "get_journal_entry":
            # Look up by number OR entry_id. Cross-tenant guard: after
            # loading the entry, verify company_id matches. Same
            # shape record_payment already uses on its invoice arg.
            entry = None
            num = (args.get("number") or "").strip()
            if num:
                entry = JournalEntry.query.filter_by(
                    company_id=company_id, number=num).first()
            elif args.get("entry_id"):
                entry = db.session.get(JournalEntry, args["entry_id"])
                if entry and entry.company_id != company_id:
                    entry = None
            if entry is None:
                return {"error": "القيد غير موجود في هذه الشركة"}
            # Account is already at module level — importing it
            # locally would shadow it into a function-scope name and
            # UnboundLocalError anywhere the module-level Account is
            # referenced later in execute_tool (Python scoping trap
            # from T1's fix). Only bring in JournalLine.
            from app.models import JournalLine
            lines = JournalLine.query.filter_by(entry_id=entry.id).all()
            return {
                "entry_id": entry.id,
                "number": entry.number,
                "date": str(entry.date),
                "description": entry.description,
                "reference": entry.reference,
                "is_reversal": bool(entry.is_reversal),
                "reversal_of": entry.reversal_of,
                "total_debit": round(
                    sum(float(l.debit or 0) for l in lines), 2),
                "total_credit": round(
                    sum(float(l.credit or 0) for l in lines), 2),
                "lines": [
                    {
                        "account_code": (db.session.get(
                            Account, l.account_id).code
                            if l.account_id else None),
                        "account_name_ar": (db.session.get(
                            Account, l.account_id).name_ar
                            if l.account_id else None),
                        "debit": float(l.debit or 0),
                        "credit": float(l.credit or 0),
                        "memo": l.memo,
                    } for l in lines
                ],
            }

        if name == "search_journals":
            # Account is already at module level — importing it
            # locally would shadow it into a function-scope name and
            # UnboundLocalError anywhere the module-level Account is
            # referenced later in execute_tool (Python scoping trap
            # from T1's fix). Only bring in JournalLine.
            from app.models import JournalLine
            start = _parse_date(args.get("start_date"),
                                _today(company).replace(day=1))
            end = _parse_date(args.get("end_date"), _today(company))
            limit = int(args.get("limit") or 20)
            q = JournalEntry.query.filter(
                JournalEntry.company_id == company_id,
                JournalEntry.date >= start,
                JournalEntry.date <= end,
            )
            text = (args.get("text") or "").strip()
            if text:
                like = f"%{text}%"
                q = q.filter(db.or_(
                    JournalEntry.description.ilike(like),
                    JournalEntry.reference.ilike(like)))
            code = (args.get("account_code") or "").strip()
            if code:
                acc = Account.query.filter_by(
                    company_id=company_id, code=code).first()
                if not acc:
                    return {"error": f"لا يوجد حساب بالكود {code}"}
                entry_ids = [r[0] for r in db.session.query(
                    JournalLine.entry_id).filter(
                    JournalLine.account_id == acc.id).distinct()]
                q = q.filter(JournalEntry.id.in_(entry_ids))
            entries = q.order_by(
                JournalEntry.date.desc(), JournalEntry.id.desc()
            ).limit(limit).all()
            return {
                "count": len(entries),
                "start_date": str(start), "end_date": str(end),
                "entries": [
                    {"entry_id": e.id, "number": e.number,
                     "date": str(e.date),
                     "description": e.description,
                     "reference": e.reference}
                    for e in entries
                ],
            }

        if name == "party_statement":
            from app.services.party_ledger import party_ledger
            kind_str = (args.get("kind") or "").lower()
            # Cross-tenant guard: verify the party belongs to THIS
            # company before we hand its id to party_ledger.
            # party_ledger has its own cross-tenant check that raises
            # ValueError, but we prefer to return a specific Arabic
            # error dict instead of letting a ValueError bubble.
            if kind_str == "customer":
                p = db.session.get(Customer, args.get("party_id"))
                if not p or p.company_id != company_id:
                    return {"error": "العميل غير موجود في هذه الشركة"}
            elif kind_str == "vendor":
                p = db.session.get(Vendor, args.get("party_id"))
                if not p or p.company_id != company_id:
                    return {"error": "المورد غير موجود في هذه الشركة"}
            else:
                return {"error": "kind لازم يكون customer أو vendor"}
            start = _parse_date(args.get("start_date"),
                                _today(company).replace(day=1))
            end = _parse_date(args.get("end_date"), _today(company))
            # party_ledger takes kind as a lowercase string
            # ("customer" / "vendor") — see _KIND_MAP in
            # services/party_ledger.py.
            result = party_ledger(company_id, kind_str,
                                   args["party_id"],
                                   start_date=start, end_date=end)
            # The returned dict may contain date objects — JSON dump
            # via the outer serializer handles them, but be explicit
            # about a couple fields for the model's readability.
            if isinstance(result, dict):
                for k in ("start_date", "end_date"):
                    if hasattr(result.get(k), "isoformat"):
                        result[k] = result[k].isoformat()
            return result

        if name == "list_vendors":
            q = Vendor.query.filter_by(
                company_id=company_id, is_active=True)
            search = (args.get("search") or "").strip()
            if search:
                like = f"%{search}%"
                q = q.filter(Vendor.name.ilike(like))
            vendors = q.order_by(Vendor.name).limit(100).all()
            return {
                "vendors": [
                    {"id": v.id, "name": v.name,
                     "email": v.email,
                     "phone": getattr(v, "phone", None),
                     "balance": round(v.balance, 2)}
                    for v in vendors
                ],
            }

        # ─── Phase 3 — read-only modules ───

        if name == "list_vendor_bills":
            from app.models import VendorBill, VendorBillStatus
            start = _parse_date(args.get("start_date"),
                                _today(company).replace(day=1))
            end = _parse_date(args.get("end_date"), _today(company))
            limit = int(args.get("limit") or 20)
            q = VendorBill.query.filter(
                VendorBill.company_id == company_id,
                VendorBill.deleted_at.is_(None),
                VendorBill.issue_date >= start,
                VendorBill.issue_date <= end,
            )
            status = args.get("status")
            excluded = []
            if status:
                try:
                    q = q.filter(VendorBill.status
                                  == VendorBillStatus[status])
                except KeyError:
                    return {"error": f"حالة غير معروفة: {status}"}
            elif not args.get("include_all_statuses"):
                # Match list_invoices: drafts/cancelled/refunded are
                # not real obligations. Toggle via include_all_statuses
                # if the user asks explicitly.
                excluded = ["DRAFT", "CANCELLED", "REFUNDED"]
                q = q.filter(~VendorBill.status.in_([
                    VendorBillStatus.DRAFT,
                    VendorBillStatus.CANCELLED,
                    VendorBillStatus.REFUNDED,
                ]))
            vid = args.get("vendor_id")
            if vid:
                # Cross-tenant: refuse a vendor_id belonging to B.
                v = db.session.get(Vendor, vid)
                if not v or v.company_id != company_id:
                    return {"error": "المورد غير موجود في هذه الشركة"}
                q = q.filter(VendorBill.vendor_id == vid)
            bills = q.order_by(
                VendorBill.issue_date.desc(),
                VendorBill.id.desc()).all()
            total = sum(float(b.total or 0) for b in bills)
            outstanding = sum(float(b.balance or 0) for b in bills)
            return {
                "count": len(bills),
                "total": round(total, 2),
                "outstanding": round(outstanding, 2),
                "start_date": str(start), "end_date": str(end),
                "excluded_statuses": excluded,
                "bills": [
                    {"bill_id": b.id, "number": b.number,
                     "vendor": (b.vendor.name if b.vendor else None),
                     "issue_date": str(b.issue_date),
                     "due_date": str(b.due_date) if b.due_date else None,
                     "total": float(b.total or 0),
                     "balance": float(b.balance or 0),
                     "status": b.status.value}
                    for b in bills[:limit]
                ],
            }

        if name == "get_vendor_bill":
            from app.models import VendorBill
            bill = None
            num = (args.get("number") or "").strip()
            if num:
                bill = VendorBill.query.filter_by(
                    company_id=company_id, number=num).first()
            elif args.get("bill_id"):
                bill = db.session.get(VendorBill, args["bill_id"])
                if bill and bill.company_id != company_id:
                    bill = None
            if bill is None:
                return {"error": "الفاتورة غير موجودة في هذه الشركة"}
            return {
                "bill_id": bill.id, "number": bill.number,
                "vendor": (bill.vendor.name if bill.vendor else None),
                "issue_date": str(bill.issue_date),
                "due_date": str(bill.due_date) if bill.due_date else None,
                "currency": bill.currency,
                "subtotal": float(bill.subtotal or 0),
                "tax_amount": float(bill.tax_amount or 0),
                "total": float(bill.total or 0),
                "paid_amount": float(bill.paid_amount or 0),
                "balance": float(bill.balance or 0),
                "status": bill.status.value,
                "items": [
                    {"description": i.description,
                     "quantity": float(i.quantity or 0),
                     "unit_price": float(i.unit_price or 0),
                     "line_total": float(i.line_total or 0),
                     "line_type": i.line_type.value}
                    for i in bill.items
                ],
                "payments": [
                    {"amount": float(p.amount or 0),
                     "payment_date": str(p.payment_date),
                     "method": p.method}
                    for p in bill.payments
                ],
            }

        if name == "list_payroll_runs":
            from app.models import PayrollRun, PayrollLine
            q = PayrollRun.query.filter_by(company_id=company_id)
            year = args.get("year")
            month = args.get("month")
            if year:
                q = q.filter(PayrollRun.period_year == int(year))
            if month:
                q = q.filter(PayrollRun.period_month == int(month))
            limit = int(args.get("limit") or 20)
            runs = q.order_by(
                PayrollRun.period_year.desc(),
                PayrollRun.period_month.desc()).limit(limit).all()
            out = []
            for r in runs:
                lines = PayrollLine.query.filter_by(run_id=r.id).all()
                out.append({
                    "run_id": r.id,
                    "number": r.number,
                    "year": r.period_year,
                    "month": r.period_month,
                    "employees": len(lines),
                    "total_net": round(
                        sum(float(l.net or 0) for l in lines), 2),
                    "total_paid": round(
                        sum(float(l.amount_paid or 0) for l in lines), 2),
                })
            return {"runs": out, "count": len(out)}

        if name == "list_employee_advances":
            from app.models import EmployeeAdvance, AdvanceStatus, Employee
            status_str = (args.get("status") or "ACTIVE").upper()
            try:
                status = AdvanceStatus[status_str]
            except KeyError:
                return {"error": f"حالة غير معروفة: {status_str}"}
            # Scope by company via the employee. EmployeeAdvance has
            # employee_id → Employee.company_id.
            q = db.session.query(EmployeeAdvance, Employee).join(
                Employee, EmployeeAdvance.employee_id == Employee.id
            ).filter(
                Employee.company_id == company_id,
                EmployeeAdvance.status == status,
            ).order_by(EmployeeAdvance.id.desc())
            rows = q.all()
            return {
                "count": len(rows),
                "status": status_str,
                "advances": [
                    {"advance_id": a.id,
                     "employee_id": e.id,
                     "employee_name": e.name,
                     "amount": float(a.amount or 0),
                     "remaining": float(a.remaining or 0)}
                    for a, e in rows
                ],
            }

        if name == "list_fixed_assets":
            # Delegate to the existing report — it already computes
            # book value + accumulated depreciation per asset.
            from app.services.reports import fixed_assets_report
            return fixed_assets_report(company_id)

        return {"error": f"أداة غير معروفة: {name}"}
    except LedgerError as e:
        db.session.rollback()
        return {"error": str(e)}
    except Exception as e:
        db.session.rollback()
        return {"error": f"خطأ في تنفيذ الأداة: {e}"}
