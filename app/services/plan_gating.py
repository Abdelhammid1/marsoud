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
    # crm
    "leads.": "crm",
    "tasks.": "crm",
    "projects.": "crm",
    # inventory + purchases
    "inventory.": "inventory",
    "transfers.": "inventory",
    "vendor_bills.": "purchases",
    # pos
    "pos.": "pos",
    "shifts.": "pos",
    "reports.cashier_sales": "pos",
    "reports.profitability": "pos",
    # hr
    "hr.": "hr",
    "payroll.": "hr",
    # reports (catch-all after the pos-specific overrides above)
    "reports.": "reports",
    # agent
    "agent.": "agent",
    # settings — always on
    "users.": "settings",
}

# Modules that are ALWAYS allowed regardless of plan (auth + basic admin).
_ALWAYS_ALLOWED = {"settings"}


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
      - the action doesn't map to a gated module (treated as ungated)
      - the module is in the always-allowed set
      - the company has no plan assigned (legacy / not yet backfilled)
      - the plan's allowed_modules list contains the module
    """
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
