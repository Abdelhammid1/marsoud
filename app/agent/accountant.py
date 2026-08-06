"""The accountant agent — thin wrapper over the shared
`run_agent_turn` loop.

MARSOUD-INSIGHTS-AGENT-01 (Batch 9 Ticket 6, 2026-08-01) — the
loop moved to `app/agent/base.py` so the insights agent can
reuse it without copy-pasting.

MARSOUD-AGENT-DEEPSEEK-02 (2026-08-06) — the AnthropicProvider
was hardcoded here. Provider is now resolved per-turn from
PlatformSetting (via get_accountant_provider_and_model), so a
super-admin can flip anthropic ↔ deepseek from the admin UI and
the NEXT chat turn lands on the new provider — no restart, no
deploy. Every accountant chat instantiates the provider once per
turn, so mid-conversation flips still work.
"""
from app.agent.base import (
    run_agent_turn, accountant_persona,
    get_accountant_provider_and_model,
)
import os
from app.agent.tools import TOOL_SCHEMAS, execute_tool
from app.services.ai_providers import AnthropicProvider, DeepseekProvider


def _provider_for(key):
    """Instantiate the provider matching a PlatformSetting value.
    DeepseekProvider raises RuntimeError inside __init__ when
    DEEPSEEK_API_KEY is not set — the caller (chat route) surfaces
    that as a specific Arabic flash message.
    MARSOUD-AGENT-DEEPSEEK-KEY-SPLIT (2026-08-06) — the accountant
    agent uses its own DeepSeek key (DEEPSEEK_API_KEY_ACCOUNTANT),
    separate from the insights agent's DEEPSEEK_API_KEY, so usage
    and billing can be tracked and capped independently."""
    if key == "deepseek":
        accountant_key = os.environ.get("DEEPSEEK_API_KEY_ACCOUNTANT")
        return DeepseekProvider(api_key=accountant_key)
    return AnthropicProvider()


def run_agent(messages, company_id, user_id,
              company_context=None, max_iters=8):
    """Public entry point — kept API-identical to pre-Batch-9."""
    provider_key, _ = get_accountant_provider_and_model()
    return run_agent_turn(
        messages=messages,
        company_id=company_id,
        user_id=user_id,
        persona=accountant_persona(),
        provider=_provider_for(provider_key),
        tools=TOOL_SCHEMAS,
        execute_tool_fn=execute_tool,
        company_context=company_context,
        max_iters=max_iters,
    )
