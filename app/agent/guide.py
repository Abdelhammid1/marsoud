"""MARSOUD-P111 — Entry point for the Marsoud System Guide Agent."""
from __future__ import annotations


def guide_persona() -> dict:
    from app.agent.guide_prompt import GUIDE_SYSTEM_PROMPT
    from app.models.platform_setting import PlatformSetting
    row = PlatformSetting.query.filter_by(key="guide_model").first() if PlatformSetting else None
    model = (row.value if row and row.value else None)
    if not model:
        ins_row = PlatformSetting.query.filter_by(key="insights_model").first() if PlatformSetting else None
        model = (ins_row.value if ins_row and ins_row.value else "deepseek-chat")
    return {
        "key": "guide",
        "system_prompt": GUIDE_SYSTEM_PROMPT,
        "model": model,
    }


def run_guide_turn(*, messages, company_id: int, user_id: int, company_context: str):
    from app.agent.base import run_agent_turn
    from app.agent.guide_tools import GUIDE_TOOL_SCHEMAS, execute_guide_tool
    from app.services.ai_providers import DeepseekProvider

    return run_agent_turn(
        messages=messages,
        company_id=company_id,
        user_id=user_id,
        persona=guide_persona(),
        provider=DeepseekProvider(),
        tools=GUIDE_TOOL_SCHEMAS,
        execute_tool_fn=execute_guide_tool,
        company_context=company_context,
        max_iters=6,
    )
