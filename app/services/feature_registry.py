"""MARSOUD-SUPERADMIN-CONTROL-01 T1 (2026-08-08) — the ONE source
of truth for every feature the system ships.

Before this file existed, three overlapping lists disagreed about
what a "module" even was:

  · _PREFIX_TO_MODULE  (app/services/plan_gating.py) — 15 codes,
    keyed by permission prefix. Drives plan_allows +
    subitem_allowed + the /admin/feature-flags UI.
  · ALL_MODULES        (app/routes/superadmin.py)   — 11 codes,
    manually maintained.  Drives ONLY the plans-form checkboxes,
    so the 3 missing (insights, cash_custody, evaluations) were
    literally unpickable — even though PLAN_SEED seeded them.
  · FeatureFlag.module_key rows in the DB — free-form strings,
    and the enforce_feature_flags middleware keyed them off
    `endpoint.split('.', 1)[0]` (the BLUEPRINT prefix), not the
    plan-gating module code. Toggling `cash_custody` OFF in the
    admin UI never fired for `custody.*` endpoints.

This file consolidates all three into a single static registry
that ships with the code. Nothing here reads from the database.
The old three surfaces become thin wrappers over the functions
below (see plan_gating.py + superadmin.py rewires in the same
ticket). Adding a new module or feature = editing this file,
period — the plan form, sidebar, gate, and CLI check all pick it
up on the next boot.

Registry surface
================
`Module` + `Feature` frozen dataclasses.
`_MODULES` and `_FEATURES` tuples at module level (constants).
Lookup dicts built once at import time (module-level constants —
no cache needed because everything is derived from immutable
Python literals).

Lookups
-------
- all_modules(include_core=True) → list[Module]
- all_features()                 → list[Feature]
- features_for_module(code)      → list[Feature]
- get_module(code)               → Module | None
- module_for_permission(perm)    → str  | None
- module_for_endpoint(endpoint)  → str  | None
- feature_for_endpoint(endpoint) → Feature | None
- module_for_blueprint(bp)       → str  | None    # NEW — fixes the
                                                   # FeatureFlag key
                                                   # mismatch bug
"""
from dataclasses import dataclass, field
from typing import Optional


# ─── Types ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Module:
    """A coarse-grained feature area a plan can toggle on/off."""
    code: str                           # unique — the plan-gating key
    label_ar: str
    label_en: str
    description_ar: str
    category: str                       # UI grouping in the plan form
    icon: str
    sidebar_section: Optional[str] = None   # legacy SUB_ITEM_CATALOG key
    is_core: bool = False               # always allowed regardless of plan
    sort_order: int = 100
    # Blueprint prefixes owned by this module. Fixes the
    # enforce_feature_flags middleware bug where the middleware
    # looked up FeatureFlags by `endpoint.split('.', 1)[0]` but the
    # admin UI stored keys as plan-gating module codes — so
    # cash_custody's flag never fired for custody.* endpoints.
    blueprint_prefixes: tuple = field(default_factory=tuple)
    # Permission prefixes (`journals.`, `custody.`, …) that identify
    # actions belonging to this module. Replaces _PREFIX_TO_MODULE.
    permission_prefixes: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class Feature:
    """A specific navigable page / capability within a module.

    Each row in the old SUB_ITEM_CATALOG maps to one Feature here.
    `endpoints` is the list of concrete Flask endpoint names that
    represent this feature (the sidebar link + any related detail
    pages if they share a permission), `permissions` lists the
    permission codes the route decorators actually require."""
    code: str                    # unique
    module: str                  # FK to Module.code
    label_ar: str
    endpoints: tuple             # ("invoices.index",)
    permissions: tuple           # ("invoices.view",)
    icon: str = ""
    is_default_on: bool = True   # applies when a plan enables the module


# ─── The registry (immutable static tuples) ──────────────────────────
# Categories for UI grouping in the plan-builder form:
CATEGORY_ACCOUNTING = "accounting"
CATEGORY_SALES = "sales"
CATEGORY_OPS = "ops"
CATEGORY_HR = "hr"
CATEGORY_AI = "ai"
CATEGORY_SYSTEM = "system"


