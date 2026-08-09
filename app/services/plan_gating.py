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


# ─── MARSOUD-ROLES-REFLECT-SCOPE (2026-08-09) — per-company effective set ─
def effective_modules(company):
    """The set of module codes actually enabled for `company`
    RIGHT NOW — plan modules ∪ always-allowed (∪ future grant
    exceptions, minus future deny exceptions).

    Single source of truth for "what does this company actually
    have". Today it wraps plan.modules + intended-plan fallback;
    when the company-level plan-exception ticket lands, this is
    the ONE function that widens. Every caller (roles page, POST
    validator, future sidebar filter) reads the same set, so
    grants/denies only need one insert point.

    Falls back from subscription_plan -> intended_plan the same
    way plan_allows does (MARSOUD-PLAN-BUNDLE-FIXES-01) so a
    company mid-onboarding renders correctly.

    Always includes _ALWAYS_ALLOWED so 'settings' is never
    hidden — otherwise the owner locks themselves out of the
    very page they're using.
    """
    modules = set(_ALWAYS_ALLOWED)
    if company is None:
        return modules
    plan = (getattr(company, "subscription_plan", None)
            or getattr(company, "intended_plan", None))
    if plan is not None:
        modules |= set(plan.modules or [])
    # Future: modules = (modules | grants) - denies
    return modules


