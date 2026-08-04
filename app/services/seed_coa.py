"""MARSOUD-COA-REBUILD — full default Chart of Accounts seeded on
company creation.

Layout:
  - Numbers preserved wherever the existing service-layer code uses
    them by literal (1110, 1130, 1200, 1290, 1300, 2130, 4100, 5100,
    5250, 5990, etc.) so the rebuild only changes what genuinely needs
    to change (VAT split + sub-account routing).
  - Header accounts (is_postable=False) exist for grouping + reports
    only; post_journal refuses any line that lands on them.
  - 1130 (Customers), 2110 (Vendors), 2130 (Salaries Payable) and 1120
    (Banks) are now headers — every customer/vendor/employee gets a
    sub-account under them at create time (see services/subsidiary.py).
"""
from app import db
from app.models import Account, AccountType, NormalSide


# Each row: (code, name_en, name_ar, type, parent_code, is_postable)
DEFAULT_COA = [
    # ===== ASSETS =====
    ("1000", "Assets", "الأصول", AccountType.ASSET, None, False),
    ("1100", "Current Assets", "الأصول المتداولة", AccountType.ASSET, "1000", False),
    ("1110", "Cash", "النقدية / الصندوق", AccountType.ASSET, "1100", True),
    # Banks — header + the real bank lines underneath
    ("1120", "Banks", "البنوك", AccountType.ASSET, "1100", False),
    ("1121", "Banque Misr - NBE", "البنك الأهلي المصري", AccountType.ASSET, "1120", True),
    ("1122", "Banque Misr", "بنك مصر", AccountType.ASSET, "1120", True),
    ("1123", "Bank of Alexandria", "بنك الإسكندرية", AccountType.ASSET, "1120", True),
    ("1124", "CIB", "بنك CIB", AccountType.ASSET, "1120", True),
    ("1125", "Banque du Caire", "بنك القاهرة", AccountType.ASSET, "1120", True),
    # Subsidiary headers — every customer gets a leaf under 1130
    ("1130", "Accounts Receivable", "العملاء — المدينون", AccountType.ASSET, "1100", False),
    ("1140", "Notes Receivable", "أوراق قبض", AccountType.ASSET, "1100", True),
    ("1150", "Prepaid Expenses", "مصروفات مدفوعة مقدماً", AccountType.ASSET, "1100", True),
    ("1160", "Employee Advances", "سلف ومستحقات الموظفين (مدين)", AccountType.ASSET, "1100", True),
    # MARSOUD-OPS-FOUNDATION (2026-08-05) — revenue earned but not yet
    # billed or collected. The code MUST stay in the 11xx range: the
    # cash-flow classifier infers the category from the account code, and
    # `code.startswith("12") and code != "1290"` means INVESTING
    # (services/reports.py). A 12xx code here would report every accrued
    # revenue as an investing activity in every company's cash-flow
    # statement. 1280 below is already caught by exactly that rule.
    ("1170", "Accrued Revenue", "إيرادات مستحقة", AccountType.ASSET, "1100", True),
    ("1280", "Input VAT (Recoverable)", "ضريبة المدخلات القابلة للخصم", AccountType.ASSET, "1100", True),
    # Inventory — 1300 stays the trading-goods account (code posts to it)
    ("1300", "Inventory", "المخزون", AccountType.ASSET, "1100", True),
    ("1310", "Goods in Transit", "بضاعة في الطريق", AccountType.ASSET, "1100", True),
    ("1320", "Raw Materials", "مخزون مواد خام", AccountType.ASSET, "1100", True),
    ("1330", "Work in Process (WIP)", "إنتاج تحت التشغيل", AccountType.ASSET, "1100", True),
    ("1340", "Finished Goods", "مخزون تام الصنع", AccountType.ASSET, "1100", True),
    ("1350", "Spare Parts & Supplies", "مخزون قطع غيار ومستلزمات", AccountType.ASSET, "1100", True),
    # Fixed assets — 1200 stays header (assets.py posts to 1210/1220/etc.)
    ("1200", "Fixed Assets", "الأصول الثابتة", AccountType.ASSET, "1000", False),
    ("1210", "Equipment & Machinery", "المعدات والآلات", AccountType.ASSET, "1200", True),
    ("1220", "Vehicles", "السيارات", AccountType.ASSET, "1200", True),
    ("1230", "Buildings", "المباني", AccountType.ASSET, "1200", True),
    ("1240", "Furniture & Fixtures", "الأثاث والتجهيزات", AccountType.ASSET, "1200", True),
    ("1250", "Computers & Systems", "أجهزة وأنظمة حاسب", AccountType.ASSET, "1200", True),
    ("1290", "Accumulated Depreciation", "مجمع إهلاك الأصول الثابتة", AccountType.ASSET, "1200", True),
    ("1700", "Intangible Assets", "أصول غير ملموسة", AccountType.ASSET, "1000", False),
    ("1710", "Software & Licenses", "برمجيات وتراخيص", AccountType.ASSET, "1700", True),
    ("1720", "Goodwill & Trademark", "شهرة وعلامة تجارية", AccountType.ASSET, "1700", True),

    # ===== LIABILITIES =====
    ("2000", "Liabilities", "الالتزامات", AccountType.LIABILITY, None, False),
    ("2100", "Current Liabilities", "الالتزامات قصيرة الأجل", AccountType.LIABILITY, "2000", False),
    ("2110", "Accounts Payable", "الموردون — الدائنون", AccountType.LIABILITY, "2100", False),
    ("2115", "Notes Payable", "أوراق دفع", AccountType.LIABILITY, "2100", True),
    # VAT split — output (sales) + input (purchases) + net (settlement)
    ("2120", "Output VAT (Payable)", "ضريبة المخرجات المستحقة", AccountType.LIABILITY, "2100", True),
    ("2125", "Net VAT Payable", "صافي الضريبة المستحقة", AccountType.LIABILITY, "2100", True),
    ("2130", "Salaries Payable", "الرواتب المستحقة", AccountType.LIABILITY, "2100", False),
    ("2135", "Social Insurance Payable (GOSI)", "تأمينات اجتماعية مستحقة", AccountType.LIABILITY, "2100", True),
    ("2136", "Payroll Tax Payable", "ضريبة كسب العمل / استقطاعات الموظفين", AccountType.LIABILITY, "2100", True),
    ("2140", "Short-term Loans", "قروض قصيرة الأجل", AccountType.LIABILITY, "2100", True),
    ("2150", "Sales Commissions Payable", "عمولات مبيعات مستحقة", AccountType.LIABILITY, "2100", True),
    ("2160", "Accrued Expenses", "مصروفات مستحقة", AccountType.LIABILITY, "2100", True),
    ("2170", "Deposits & Retentions", "أمانات ومحتجزات", AccountType.LIABILITY, "2100", True),
    ("2180", "Unearned Revenue", "إيرادات مقبوضة مقدماً", AccountType.LIABILITY, "2100", True),
    ("2190", "Dividends Payable", "توزيعات أرباح مستحقة", AccountType.LIABILITY, "2100", True),
    ("2200", "Long-term Liabilities", "الالتزامات طويلة الأجل", AccountType.LIABILITY, "2000", False),
    ("2210", "Long-term Loans", "قروض طويلة الأجل", AccountType.LIABILITY, "2200", True),
    ("2220", "End-of-Service Provision", "مخصص مكافأة نهاية الخدمة", AccountType.LIABILITY, "2200", True),

    # ===== EQUITY =====
    ("3000", "Equity", "حقوق الملكية", AccountType.EQUITY, None, False),
    ("3100", "Owner's Capital", "رأس المال", AccountType.EQUITY, "3000", True),
    ("3200", "Drawings", "جاري الشركاء / المسحوبات", AccountType.EQUITY, "3000", True),
    ("3300", "Retained Earnings", "الأرباح المحتجزة", AccountType.EQUITY, "3000", True),
    ("3400", "Current Year Earnings", "أرباح/خسائر العام الحالي", AccountType.EQUITY, "3000", True),
    ("3500", "Legal Reserve", "احتياطي قانوني", AccountType.EQUITY, "3000", True),
    ("3900", "Opening Balance Equity", "حساب الافتتاح", AccountType.EQUITY, "3000", True),

    # ===== REVENUE =====
    ("4000", "Revenue", "الإيرادات", AccountType.REVENUE, None, False),
    ("4100", "Sales Revenue", "إيرادات المبيعات", AccountType.REVENUE, "4000", True),
    ("4200", "Service Revenue", "إيرادات الخدمات", AccountType.REVENUE, "4000", True),
    ("4300", "Sales Returns & Allowances", "مردودات ومسموحات المبيعات", AccountType.REVENUE, "4000", True),
    ("4400", "Discounts Allowed", "خصم مسموح به", AccountType.REVENUE, "4000", True),
    ("4500", "Other Income", "إيرادات أخرى", AccountType.REVENUE, "4000", True),

    # ===== EXPENSES =====
    ("5000", "Expenses", "المصروفات", AccountType.EXPENSE, None, False),
    ("5100", "Cost of Sales", "تكلفة المبيعات", AccountType.EXPENSE, "5000", True),
    ("5105", "Purchase Returns & Allowances", "مردودات ومسموحات المشتريات", AccountType.EXPENSE, "5000", True),
    ("5120", "Direct Labor", "أجور إنتاج مباشرة", AccountType.EXPENSE, "5000", True),
    ("5130", "Manufacturing Overhead", "مصروفات صناعية غير مباشرة", AccountType.EXPENSE, "5000", True),
    ("5140", "Manufacturing Variances", "انحرافات تكلفة التصنيع", AccountType.EXPENSE, "5000", True),
    ("5990", "Inventory Variance", "فروقات الجرد", AccountType.EXPENSE, "5000", True),
    ("5200", "Operating Expenses", "المصروفات التشغيلية", AccountType.EXPENSE, "5000", False),
    ("5210", "Salaries Expense", "الرواتب والأجور", AccountType.EXPENSE, "5200", True),
    ("5215", "Communications & Internet", "اتصالات وإنترنت", AccountType.EXPENSE, "5200", True),
    ("5216", "Allowances & Bonuses", "بدلات ومكافآت", AccountType.EXPENSE, "5200", True),
    ("5217", "Social Insurance (Company Share)", "تأمينات اجتماعية (حصة الشركة)", AccountType.EXPENSE, "5200", True),
    ("5220", "Rent Expense", "الإيجار", AccountType.EXPENSE, "5200", True),
    ("5230", "Utilities", "المرافق (كهربا/مياه)", AccountType.EXPENSE, "5200", True),
    ("5235", "Repairs & Maintenance", "صيانة وإصلاحات", AccountType.EXPENSE, "5200", True),
    ("5240", "Marketing & Advertising", "تسويق وإعلان", AccountType.EXPENSE, "5200", True),
    ("5245", "Software Subscriptions (SaaS)", "اشتراكات برمجية", AccountType.EXPENSE, "5200", True),
    ("5250", "Depreciation Expense", "مصروف الإهلاك", AccountType.EXPENSE, "5200", True),
    ("5255", "Government Fees & Charges", "رسوم حكومية ورخص", AccountType.EXPENSE, "5200", True),
    ("5260", "Office Supplies", "أدوات ومستلزمات مكتبية", AccountType.EXPENSE, "5200", True),
    ("5265", "Legal & Accounting Fees", "رسوم قانونية ومحاسبية ومهنية", AccountType.EXPENSE, "5200", True),
    ("5270", "Bank Charges", "عمولات ومصاريف بنكية", AccountType.EXPENSE, "5200", True),
    ("5275", "Insurance", "تأمينات", AccountType.EXPENSE, "5200", True),
    ("5280", "Sales Commissions Expense", "مصروف عمولات المبيعات", AccountType.EXPENSE, "5200", True),
    ("5285", "Travel & Transportation", "سفر وانتقالات", AccountType.EXPENSE, "5200", True),
    ("5299", "Miscellaneous Expenses", "مصاريف نثرية / متنوعة", AccountType.EXPENSE, "5200", True),
    ("5300", "Formation & Setup Expenses", "مصروفات التأسيس", AccountType.EXPENSE, "5000", False),
    ("5310", "Formation-stage Salaries", "رواتب مرحلة التأسيس", AccountType.EXPENSE, "5300", True),
    ("5320", "Market Research & Studies", "أبحاث ودراسة السوق", AccountType.EXPENSE, "5300", True),
    ("5330", "Incorporation & Registration Fees", "رسوم التأسيس والتسجيل", AccountType.EXPENSE, "5300", True),
    ("5340", "Legal & Accounting Consultations", "استشارات قانونية ومحاسبية (تأسيس)", AccountType.EXPENSE, "5300", True),
    ("5350", "Branding & Identity Design", "تصميم الهوية والعلامة التجارية", AccountType.EXPENSE, "5300", True),
    ("5360", "Travel & Founding Meetings", "سفر واجتماعات تأسيسية", AccountType.EXPENSE, "5300", True),
    ("5390", "Other Formation Expenses", "مصاريف تأسيس أخرى", AccountType.EXPENSE, "5300", True),
    ("5900", "Other Expenses", "مصروفات أخرى", AccountType.EXPENSE, "5000", False),
    ("5910", "Bad Debts", "ديون معدومة", AccountType.EXPENSE, "5900", True),
    ("5920", "Currency Exchange Losses", "خسائر فروق عملة", AccountType.EXPENSE, "5900", True),
    ("5930", "Other Misc Expenses", "مصروفات متنوعة أخرى", AccountType.EXPENSE, "5900", True),
    # MARSOUD-OPS-FOUNDATION — interest and financing charges, for the loan
    # instalment operation. Under 5900 «مصروفات أخرى» rather than beside
    # 5270 «عمولات ومصاريف بنكية»: a finance cost is not an operating
    # expense, and 5200 is the operating subtotal. 5270 is a different
    # line (account fees, transfer charges), not a substitute for this.
    ("5940", "Interest & Financing Charges", "فوائد وأعباء تمويلية", AccountType.EXPENSE, "5900", True),
]