_MODULES: tuple = (
    Module("accounting", "المحاسبة", "Accounting",
           "القيود والحسابات والتقارير المالية وطرق الدفع",
           CATEGORY_ACCOUNTING, "📊",
           sidebar_section="accounting", sort_order=10,
           blueprint_prefixes=("journals", "accounts", "accounting_ops",
                                "assets", "payment_methods", "partners",
                                "party_ledger"),
           permission_prefixes=("journals.", "accounts.",
                                "accounting_ops.", "payment_methods.",
                                "partners.", "assets.",
                                "party_ledger.", "ops.")),
    Module("sales", "المبيعات", "Sales",
           "الفواتير والمنتجات والعملاء والفواتير المتكررة",
           CATEGORY_SALES, "💵",
           sidebar_section="sales", sort_order=20,
           blueprint_prefixes=("invoices", "recurring_invoices",
                                "customers", "products"),
           permission_prefixes=("invoices.", "products.",
                                "customers.", "refunds.")),
    Module("purchases", "المشتريات", "Purchases",
           "فواتير الموردين والمشتريات المتكررة والفواتير الجايّة",
           CATEGORY_SALES, "📥",
           sidebar_section="purchases", sort_order=30,
           blueprint_prefixes=("vendor_bills", "recurring_bills",
                                "forecast", "vendors"),
           permission_prefixes=("vendor_bills.", "recurring_bills.",
                                "forecast.")),
    Module("inventory", "المخزون", "Inventory",
           "المخزون والمخازن والحركات والتحويلات والجرد",
           CATEGORY_OPS, "📦",
           sidebar_section="inventory", sort_order=40,
           blueprint_prefixes=("inventory", "inventory_counts",
                                "transfers"),
           permission_prefixes=("inventory.", "transfers.")),
    Module("pos", "نقطة البيع", "POS",
           "نقطة البيع والشيفتات وسجل المبيعات ومردود الربحية",
           CATEGORY_SALES, "🛒",
           sidebar_section=None, sort_order=50,
           blueprint_prefixes=("pos", "shifts"),
           permission_prefixes=("pos.", "shifts.",
                                "reports.cashier_sales",
                                "reports.profitability")),
    Module("reports", "التقارير المالية", "Reports",
           "الميزانية العمومية وقائمة الدخل والتقارير المالية",
           CATEGORY_ACCOUNTING, "📑",
           sidebar_section=None, sort_order=60,
           blueprint_prefixes=("reports",),
           permission_prefixes=("reports.",)),
    Module("crm", "العملاء المحتملين (CRM)", "CRM",
           "العملاء المحتملين والحملات وجهات الاتصال والمشاريع والمهام",
           CATEGORY_SALES, "🎯",
           sidebar_section="crm", sort_order=70,
           blueprint_prefixes=("leads", "crm", "tasks", "projects",
                                "calendar"),
           permission_prefixes=("leads.", "tasks.", "projects.",
                                "crm.")),
    Module("hr", "الموارد البشرية", "HR",
           "الموظفين والرواتب والحضور والسلف",
           CATEGORY_HR, "👤",
           sidebar_section="hr", sort_order=80,
           blueprint_prefixes=("hr", "payroll", "hr_ss", "advances"),
           permission_prefixes=("hr.", "payroll.", "advances.",
                                "employees.")),
    Module("employee_reports", "تقارير الموظفين اليومية", "Employee Reports",
           "التقارير اليومية للموظفين",
           CATEGORY_HR, "📝",
           sidebar_section="employee_reports", sort_order=90,
           blueprint_prefixes=(),
           permission_prefixes=("employee_reports.",)),
    Module("manufacturing", "التصنيع", "Manufacturing",
           "تركيبات المنتجات (BOM) وأوامر الإنتاج",
           CATEGORY_OPS, "🧬",
           sidebar_section="manufacturing", sort_order=100,
           blueprint_prefixes=("manufacturing",),
           permission_prefixes=("manufacturing.",)),
    Module("evaluations", "تقييم الأداء", "Performance Evaluations",
           "تقييمات الأداء الشهرية للموظفين",
           CATEGORY_HR, "⭐", sort_order=110,
           blueprint_prefixes=("evaluations",),
           permission_prefixes=("evaluations.",)),
    Module("cash_custody", "العهدة النقدية والعينية", "Cash Custody",
           "صرف عهد نقدية ومعدات للموظفين وتسويتها",
           CATEGORY_OPS, "💵", sort_order=120,
           blueprint_prefixes=("custody", "item_custody"),
           permission_prefixes=("custody.",)),
    Module("agent", "المحاسب الذكي", "Accountant Agent",
           "الوكيل المحاسبي (كتابة القيود بالتأكيد)",
           CATEGORY_AI, "🤖",
           sidebar_section=None, sort_order=130, is_core=False,
           blueprint_prefixes=("agent",),
           permission_prefixes=("agent.",)),
    Module("insights", "المحلل الذكي", "Insights Agent",
           "الوكيل التحليلي (قراءة وتحليل الأرقام)",
           CATEGORY_AI, "🔍", sort_order=140,
           blueprint_prefixes=("insights",),
           permission_prefixes=("insights.",)),
    Module("settings", "الإعدادات والنظام", "Settings",
           "المستخدمين والأدوار وبيانات الشركة وسجل التدقيق",
           CATEGORY_SYSTEM, "⚙️",
           sidebar_section="settings", is_core=True, sort_order=200,
           blueprint_prefixes=("users", "settings_roles",
                                "settings_api_tokens", "settings_activity",
                                "settings_backup", "settings_usage",
                                "settings_employee_reports",
                                "audit_log", "companies",
                                "support", "support_admin"),
           permission_prefixes=("users.", "activity_log.",
                                "api_tokens.", "backup.",
                                "settings_usage.", "companies.",
                                "company.", "support.")),
)


