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

# MARSOUD-SUPERADMIN-CONTROL-01 T1 (2026-08-08) — the real source
# of truth moved to app/services/feature_registry.py. This dict is
# kept as a computed alias so nothing that imports it breaks; the
# entries below are DERIVED from the registry at import time so
# there's zero risk of drift. Every code change goes into the
# registry file — this dict updates itself.
def _build_prefix_to_module():
    from app.services.feature_registry import all_modules
    out = {}
    for m in all_modules():
        for p in m.permission_prefixes:
            out[p] = m.code
    return out


_PREFIX_TO_MODULE = _build_prefix_to_module()

# The rest of this file assumes _PREFIX_TO_MODULE dict semantics
# from the pre-registry era. If you're adding a NEW permission
# prefix, add it to the appropriate Module in feature_registry.py
# — NOT here.

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
      - the company has NEITHER a promoted plan NOR an intended plan
        (pre-/choose-plan onboarding — back-compat "don't lock")
      - the plan's allowed_modules list contains the module

    MARSOUD-PLAN-BUNDLE-FIXES-01 (2026-08-07) — previously this
    checked ONLY `subscription_plan` (which resolves via `plan_id`),
    so a company that picked Starter at /choose-plan but hadn't been
    promoted from `intended_plan_id` → `plan_id` got full access for
    the entire trial. That's the "Starter has access to everything"
    the user reported. Now: fall back to `intended_plan_id` when
    `plan_id` is NULL, matching the convention already used by
    quotas.py, saas_billing.py, platform_metrics.py.
    """
    if action in _ALWAYS_READABLE:
        return True
    module = action_module(action)
    if not module:
        return True
    if module in _ALWAYS_ALLOWED:
        return True
    if not company:
        return True
    plan = getattr(company, "subscription_plan", None)
    if plan is None:
        # Fall back to the picked-but-not-promoted plan. See docstring.
        plan = getattr(company, "intended_plan", None)
    if plan is None:
        # Truly no pick yet — pre-/choose-plan onboarding.
        return True
    return module in plan.modules


# ─── MARSOUD-58 / MARSOUD-PERM-EXPAND — per-section sub-item catalog ───
# Sections mirror the 9 sidebar sections currently rendered by base.html.
# Each entry lists the sub-items (endpoint, label, icon) the section can
# contain. This is the source of truth for the /admin/plans nested-
# checkbox UI, so super-admin can toggle any item on/off per package.
SUB_ITEM_CATALOG = {
    "main": [
        ("dashboard.index", "لوحة المعلومات", "📊"),
        # MARSOUD-INSIGHTS-AGENT-PROFESSIONAL follow-up (2026-08-06) —
        # the analyst tab was added to the sidebar under المحاسب الذكي
        # but plan-managed companies (subitems list explicit) were
        # hiding the row because the endpoint wasn't in the catalog.
        # Add it here so super-admin can opt each plan into it via
        # the admin/plans nested-checkbox UI.
        ("agent.insights_index", "المحلل الذكي", "🔍"),
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
        during the trial, ALL sub-items are shown so a fine-grained
        super-admin toggle doesn't surprise a trial user. The coarse
        module gate on `plan_allows` (which the sidebar template AND-s
        with this one via `has_permission`) is what enforces the
        picked-plan restrictions during trial — see MARSOUD-PLAN-
        BUNDLE-FIXES-01.
    """
    if not company:
        return True
    # Trial keeps sub-items unlocked; the coarse module gate on
    # plan_allows already blocks features that aren't in the picked
    # plan's modules. Sub-item toggles are a fine-grained super-admin
    # customization that shouldn't surprise a trial user.
    if _company_in_trial(company):
        return True
    plan = getattr(company, "subscription_plan", None)
    if plan is None:
        # Fall back to the picked-but-not-promoted plan, same
        # convention as plan_allows.
        plan = getattr(company, "intended_plan", None)
    if plan is None:
        return True
    items = plan.subitems
    if items is None:
        # Back-compat: NULL allowed_subitems = no filtering.
        return True
    return endpoint in items


def _company_in_trial(company):
    """True iff the company is still inside its subscription window.
    Named "trial" because for freshly-registered companies the window
    IS the trial. Same predicate also protects any renewal buffer.

    MARSOUD-PLAN-BUNDLE-FIXES-01 (2026-08-07) — this predicate no
    longer implies "full access" on its own. Once a company picks a
    plan (intended OR promoted), gating uses that plan's modules
    even inside the trial window. Kept as a helper because callers
    may still want to distinguish "paying" from "trialing" for UI or
    billing reasons unrelated to feature gating.
    """
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
    # MARSOUD-ACCOUNTING-OPS — the 🧮 wizards are journal creation with the
    # accounts picked for you, so they ride on the القيود اليومية sub-item
    # rather than adding one of their own. Deliberate: a brand-new
    # sub-item is absent from every existing plan's stored
    # allowed_subitems, so the page would 403 for every tenant until a
    # super-admin ticked it on each plan.
    if endpoint.startswith("accounting_ops."):
        return "journals.index"
    # Special-case inventory: warehouses is a separate sub-item.
    if endpoint == "inventory.warehouses" or endpoint.startswith("inventory.warehouse_"):
        return "inventory.warehouses"
    if endpoint.startswith("inventory."):
        return "inventory.index"
    # Special-case HR: attendance has its own sub-item.
    # MARSOUD-4-BRANCH-REPAIR (2026-08-08) — hr.employee_attendance_detail
    # doesn't match the "hr.attendance_" prefix, so it fell through to
    # hr.index. A tenant with hr.attendance in allowed_subitems but not
    # hr.index would then 403 on the per-employee detail. Group all HR
    # attendance-family endpoints (grid + per-employee detail) under the
    # hr.attendance gate explicitly.
    if (endpoint == "hr.attendance"
            or endpoint.startswith("hr.attendance_")
            or endpoint == "hr.employee_attendance_detail"):
        return "hr.attendance"
    if endpoint.startswith("hr."):
        return "hr.index"
    # Generic: same blueprint as a sub-item endpoint.
    bp = endpoint.split(".")[0]
    for ep in ALL_SUB_ITEM_ENDPOINTS:
        if ep.split(".")[0] == bp:
            return ep
    return None
