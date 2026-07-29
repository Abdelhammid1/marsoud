"""MARSOUD-57.2 — plan-based permission gating.

Sits as a layer above the role/permission system: even if a user's role
grants `pos.use`, if the company's plan doesn't include the "pos" module,
`has_permission("pos.use", company)` must return False.

The gating is a coarse classification — every action maps to exactly one
module. Modules currently in use:
  accounting   journals, accounts, payment methods, partners
  sales        invoices, products, customers
  inventory    inventory, transfers
  purchases    vendor bills, vendors
  pos          POS, shifts, cashier sales, profitability
  crm          leads, tasks, projects
  hr           HR, payroll, employee accounts
  reports      reports.view (financial reports dashboard)
  agent        AI agent
  settings     users.view/manage, roles, company settings — always allowed
  platform     superadmin.* — gating bypassed for super-admins
"""

# Single source of truth: action prefix → module code.
# When you add a new permission code, also add its prefix here.
_PREFIX_TO_MODULE = {
    # accounting
    "journals.": "accounting",
    "accounts.": "accounting",
    "payment_methods.": "accounting",
    "partners.": "accounting",
    "assets.": "accounting",
    # sales
    "invoices.": "sales",
    "products.": "sales",
    # MARSOUD-REFUNDS-01 — refunds are gated on the sales module (any
    # plan that has sales gets refund view + issue). Sales and purchase
    # refunds live in one blueprint; the module lookup for both
    # `refunds.view`/`refunds.manage` returns "sales".
    "refunds.": "sales",
    # crm
    "leads.": "crm",
    "tasks.": "crm",
    "projects.": "crm",
    # inventory + purchases
    "inventory.": "inventory",
    "transfers.": "inventory",
    "vendor_bills.": "purchases",
    "recurring_bills.": "purchases",
    "forecast.": "purchases",
    # pos
    "pos.": "pos",
    "shifts.": "pos",
    "reports.cashier_sales": "pos",
    "reports.profitability": "pos",
    # hr
    "hr.": "hr",
    "payroll.": "hr",
    # MARSOUD-EMPLOYEE-DAILY-REPORTS — own coarse module so plans can
    # ship without it (the ticket wants Super Admin control per package).
    "employee_reports.": "employee_reports",
    # MARSOUD-MANUFACTURING-01 — own coarse module (BOM + work orders).
    "manufacturing.": "manufacturing",
    # MARSOUD-EVALUATIONS-PRO-GATING (Batch 5 Ticket 6, 2026-07-29) —
    # employee-evaluations feature is Pro-only. Starter/Growth companies
    # get 403 on /evaluations/* until they upgrade.
    "evaluations.": "evaluations",
    # reports (catch-all after the pos-specific overrides above)
    "reports.": "reports",
    # agent
    "agent.": "agent",
    # settings — always on
    "users.": "settings",
}

# Modules that are ALWAYS allowed regardless of plan (auth + basic admin).
_ALWAYS_ALLOWED = {"settings"}

# Specific ACTIONS that stay readable even after the module is disabled.
# The rule: if a company had these features and its plan is later
# downgraded, the OLD data must remain viewable (per ticket edge cases
# like MARSOUD-REFUNDS-01: "شركة على باقة مفيهاش الميزة عندها مرتجعات
# قديمة من قبل — تفضل تتعرض للقراءة بس مش تتعدل"). Read-only, no writes.
_ALWAYS_READABLE = {
    "refunds.view",
}


def action_module(action):
    """Return the module code for a permission action, or None if unmapped."""
    # Exact match first (e.g. "reports.cashier_sales") then prefix.
    if action in _PREFIX_TO_MODULE:
        return _PREFIX_TO_MODULE[action]
    for prefix, module in _PREFIX_TO_MODULE.items():
        if prefix.endswith(".") and action.startswith(prefix):
            return module
    return None