_FEATURES: tuple = (
    # ─── Main section (dashboard + insights entry point) ─────────
    Feature("dashboard", "settings", "لوحة المعلومات",
             endpoints=("dashboard.index",), permissions=(),
             icon="📊"),
    Feature("insights_index", "insights", "المحلل الذكي",
             endpoints=("agent.insights_index",),
             permissions=("insights.use",),
             icon="🔍"),

    # ─── Accounting ─────────────────────────────────────────────
    Feature("journals_index", "accounting", "القيود اليومية",
             endpoints=("journals.index",),
             permissions=("journals.view",), icon="📒"),
    Feature("accounts_index", "accounting", "دليل الحسابات",
             endpoints=("accounts.index",),
             # Route is @login_required only — no perm check at
             # the route layer. Sidebar-side we mirror that so the
             # 'visible ⟺ openable' invariant holds.
             permissions=(), icon="🌳"),
    Feature("accounting_ops_index", "accounting", "العمليات المحاسبية",
             endpoints=("accounting_ops.index",),
             permissions=("journals.create",), icon="🧮"),
    Feature("assets_index", "accounting", "الأصول الثابتة",
             endpoints=("assets.index",),
             permissions=("assets.manage",), icon="🏗️"),
    Feature("reports_index", "reports", "التقارير المالية",
             endpoints=("reports.index",),
             permissions=("reports.view",), icon="📑"),
    Feature("party_ledger_index", "accounting", "كشف حساب طرف",
             endpoints=("party_ledger.index",),
             permissions=("party_ledger.view",), icon="📒"),
    Feature("payment_methods_index", "settings", "طرق الدفع",
             endpoints=("payment_methods.index",),
             permissions=("payment_methods.manage",), icon="💳"),

    # ─── Sales ──────────────────────────────────────────────────
    Feature("pos_index", "pos", "نقطة البيع",
             endpoints=("pos.index", "pos.history"),
             permissions=("pos.use",), icon="🛒"),
    Feature("pos_shifts", "pos", "الشيفتات",
             endpoints=("pos.shifts",),
             permissions=("shifts.manage",), icon="🕒"),
    Feature("invoices_index", "sales", "الفواتير",
             endpoints=("invoices.index",),
             permissions=("invoices.create",), icon="🧾"),
    Feature("recurring_invoices_index", "sales", "الفواتير المتكررة",
             endpoints=("recurring_invoices.index",),
             permissions=("invoices.create",), icon="🔁"),
    Feature("customers_index", "sales", "العملاء",
             endpoints=("customers.index",),
             permissions=("customers.view",), icon="👥"),
    Feature("products_index", "sales", "المنتجات والخدمات",
             endpoints=("products.index", "products.hierarchy"),
             permissions=("products.manage",), icon="🏷️"),

    # ─── Purchases ──────────────────────────────────────────────
    Feature("vendor_bills_index", "purchases", "فواتير الموردين",
             endpoints=("vendor_bills.index",),
             permissions=("vendor_bills.create",), icon="📥"),
    Feature("recurring_bills_index", "purchases", "الفواتير المتكررة",
             endpoints=("recurring_bills.index",),
             permissions=("vendor_bills.create",), icon="🔁"),
    Feature("forecast_index", "purchases", "الفواتير الجايّة",
             endpoints=("forecast.index",),
             permissions=("vendor_bills.create",), icon="📅"),
    Feature("vendors_index", "purchases", "الموردين",
             endpoints=("vendors.index",),
             permissions=("partners.manage",), icon="🏢"),
    Feature("vendor_sub_categories", "purchases",
             "تصنيفات فرعية للموردين",
             endpoints=("reports.vendor_sub_categories",),
             permissions=("vendor_bills.create",), icon="🏷️"),

    # ─── Refunds — cross-cuts sales+purchases ───────────────────
    Feature("refunds_index", "sales", "المرتجعات",
             endpoints=("refunds.index", "refunds.report"),
             permissions=("refunds.view",), icon="↩️"),

    # ─── Inventory ──────────────────────────────────────────────
    Feature("inventory_index", "inventory", "المخزون",
             endpoints=("inventory.index", "inventory.movements",
                        "inventory.inventory_balance"),
             permissions=("inventory.view",), icon="📊"),
    Feature("inventory_warehouses", "inventory", "المخازن",
             endpoints=("inventory.warehouses",),
             permissions=("inventory.manage",), icon="🏬"),
    Feature("inventory_transfers", "inventory", "التحويلات",
             endpoints=("inventory.transfers",),
             permissions=("transfers.view",), icon="🔀"),
    Feature("inventory_counts_index", "inventory", "الجرد",
             endpoints=("inventory_counts.index",
                        "inventory.adjust",
                        "inventory.opening_balance",
                        "inventory.barcodes_picker"),
             permissions=("inventory.manage",), icon="📊"),

    # ─── CRM ────────────────────────────────────────────────────
    Feature("leads_index", "crm", "Leads",
             endpoints=("leads.index",
                        "leads.no_response_index"),
             permissions=("leads.view",), icon="🎯"),
    Feature("crm_campaigns_index", "crm", "الحملات التسويقية",
             endpoints=("crm.campaigns_index",),
             permissions=("crm.campaigns.view",), icon="🎯"),
    Feature("crm_activities_index", "crm", "الأنشطة والمتابعات",
             endpoints=("crm.activities_index",),
             permissions=("crm.activities.view",), icon="📅"),
    Feature("crm_contacts_index", "crm", "جهات الاتصال",
             endpoints=("crm.contacts_index",),
             permissions=("crm.contacts.view",), icon="👥"),
    Feature("crm_analytics", "crm", "تحليلات CRM",
             endpoints=("crm.analytics",),
             permissions=("crm.analytics.view",), icon="📈"),

    # ─── Workflow (tasks + projects + calendar — under crm module) ─
    Feature("tasks_index", "crm", "المهام",
             endpoints=("tasks.index", "tasks.archive_list"),
             permissions=("tasks.view",), icon="✅"),
    Feature("projects_index", "crm", "المشاريع",
             endpoints=("projects.index",),
             permissions=("projects.view",), icon="📂"),
    Feature("calendar_index", "crm", "التقويم",
             endpoints=("calendar.index",),
             permissions=("tasks.view",), icon="📅"),

    # ─── HR ─────────────────────────────────────────────────────
    Feature("hr_index", "hr", "الموظفين",
             endpoints=("hr.index",),
             permissions=("hr.manage",), icon="👤"),
    Feature("hr_departments", "hr", "الأقسام",
             endpoints=("hr.departments",),
             permissions=("hr.manage",), icon="🏢"),
    Feature("hr_attendance", "hr", "الحضور والإجازات",
             endpoints=("hr.attendance", "hr.attendance_policies"),
             permissions=("hr.manage",), icon="📅"),
    Feature("hr_leave_requests", "hr", "طلبات الإجازة",
             endpoints=("hr.leave_requests",),
             permissions=("hr.manage",), icon="🌴"),
    Feature("hr_leave_types", "hr", "أنواع الإجازة",
             endpoints=("hr.leave_types",),
             permissions=("hr.manage",), icon="🌴"),
    Feature("payroll_index", "hr", "الرواتب",
             endpoints=("payroll.index", "payroll.archive"),
             permissions=("employees.view",), icon="💼"),
    Feature("advances_index", "hr", "سلف الموظفين",
             endpoints=("advances.index",),
             permissions=("advances.manage",), icon="💵"),
    Feature("hr_ss_index", "settings", "حسابات الموظفين",
             endpoints=("hr_ss.index",),
             permissions=("users.manage",), icon="🔑"),

    # ─── Cash / item custody ────────────────────────────────────
    Feature("custody_index", "cash_custody", "العهدة النقدية",
             endpoints=("custody.index",),
             permissions=("custody.manage",), icon="💵"),
    Feature("item_custody_index", "cash_custody", "العهدة العينية",
             endpoints=("item_custody.index",),
             permissions=("custody.manage",), icon="📦"),

    # ─── Manufacturing ──────────────────────────────────────────
    Feature("manufacturing_boms_index", "manufacturing",
             "تركيبات المنتجات (BOM)",
             endpoints=("manufacturing.boms_index",),
             permissions=("manufacturing.view",), icon="🧬"),
    Feature("manufacturing_work_orders_index", "manufacturing",
             "أوامر الإنتاج",
             endpoints=("manufacturing.work_orders_index",),
             permissions=("manufacturing.view",), icon="⚙️"),
    Feature("manufacturing_reports", "manufacturing",
             "تقارير التكلفة الصناعية",
             endpoints=("manufacturing.reports",),
             permissions=("manufacturing.view",), icon="📊"),

    # ─── Reports (sub-features under 'reports' module) ──────────
    Feature("reports_cashier_sales", "pos", "تقرير الكاشير",
             endpoints=("reports.cashier_sales",),
             permissions=("reports.cashier_sales",), icon="💰"),
    Feature("reports_profitability", "pos", "الربحية",
             endpoints=("reports.profitability",),
             permissions=("reports.profitability",), icon="📈"),
    Feature("reports_employees_index", "employee_reports",
             "تقارير الموظفين اليومية",
             endpoints=("reports.employees_index",),
             permissions=("employee_reports.view",), icon="📝"),
    Feature("reports_metric_logs", "settings", "سجل المؤشرات",
             endpoints=("reports.metric_logs",),
             permissions=("users.manage",), icon="📊"),
    Feature("settings_employee_reports_index", "settings",
             "إعدادات تقارير الموظفين",
             endpoints=("settings_employee_reports.index",),
             permissions=("users.manage",), icon="⚙️"),

    # ─── Evaluations ────────────────────────────────────────────
    Feature("evaluations_index", "evaluations", "تقييمات الأداء",
             endpoints=("evaluations.index",
                        "evaluations.logs_index"),
             permissions=("evaluations.manage",), icon="⭐"),

    # ─── Agent (accountant) ─────────────────────────────────────
    Feature("agent_index", "agent", "المحاسب الذكي",
             endpoints=("agent.index",),
             permissions=("agent.use",), icon="🤖"),

    # ─── Settings + system ──────────────────────────────────────
    Feature("settings_roles_index", "settings",
             "المستخدمين والأدوار",
             endpoints=("settings_roles.index",),
             permissions=("users.manage",), icon="🔐"),
    Feature("settings_api_tokens_index", "settings",
             "مفاتيح الـ API",
             endpoints=("settings_api_tokens.index",),
             permissions=("api_tokens.manage",), icon="🔑"),
    Feature("settings_activity_index", "settings",
             "نشاط الموظفين",
             endpoints=("settings_activity.index",),
             permissions=("activity_log.view",), icon="👣"),
    Feature("settings_backup_index", "settings",
             "نسخة احتياطية",
             endpoints=("settings_backup.index",),
             permissions=("backup.download",), icon="📥"),
    Feature("settings_usage_index", "settings",
             "الاستخدام والحدود",
             endpoints=("settings_usage.index",),
             permissions=("settings_usage.view",), icon="📊"),
    Feature("companies_edit", "settings", "بيانات الشركة",
             endpoints=("companies.edit",),
             permissions=("users.manage",), icon="🏢"),
    Feature("audit_log_index", "settings", "سجل التدقيق",
             endpoints=("audit_log.index",),
             permissions=("users.view",), icon="🔍"),
    Feature("user_files_index", "settings", "ملفاتي",
             endpoints=("user_files.index",),
             permissions=(),  # login_required only
             icon="📁"),

    # ─── Support / help ─────────────────────────────────────────
    Feature("support_index", "settings", "الدعم الفني",
             endpoints=("support.index",),
             permissions=(),
             icon="🆘"),
    Feature("support_admin_index", "settings", "لوحة الدعم",
             endpoints=("support_admin.index",),
             permissions=("support.manage_tickets",), icon="🛠"),
)


