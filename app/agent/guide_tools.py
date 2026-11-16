"""MARSOUD-P111 — Read-only inspection tools for the Marsoud System Guide Agent."""
from __future__ import annotations

GUIDE_TOOL_SCHEMAS = [
    {
        "name": "search_marsoud_screens",
        "description": (
            "البحث في خريطة نظام مرصود الفعلية (المولّدة من الكود والواجهات) لإيجاد الشاشات والميزات "
            "والروابط الحقيقية داخل النظام لأي وظيفة (مثل: فاتورة، قيد، مخزون، باركود، صلاحية، سيريال، "
            "تصنيع، موظفين، رواتب، عملاء، عهدة، تقارير، إقفال، ضريبة)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "كلمة مفتاحية بالعربي أو الإنجليزي للبحث في شاشات وميزات النظام",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "inspect_screen_ui",
        "description": (
            "فحص واجهة HTML الفعلية لشاشة معينة في مرصود لمعرفة العناوين والحقول الإجبارية والاختيارية "
            "والأزرار الموجودة داخلها بالضبط، حتى تشرح للمستخدم كيف يملأ الشاشة بدقة."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "screen_url_or_name": {
                    "type": "string",
                    "description": "رابط الشاشة مثل /invoices/new أو اسمها بالعربي مثل فاتورة مبيعات جديدة",
                }
            },
            "required": ["screen_url_or_name"],
        },
    },
    {
        "name": "inspect_company_setup",
        "description": (
            "فحص وضع شركة المستخدم الحالية (إعدادات الشركة، فروعها، طرق الدفع/الخزائن، المستودعات، "
            "شجرة الحسابات، المنتجات، العملاء، الموردين) لتحديد ما تم إعداده وما ينقص لبناء الشركة بالترتيب الصحيح."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


def execute_guide_tool(name: str, args: dict, company_id: int, user_id: int):
    args = args or {}
    if name == "search_marsoud_screens":
        from app.agent.system_map import get_system_map
        q = (args.get("query") or "").strip().lower()
        smap = get_system_map()
        matches = []
        for s in smap["screens"]:
            hay = f"{s['label_ar']} {s['module_ar']} {s['url']} {s['code']}".lower()
            if not q or any(tok in hay for tok in q.split()):
                matches.append({
                    "screen": s["label_ar"],
                    "module": s["module_ar"],
                    "url": s["url"],
                })
        # Also include matching modules
        mod_matches = [
            {"module": m["label_ar"], "description": m["description_ar"]}
            for m in smap["modules"]
            if not q or any(tok in f"{m['label_ar']} {m['description_ar']} {m['code']}".lower() for tok in q.split())
        ]
        return {
            "matching_modules": mod_matches[:10],
            "matching_screens": matches[:25],
            "industry_presets": smap["presets"],
        }

    if name == "inspect_screen_ui":
        from app.agent.system_map import inspect_screen_details
        return inspect_screen_details(args.get("screen_url_or_name", ""))

    if name == "inspect_company_setup":
        from app import db
        from app.models import Company, Account, Customer, Vendor, Product, Invoice, VendorBill
        co = db.session.get(Company, company_id)
        if not co:
            return {"error": "الشركة غير موجودة"}

        pay_methods = []
        try:
            from app.models import PaymentMethod
            pms = PaymentMethod.query.filter_by(company_id=company_id).all()
            pay_methods = [getattr(p, "name_ar", None) or getattr(p, "name", "") for p in pms]
        except Exception:
            pass

        warehouses = []
        try:
            from app.models import Warehouse
            whs = Warehouse.query.filter_by(company_id=company_id).all()
            warehouses = [getattr(w, "name_ar", None) or getattr(w, "name", "") for w in whs]
        except Exception:
            pass

        branches = []
        try:
            from app.models import Branch
            brs = Branch.query.filter_by(company_id=company_id).all()
            branches = [getattr(b, "name_ar", None) or getattr(b, "name", "") for b in brs]
        except Exception:
            pass

        counts = {
            "accounts": Account.query.filter_by(company_id=company_id, is_active=True).count(),
            "warehouses": len(warehouses),
            "warehouse_names": warehouses[:10],
            "payment_methods": len(pay_methods),
            "payment_method_names": pay_methods[:10],
            "branches": len(branches),
            "products": Product.query.filter_by(company_id=company_id).count(),
            "customers": Customer.query.filter_by(company_id=company_id).count(),
            "vendors": Vendor.query.filter_by(company_id=company_id).count(),
            "invoices": Invoice.query.filter_by(company_id=company_id).count(),
            "vendor_bills": VendorBill.query.filter_by(company_id=company_id).count(),
        }

        checklist = [
            {
                "step": 1,
                "title": "بيانات الشركة والضريبة والعملة",
                "done": bool(co.name and co.base_currency),
                "url": "/companies/",
            },
            {
                "step": 2,
                "title": "دليل الحسابات (شجرة الحسابات)",
                "done": counts["accounts"] >= 5,
                "url": "/accounts/",
            },
            {
                "step": 3,
                "title": "طرق الدفع والخزائن والبنوك",
                "done": counts["payment_methods"] >= 1,
                "url": "/payment-methods/",
            },
            {
                "step": 4,
                "title": "المستودعات (للمخزون)",
                "done": counts["warehouses"] >= 1,
                "url": "/inventory/warehouses",
            },
            {
                "step": 5,
                "title": "المنتجات والخدمات والأسعار",
                "done": counts["products"] >= 1,
                "url": "/products/",
            },
            {
                "step": 6,
                "title": "العملاء والموردون",
                "done": (counts["customers"] + counts["vendors"]) >= 1,
                "url": "/customers/",
            },
            {
                "step": 7,
                "title": "أول فاتورة مشتريات أو رصيد افتتاحي ثم فواتير المبيعات",
                "done": (counts["invoices"] + counts["vendor_bills"]) >= 1,
                "url": "/invoices/",
            },
        ]
        return {
            "company_name": co.name,
            "industry_code": getattr(co, "industry_code", "general"),
            "counts": counts,
            "onboarding_checklist": checklist,
        }

    return {"error": f"أداة غير معروفة: {name}"}