def plan_allows(action, company):
    """Return True if the company's plan allows this action's module.

    Returns True when:
      - the action is in _ALWAYS_READABLE (legacy data stays viewable
        even if the module is disabled)
      - the action doesn't map to a gated module (treated as ungated)
      - the module is in the always-allowed set
      - the company has no plan assigned (legacy / not yet backfilled)
      - the plan's allowed_modules list contains the module
    """
    if action in _ALWAYS_READABLE:
        return True
    module = action_module(action)
    if not module:
        return True
    if module in _ALWAYS_ALLOWED:
        return True
    if not company or not getattr(company, "subscription_plan", None):
        # No plan assigned → don't lock anything (back-compat).
        return True
    plan = company.subscription_plan
    return module in plan.modules


# ─── MARSOUD-58 / MARSOUD-PERM-EXPAND — per-section sub-item catalog ───
# Sections mirror the 9 sidebar sections currently rendered by base.html.
# Each entry lists the sub-items (endpoint, label, icon) the section can
# contain. This is the source of truth for the /admin/plans nested-
# checkbox UI, so super-admin can toggle any item on/off per package.
SUB_ITEM_CATALOG = {
    "main": [
        ("dashboard.index", "لوحة المعلومات", "📊"),
    ],
    "accounting": [
        ("journals.index", "القيود اليومية", "📒"),
        ("accounts.index", "دليل الحسابات", "🌳"),
        ("assets.index", "الأصول الثابتة", "🏗️"),
        ("reports.index", "التقارير المالية", "📑"),
        ("party_ledger.index", "كشف حساب طرف", "📒"),
    ],
    "sales": [
        ("pos.index", "نقطة البيع", "🛒"),
        ("invoices.index", "الفواتير", "🧾"),
        ("customers.index", "العملاء", "👥"),
    ],
    "purchases": [
        ("vendor_bills.index", "فواتير الموردين", "📥"),
        ("recurring_bills.index", "الفواتير المتكررة", "🔁"),
        ("forecast.index", "الفواتير الجايّة", "📅"),
        ("vendors.index", "الموردين", "🏢"),
    ],
    "refunds": [
        ("refunds.index", "كل المرتجعات", "📋"),
        ("refunds.report", "تقرير المرتجعات", "📊"),
    ],
    "employee_reports": [
        ("reports.employees_index", "تقارير الموظفين اليومية", "📝"),
    ],
    "manufacturing": [
        ("manufacturing.boms_index", "تركيبات المنتجات (BOM)", "🧬"),
        ("manufacturing.work_orders_index", "أوامر الإنتاج", "⚙️"),
        ("manufacturing.reports", "تقارير التكلفة الصناعية", "📊"),
    ],
    "inventory": [
        ("inventory.index", "المخزون", "📊"),
        ("inventory.warehouses", "المخازن", "🏬"),
        ("products.index", "المنتجات والخدمات", "🏷️"),
    ],
    "crm": [
        ("leads.index", "Leads", "🎯"),
        ("crm.campaigns_index", "الحملات التسويقية", "🎯"),
        ("crm.activities_index", "الأنشطة والمتابعات", "📅"),
        ("crm.contacts_index", "جهات الاتصال", "👥"),
        ("crm.analytics", "تحليلات CRM", "📈"),
    ],
    "workflow": [
        ("tasks.index", "المهام", "✅"),
        ("projects.index", "المشاريع", "📂"),
        ("calendar.index", "التقويم", "📅"),
    ],
    "hr": [
        ("hr.index", "الموظفين", "👤"),
        ("payroll.index", "الرواتب", "💼"),
        ("hr.attendance", "الحضور والإجازات", "📅"),
        ("hr_ss.index", "حسابات الموظفين", "🔑"),
    ],
    "settings": [
        ("settings_roles.index", "المستخدمين والأدوار", "🔐"),
        ("settings_api_tokens.index", "مفاتيح الـ API", "🔑"),
        ("settings_activity.index", "نشاط الموظفين", "👣"),
        ("settings_backup.index", "نسخة احتياطية (Excel)", "📥"),
        ("payment_methods.index", "طرق الدفع", "💳"),
        ("companies.edit", "بيانات الشركة", "🏢"),
        ("audit_log.index", "سجل التدقيق", "🔍"),
    ],
}

