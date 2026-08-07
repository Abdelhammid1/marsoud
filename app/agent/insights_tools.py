"""MARSOUD-INSIGHTS-AGENT-PROFESSIONAL (2026-08-06) — thin facade
over the analyst tool registry.

Was: a single 400-line file with 5 hand-written tools. Now: this
module just imports every batch by side effect (each batch module
stacks its @register decorators), then re-exports the assembled
schema list + execute helper so `app/routes/agent.py::insights_chat`
doesn't have to change.

To ADD a new analyst tool: pick a batch module (or create one under
`insights_batches/`), decorate the backing function with
`@register(name, description, input_schema, permission=…)`, and
add its module to the import list below.

The old `_todays_summary`, `_tasks_stats`, `_employees_performance`,
`_overdue_items`, `_module_activity` symbols are kept as aliases at
the bottom of this file for backward compatibility with
`tests/audit_insights_agent.py` (checks #5, #6, #7 import them).
"""
from __future__ import annotations

# Import every batch by side effect. Order matters ONLY for the
# tools-array order the model sees (Python's dict preserves
# insertion order), which we keep stable so DeepSeek's prompt cache
# stays warm.
from app.agent.insights_batches import core_reads          # 5 originals
from app.agent.insights_batches import composites          # 3 composites
from app.agent.insights_batches import accounting_reads    # 22 accountant reads
from app.agent.insights_batches import people_reads        # 7 HR reads
from app.agent.insights_batches import crm_reads           # 6 CRM reads
from app.agent.insights_batches import tasks_reads         # 4 tasks reads

from app.agent.insights_catalog import (
    build_schemas, execute, all_names, entry,
)


# The shape `routes/agent.py` and `tests/audit_insights_agent.py`
# already import.
INSIGHTS_TOOL_SCHEMAS = build_schemas()


def execute_insights_tool(name, args, company_id, user_id):
    return execute(name, args, company_id, user_id)


# ─── Back-compat aliases ────────────────────────────────────────
# Existing audit checks (`tests/audit_insights_agent.py:5,6,7`)
# import these underscored names directly. Keep them as thin
# forwarders so the audit's positional-arg call signature keeps
# working; the actual body lives in `insights_batches.core_reads`.

def _todays_summary(args, company_id, user_id):
    from app.agent.insights_batches.core_reads import todays_summary
    return todays_summary(args, company_id, user_id)


def _tasks_stats(args, company_id, user_id):
    from app.agent.insights_batches.core_reads import tasks_stats
    return tasks_stats(args, company_id, user_id)


def _employees_performance(args, company_id, user_id):
    from app.agent.insights_batches.core_reads import (
        employees_performance,
    )
    return employees_performance(args, company_id, user_id)


def _overdue_items(args, company_id, user_id):
    from app.agent.insights_batches.core_reads import overdue_items
    return overdue_items(args, company_id, user_id)


def _module_activity(args, company_id, user_id):
    from app.agent.insights_batches.core_reads import module_activity
    return module_activity(args, company_id, user_id)


# ─── Introspection helpers (used by the audit) ─────────────────
def registered_tool_names():
    return all_names()


def registered_tool(name):
    return entry(name)
