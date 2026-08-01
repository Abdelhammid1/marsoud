"""The accountant agent — thin wrapper over the shared
`run_agent_turn` loop. Behaviour preserved 100% from the pre-
Batch-9 implementation: Anthropic client, same system prompt,
same 17 tools, same max_iters=8, same token-quota flow.

MARSOUD-INSIGHTS-AGENT-01 (Batch 9 Ticket 6, 2026-08-01) — the
loop moved to `app/agent/base.py` so the insights agent can
reuse it without copy-pasting. Signature of `run_agent()` is
untouched so `app/routes/agent.py` needs no changes.
"""
from app.agent.base import run_agent_turn, accountant_persona
from app.agent.tools import TOOL_SCHEMAS, execute_tool
from app.services.ai_providers import AnthropicProvider


def run_agent(messages, company_id, user_id,
              company_context=None, max_iters=8):
    """Public entry point — kept API-identical to pre-Batch-9."""
    return run_agent_turn(
        messages=messages,
        company_id=company_id,
        user_id=user_id,
        persona=accountant_persona(),
        provider=AnthropicProvider(),
        tools=TOOL_SCHEMAS,
        execute_tool_fn=execute_tool,
        company_context=company_context,
        max_iters=max_iters,
    )
