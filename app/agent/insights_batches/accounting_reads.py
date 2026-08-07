"""MARSOUD-INSIGHTS-AGENT-PROFESSIONAL (2026-08-06) — re-mount the
accountant agent's read-only tools on the analyst.

The accountant already has 22 read-only tools spanning accounts,
customers, invoices, journals, vendors, vendor bills, payroll,
advances, fixed assets, POS/shifts, inventory, and full reports.
Historically the analyst couldn't see any of them — the two agents
were disjoint by history, not by design. The ticket says the
analyst gets EVERY read tool the system has, which starts here.

Approach:
  · Import `TOOL_SCHEMAS` + `execute_tool` from `app.agent.tools`
  · Filter out the 4 known writes
    (`create_customer`, `create_journal_entry`, `create_invoice`,
    `record_invoice_payment`)
  · For each remaining schema, register a thin wrapper that
    forwards to `execute_tool` — same name, same schema, same
    behaviour. Zero code duplication.

The `explain_concept` tool is kept — it's not a data read, but the
analyst benefits from being able to explain a term (e.g. "what is
DSO?") in the same turn as it computes it.

Per-tool permissions: the accountant is gated at the ROUTE by
`agent.write` (which blindly grants read too). On the analyst side
the route gate is `insights.use`, which is broader (sales_manager,
project_manager, hr_manager, ceo — not all of them have
`payroll.view` or `customers.view`). So the wrapper adds per-tool
`_has_perm` gates for the sensitive-data reads. The safe rule of
thumb: any tool whose result includes salary numbers, customer
balances, party ledgers, or vendor terms gets a per-tool gate.
"""
from __future__ import annotations
from app.agent.insights_catalog import register, has_perm, perm_denied


# The 4 accountant writes — never surface these on the analyst.
_ACCOUNTANT_WRITES = {
    "create_customer",
    "create_journal_entry",
    "create_invoice",
    "record_invoice_payment",
}

# Per-tool permission for the sensitive reads. Everything not listed
# here inherits the route-level `insights.use` only.
_TOOL_PERMS = {
    "list_customers":         "customers.view",
    "get_invoice":            "customers.view",
    "list_invoices":          "customers.view",
    "party_statement":        "party_ledger.view",
    "list_vendors":           "partners.manage",
    "list_vendor_bills":      "partners.manage",
    "get_vendor_bill":        "partners.manage",
    "list_payroll_runs":      "payroll.view",
    "list_employee_advances": "payroll.view",
    "list_fixed_assets":      "assets.manage",
    "run_report":             "reports.view",
    "get_journal_entry":      "journals.view",
    "search_journals":        "journals.view",
    "list_accounts":          "journals.view",
    "get_stock_level":        "inventory.view",
    "list_low_stock":         "inventory.view",
    "get_product_profitability": "reports.profitability",
    "get_top_products":       "reports.view",
    "get_cashier_sales":      "reports.cashier_sales",
    "get_open_shifts":        "pos.use",
    "get_shift_summary":      "pos.use",
    "transfer_history":       "transfers.view",
    # explain_concept — no gate; it's pure documentation
}


def _make_wrapper(name, perm):
    """Return a fn `(args, company_id, user_id) -> dict` that
    checks the per-tool permission then delegates to the
    accountant's execute_tool. Captured-by-closure form so the
    for-loop below doesn't shadow the name across iterations."""
    def _wrap(args, company_id, user_id):
        if perm and not has_perm(user_id, company_id, perm):
            return perm_denied(perm)
        from app.agent.tools import execute_tool
        return execute_tool(name, args, company_id, user_id)
    _wrap.__name__ = f"insights_wrap_{name}"
    return _wrap


def _register_all():
    """Called once at import time. Reads TOOL_SCHEMAS, skips
    writes, registers a wrapper per read tool."""
    from app.agent.tools import TOOL_SCHEMAS
    for schema in TOOL_SCHEMAS:
        name = schema["name"]
        if name in _ACCOUNTANT_WRITES:
            continue
        perm = _TOOL_PERMS.get(name)
        register(
            name=name,
            description=schema["description"],
            input_schema=schema["input_schema"],
            permission=perm,
            backend_module="accountant_read_remount",
        )(_make_wrapper(name, perm))


_register_all()