def seed_default_coa(company_id):
    """Create the default Chart of Accounts AND payment methods for a
    new company. Reads is_postable from the 6th tuple element."""
    from app.models.account import NORMAL_SIDE_FOR_TYPE
    from app.models import PaymentMethod

    code_to_id = {}
    for row in DEFAULT_COA:
        # Support both 5- and 6-tuple rows so we don't break older
        # callers that hand-build a tree. New rows ship with is_postable.
        if len(row) == 6:
            code, name, name_ar, acc_type, parent_code, is_postable = row
        else:
            code, name, name_ar, acc_type, parent_code = row
            is_postable = True
        parent_id = code_to_id.get(parent_code) if parent_code else None
        acc = Account(
            company_id=company_id,
            code=code,
            name=name,
            name_ar=name_ar,
            type=acc_type,
            normal_side=NORMAL_SIDE_FOR_TYPE[acc_type],
            parent_id=parent_id,
            is_postable=is_postable,
        )
        db.session.add(acc)
        db.session.flush()
        code_to_id[code] = acc.id

    # Default payment methods.
    # Cash → 1110 (still a leaf).
    # Bank → first CIB (1124) since 1120 is now a header and refuses
    #   journal lines. Owner can rewire it to any bank via the
    #   /payment-methods page later.
    cash_acc_id = code_to_id.get("1110")
    bank_acc_id = code_to_id.get("1124") or code_to_id.get("1121")
    if cash_acc_id:
        db.session.add(PaymentMethod(
            company_id=company_id, name="Cash", name_ar="نقدي",
            account_id=cash_acc_id, is_default=True,
        ))
    if bank_acc_id:
        db.session.add(PaymentMethod(
            company_id=company_id, name="Bank Transfer", name_ar="حوالة بنكية",
            account_id=bank_acc_id,
        ))

    db.session.commit()