# ─── Lookups (built once at import time) ─────────────────────────────
_MODULES_BY_CODE = {m.code: m for m in _MODULES}
_FEATURES_BY_CODE = {f.code: f for f in _FEATURES}
_FEATURES_BY_MODULE = {}
for _f in _FEATURES:
    _FEATURES_BY_MODULE.setdefault(_f.module, []).append(_f)

# permission → module (walks module.permission_prefixes; longest match wins
# for correctness with reports.cashier_sales vs reports.*)
_PERM_LOOKUP = []
for _m in _MODULES:
    for _p in _m.permission_prefixes:
        _PERM_LOOKUP.append((_p, _m.code))
# Sort longest-prefix-first so 'reports.cashier_sales' matches 'pos'
# (a full string) BEFORE 'reports.' matches 'reports'.
_PERM_LOOKUP.sort(key=lambda x: (-len(x[0]), x[0]))

# endpoint → feature (exact match)
_ENDPOINT_TO_FEATURE = {}
for _f in _FEATURES:
    for _ep in _f.endpoints:
        _ENDPOINT_TO_FEATURE[_ep] = _f

# blueprint prefix → module (for the FeatureFlag middleware).
_BLUEPRINT_TO_MODULE = {}
for _m in _MODULES:
    for _bp in _m.blueprint_prefixes:
        _BLUEPRINT_TO_MODULE[_bp] = _m.code