def effective_subitems(company):
    """Same shape for the per-sidebar-item catalog. None means
    'all allowed' — matches the Plan.subitems back-compat
    convention at plan.py:49-60. Not consumed by this ticket
    but exposed now so the companion 'sidebar items' ticket
    plugs in without re-writing every caller."""
    if company is None:
        return None
    plan = (getattr(company, "subscription_plan", None)
            or getattr(company, "intended_plan", None))
    if plan is None:
        return None
    return plan.subitems


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
        # MARSOUD-PLAN-SUBITEMS-27 (2026-08-09) — the wizards page
        # used to be lumped under journals.index via
        # endpoint_to_subitem; now it's its own catalog entry.
        # See the guard on that shortcut below.
        ("accounting_ops.index", "العمليات المحاسبية", "🧮"),
    ],
    "sales": [
        ("pos.index", "نقطة البيع", "🛒"),
        ("invoices.index", "الفواتير", "🧾"),
        ("customers.index", "العملاء", "👥"),
        # MARSOUD-PLAN-SUBITEMS-27 (2026-08-09). Note the label
        # collision with purchases/recurring_bills.index — that's
        # the purchases side; this one is the sales side and it's
        # a separate blueprint (recurring_invoices, not
        # recurring_bills). Both rows live in their own section.
        ("recurring_invoices.index", "الفواتير المتكررة", "🔁"),
        ("pos.shifts",  "الورديات",  "🕒"),
        ("pos.history", "سجل نقطة البيع", "📜"),
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
        # MARSOUD-PLAN-SUBITEMS-27 (2026-08-09) — 8 inventory
        # rows that used to lump under inventory.index (or were
        # ungated entirely: inventory_counts.index,
        # products.hierarchy) become togglable independently.
        # Migration f8c2e5a9d4b1 appends the lumped ones onto
        # plans that already had inventory.index in their list,
        # so no visibility change on existing plans.
        ("inventory.adjust",           "تسوية المخزون",       "⚖️"),
        ("inventory.opening_balance",  "رصيد افتتاحي مخزون",  "🎬"),
        ("inventory.movements",        "حركات المخزون",       "🔀"),
        ("inventory.transfers",        "تحويلات المخزون",     "🚚"),
        ("inventory.inventory_balance","رصيد المخزون",        "📊"),
        ("inventory.barcodes_picker",  "طباعة الباركود",      "🖨️"),
        ("inventory_counts.index",     "الجرد",               "🔢"),
        ("products.hierarchy",         "التصنيفات والفئات",   "🏷️"),
    ],
    "crm": [
        ("leads.index", "Leads", "🎯"),
        ("crm.campaigns_index", "الحملات التسويقية", "🎯"),
        ("crm.activities_index", "الأنشطة والمتابعات", "📅"),
        ("crm.contacts_index", "جهات الاتصال", "👥"),
        ("crm.analytics", "تحليلات CRM", "📈"),
        # MARSOUD-PLAN-SUBITEMS-27 (2026-08-09).
        ("leads.no_response_index", "العملاء بدون رد", "🕐"),
    ],
    "workflow": [
        ("tasks.index", "المهام", "✅"),
        ("projects.index", "المشاريع", "📂"),
        ("calendar.index", "التقويم", "📅"),
        # MARSOUD-PLAN-SUBITEMS-27 (2026-08-09).
        ("tasks.archive_mine", "أرشيف مهامي", "📦"),
    ],
    "hr": [
        ("hr.index", "الموظفين", "👤"),
        ("payroll.index", "الرواتب", "💼"),
        ("hr.attendance", "الحضور والإجازات", "📅"),
        ("hr_ss.index", "حسابات الموظفين", "🔑"),
        # MARSOUD-PLAN-SUBITEMS-27 (2026-08-09) — HR + Custody +
        # Evaluations rows land under the hr bucket to mirror
        # where base.html renders them today (:512-516, :666-673).
        # No new section keys → no SECTION_LABEL_AR / SECTION_
        # REQUIRES_MODULES churn.
        ("hr.departments",         "الأقسام",                 "🏛️"),
        ("hr.leave_types",         "أنواع الإجازات",          "📂"),
        ("hr.leave_requests",      "طلبات الإجازات",          "📝"),
        ("hr.attendance_policies", "سياسات الحضور",           "🕐"),
        ("advances.index",         "سلف الموظفين",            "💵"),
        # payroll.archive serves the terminated/suspended-employee
        # list — that's the label the sidebar uses today. Ticket
        # said "أرشيف الرواتب" but confirmed we keep the sidebar
        # wording so the checkbox matches the row the user sees.
        ("payroll.archive",        "الموظفون السابقون",       "👥"),
        ("custody.index",          "العهدة النقدية",          "💰"),
        ("item_custody.index",     "العهدة العينية",          "📦"),
        ("evaluations.index",      "التقييمات",               "⭐"),
        ("evaluations.logs_index", "سجل التقييمات",           "📜"),
    ],
    "settings": [
        ("settings_roles.index", "المستخدمين والأدوار", "🔐"),
        ("settings_api_tokens.index", "مفاتيح الـ API", "🔑"),
        ("settings_activity.index", "نشاط الموظفين", "👣"),
        ("settings_backup.index", "نسخة احتياطية (Excel)", "📥"),
        ("payment_methods.index", "طرق الدفع", "💳"),
        ("companies.edit", "بيانات الشركة", "🏢"),
        ("audit_log.index", "سجل التدقيق", "🔍"),
        # MARSOUD-PLAN-SUBITEMS-27 (2026-08-09).
        ("settings_employee_reports.index", "إعدادات تقارير الموظفين", "📝"),
        ("settings_usage.index",            "إعدادات الاستخدام",       "📊"),
        ("user_files.index",                "ملفاتي",                  "📁"),
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
    # MARSOUD-PLAN-SUBITEMS-27 (2026-08-09) — the wizards' INDEX
    # is now its own catalog entry (accounting_ops.index), so
    # only the SUB-pages (accounting_ops.new_journal, etc.) still
    # ride on journals.index. Without this guard the index would
    # double-gate: hit accounting_ops.index → the direct-match
    # path at line 372 catches it FIRST, so this branch never
    # runs for the index — but we still exclude it explicitly so
    # a reader doesn't chase the wrong lead.
    if (endpoint.startswith("accounting_ops.")
            and endpoint != "accounting_ops.index"):
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