SECTION_LABEL_AR = {
    "main": "الرئيسية",
    "accounting": "المالية والمحاسبة",
    "sales": "المبيعات",
    "purchases": "المشتريات",
    "refunds": "المرتجعات",
    "employee_reports": "تقارير الموظفين",
    "manufacturing": "التصنيع",
    "inventory": "المخزون",
    "crm": "العملاء المحتملين (CRM)",
    "workflow": "إدارة العمل",
    "hr": "الموارد البشرية",
    "settings": "الإعدادات والنظام",
}

# Sections need at least ONE of these modules to be visible. Empty list
# means "section is always available regardless of plan".
SECTION_REQUIRES_MODULES = {
    "main": [],
    "accounting": ["accounting", "reports"],
    "sales": ["sales"],
    "purchases": ["purchases"],
    # MARSOUD-REFUNDS-01 — refunds visible if either side is available.
    "refunds": ["sales", "purchases"],
    "employee_reports": ["employee_reports"],
    "manufacturing": ["manufacturing"],
    "inventory": ["inventory"],
    "crm": ["crm"],
    "workflow": ["crm"],
    "hr": ["hr"],
    "settings": [],
}

ALL_SUB_ITEM_ENDPOINTS = [
    ep for items in SUB_ITEM_CATALOG.values() for (ep, _, _) in items
]


def subitem_allowed(endpoint, company):
    """True if the company's plan allows this sub-item endpoint.

    Returns True when:
      - the company has no plan attached (back-compat)
      - the plan's allowed_subitems is None (legacy / not yet set)
      - the endpoint is in the plan's allowed_subitems list
      - the company is inside its trial window (MARSOUD-CHOOSE-PLAN):
        during the trial, ALL features are enabled regardless of the
        picked plan so the user experiences the full product before
        deciding whether to keep the chosen tier.
    """
    if not company:
        return True
    # MARSOUD-CHOOSE-PLAN — trial window overrides plan gating.
    if _company_in_trial(company):
        return True
    plan = getattr(company, "subscription_plan", None)
    if not plan:
        return True
    items = plan.subitems
    if items is None:
        # Back-compat: NULL allowed_subitems = no filtering.
        return True
    return endpoint in items


def _company_in_trial(company):
    """MARSOUD-CHOOSE-PLAN — true iff the company is still inside its
    subscription window. Named "trial" because for freshly-registered
    companies the window IS the trial. Same predicate also protects
    any renewal buffer (the app should stay full-features during the
    active window regardless of trial-vs-paid nuance)."""
    from datetime import datetime
    expires = getattr(company, "subscription_expires_at", None)
    return bool(expires and expires > datetime.utcnow())


def endpoint_to_subitem(endpoint):
    """Map a request endpoint to the sub-item key that gates it.

    Returns None for endpoints that aren't gated by any sub-item — e.g.
    superadmin.*, auth.*, cron.*, portal_emp.*, notifications.*.
    """
    if not endpoint:
        return None
    # Bypass exempt blueprints entirely.
    if endpoint.startswith(("superadmin.", "auth.", "cron.", "portal_emp.",
                             "portal.", "notifications.", "static",
                             "invitations.")):
        return None
    # Direct match in the catalog.
    if endpoint in ALL_SUB_ITEM_ENDPOINTS:
        return endpoint
    # Special-case inventory: warehouses is a separate sub-item.
    if endpoint == "inventory.warehouses" or endpoint.startswith("inventory.warehouse_"):
        return "inventory.warehouses"
    if endpoint.startswith("inventory."):
        return "inventory.index"
    # Special-case HR: attendance has its own sub-item.
    if endpoint == "hr.attendance" or endpoint.startswith("hr.attendance_"):
        return "hr.attendance"
    if endpoint.startswith("hr."):
        return "hr.index"
    # Generic: same blueprint as a sub-item endpoint.
    bp = endpoint.split(".")[0]
    for ep in ALL_SUB_ITEM_ENDPOINTS:
        if ep.split(".")[0] == bp:
            return ep
    return None