def all_modules(include_core=True):
    out = list(_MODULES)
    if not include_core:
        out = [m for m in out if not m.is_core]
    out.sort(key=lambda m: (m.sort_order, m.code))
    return out


def all_features():
    return list(_FEATURES)


def features_for_module(code):
    return list(_FEATURES_BY_MODULE.get(code, ()))


def get_module(code):
    return _MODULES_BY_CODE.get(code)


def module_for_permission(perm_code):
    """Replaces the old plan_gating._PREFIX_TO_MODULE lookup.
    Longest-prefix match — 'reports.cashier_sales' resolves to
    'pos' before the shorter 'reports.' catch-all fires."""
    if not perm_code:
        return None
    for prefix, module in _PERM_LOOKUP:
        if prefix.endswith(".") and perm_code.startswith(prefix):
            return module
        if not prefix.endswith(".") and perm_code == prefix:
            return module
    return None


def module_for_endpoint(endpoint):
    """endpoint 'custody.new' → 'cash_custody' (via the blueprint
    map). Falls back to feature lookup, then permission-prefix
    lookup on the endpoint string as a last resort."""
    if not endpoint:
        return None
    # Exact feature match wins
    f = _ENDPOINT_TO_FEATURE.get(endpoint)
    if f is not None:
        return f.module
    # Otherwise blueprint prefix (fixes the FeatureFlag key bug)
    bp = endpoint.split(".", 1)[0]
    m = _BLUEPRINT_TO_MODULE.get(bp)
    if m is not None:
        return m
    return None


def feature_for_endpoint(endpoint):
    return _ENDPOINT_TO_FEATURE.get(endpoint)


def module_for_blueprint(bp_name):
    return _BLUEPRINT_TO_MODULE.get(bp_name)


def all_module_codes():
    """Convenience for tests / drift checks."""
    return {m.code for m in _MODULES}
