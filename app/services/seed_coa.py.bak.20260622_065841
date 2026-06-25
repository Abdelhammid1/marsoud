"""Default Chart of Accounts seeded on company creation."""
from app import db
from app.models import Account, AccountType, NormalSide


DEFAULT_COA = [
    # ASSETS (1xxx)
    ("1000", "Assets", "الأصول", AccountType.ASSET, None),
    ("1100", "Current Assets", "الأصول المتداولة", AccountType.ASSET, "1000"),
    ("1110", "Cash", "النقدية", AccountType.ASSET, "1100"),
    ("1120", "Bank", "البنك", AccountType.ASSET, "1100"),
    ("1130", "Accounts Receivable", "العملاء — المدينون", AccountType.ASSET, "1100"),
    ("1300", "Inventory", "المخزون", AccountType.ASSET, "1100"),
    ("1150", "Prepaid Expenses", "المصروفات المدفوعة مقدماً", AccountType.ASSET, "1100"),
    ("1200", "Fixed Assets", "الأصول الثابتة", AccountType.ASSET, "1000"),
    ("1210", "Equipment", "المعدات", AccountType.ASSET, "1200"),
    ("1220", "Vehicles", "السيارات", AccountType.ASSET, "1200"),
    ("1230", "Buildings", "المباني", AccountType.ASSET, "1200"),
    ("1290", "Accumulated Depreciation — Fixed Assets", "مجمع إهلاك الأصول الثابتة", AccountType.ASSET, "1200"),
    # LIABILITIES (2xxx)
    ("2000", "Liabilities", "الالتزامات", AccountType.LIABILITY, None),
    ("2100", "Current Liabilities", "الالتزامات قصيرة الأجل", AccountType.LIABILITY, "2000"),
    ("2110", "Accounts Payable", "الموردون — الدائنون", AccountType.LIABILITY, "2100"),
    ("2120", "VAT Payable", "ضريبة القيمة المضافة المستحقة", AccountType.LIABILITY, "2100"),
    ("2130", "Salaries Payable", "الرواتب المستحقة", AccountType.LIABILITY, "2100"),
    ("2140", "Short-term Loans", "قروض قصيرة الأجل", AccountType.LIABILITY, "2100"),
    # MARSOUD-COMM-01 — sales commissions owed to reps (separate from
    # 2130 so commission/salary balances are individually visible).
    ("2150", "Sales Commissions Payable", "عمولات مبيعات مستحقة", AccountType.LIABILITY, "2100"),
    ("2200", "Long-term Liabilities", "الالتزامات طويلة الأجل", AccountType.LIABILITY, "2000"),
    ("2210", "Long-term Loans", "قروض طويلة الأجل", AccountType.LIABILITY, "2200"),
    # EQUITY (3xxx)
    ("3000", "Equity", "حقوق الملكية", AccountType.EQUITY, None),
    ("3100", "Owner's Capital", "رأس المال", AccountType.EQUITY, "3000"),
    ("3200", "Retained Earnings", "الأرباح المحتجزة", AccountType.EQUITY, "3000"),
    ("3300", "Drawings", "المسحوبات الشخصية", AccountType.EQUITY, "3000"),
    # ERP-01 — opening-balance contra for first-time inventory entry
    ("3900", "Opening Balance Equity", "حساب الافتتاح", AccountType.EQUITY, "3000"),
    # REVENUE (4xxx)
    ("4000", "Revenue", "الإيرادات", AccountType.REVENUE, None),
    ("4100", "Sales Revenue", "إيرادات المبيعات", AccountType.REVENUE, "4000"),
    ("4200", "Service Revenue", "إيرادات الخدمات", AccountType.REVENUE, "4000"),
    ("4300", "Other Income", "إيرادات أخرى", AccountType.REVENUE, "4000"),
    # EXPENSES (5xxx)
    ("5000", "Expenses", "المصروفات", AccountType.EXPENSE, None),
    ("5100", "Cost of Goods Sold", "تكلفة البضاعة المباعة", AccountType.EXPENSE, "5000"),
    ("5200", "Operating Expenses", "المصروفات التشغيلية", AccountType.EXPENSE, "5000"),
    ("5210", "Salaries Expense", "مصروف الرواتب", AccountType.EXPENSE, "5200"),
    ("5220", "Rent Expense", "مصروف الإيجار", AccountType.EXPENSE, "5200"),
    ("5230", "Utilities", "المرافق", AccountType.EXPENSE, "5200"),
    ("5240", "Marketing", "التسويق والإعلان", AccountType.EXPENSE, "5200"),
    ("5250", "Fixed Assets Depreciation Expense", "مصاريف إهلاك الأصول الثابتة", AccountType.EXPENSE, "5200"),
    ("5260", "Office Supplies", "أدوات مكتبية", AccountType.EXPENSE, "5200"),
    ("5270", "Bank Charges", "عمولات البنك", AccountType.EXPENSE, "5200"),
    # MARSOUD-COMM-01 — commission expense, separate from salary expense
    # so the P&L distinguishes commissions from fixed compensation.
    ("5280", "Sales Commissions Expense", "مصروف عمولات المبيعات", AccountType.EXPENSE, "5200"),
    # ERP-01 — stock-count variance account
    ("5990", "Inventory Variance", "فروقات الجرد", AccountType.EXPENSE, "5000"),
]


def seed_default_coa(company_id):
    """Create the default Chart of Accounts AND payment methods for a new company."""
    from app.models.account import NORMAL_SIDE_FOR_TYPE
    from app.models import PaymentMethod

    code_to_id = {}
    for code, name, name_ar, acc_type, parent_code in DEFAULT_COA:
        parent_id = code_to_id.get(parent_code) if parent_code else None
        acc = Account(
            company_id=company_id,
            code=code,
            name=name,
            name_ar=name_ar,
            type=acc_type,
            normal_side=NORMAL_SIDE_FOR_TYPE[acc_type],
            parent_id=parent_id,
        )
        db.session.add(acc)
        db.session.flush()
        code_to_id[code] = acc.id

    # Default payment methods: cash → 1110, bank → 1120
    cash_acc_id = code_to_id.get("1110")
    bank_acc_id = code_to_id.get("1120")
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
