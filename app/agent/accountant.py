"""The accountant agent loop: Claude + tool use."""
import json
from flask import current_app
from anthropic import Anthropic
from app import db
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import TOOL_SCHEMAS, execute_tool


def _client():
    api_key = current_app.config.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY غير مضبوط في .env")
    return Anthropic(api_key=api_key)


def run_agent(messages, company_id, user_id, company_context=None, max_iters=8):
    """Run a conversation turn with the agent.

    messages: list of {role, content} dicts (Anthropic format)
    Returns: (final_text, updated_messages, tool_trace)
    """
    client = _client()
    model = current_app.config.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    system = SYSTEM_PROMPT
    if company_context:
        system += f"\n\nسياق الشركة الحالية:\n{company_context}"

    tool_trace = []
    iterations = 0
    final_text = ""

    while iterations < max_iters:
        iterations += 1
        # MARSOUD-PLANS-COMPLETE (Abdelhamid 2026-07-22) — check the
        # monthly AI-token quota BEFORE spending more. We can't know
        # the exact token cost of this call in advance, so we ask for
        # a "1 token" increment — enough to trip a company already
        # sitting on the ceiling. Blocking here surfaces a friendlier
        # QuotaBlockedError to the caller than a 402 from Anthropic.
        try:
            from app.services.quotas import (
                check_quota, QUOTA_AI_TOKENS_MONTH, QuotaBlockedError,
            )
            from app.models import Company
            _co = db.session.get(Company, company_id) if company_id else None
            if _co:
                check_quota(_co, QUOTA_AI_TOKENS_MONTH, incoming=1,
                             user_id=user_id)
        except QuotaBlockedError:
            raise
        except Exception:
            pass
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )
        # After the call, record actual token usage so the meter +
        # threshold notifications work correctly on the next request.
        try:
            from app.services.quotas import log_ai_usage
            usage = getattr(response, "usage", None)
            if usage is not None:
                log_ai_usage(
                    company_id=company_id, user_id=user_id,
                    provider="anthropic", model=model,
                    input_tokens=getattr(usage, "input_tokens", 0),
                    output_tokens=getattr(usage, "output_tokens", 0),
                )
        except Exception:
            pass

        # Collect assistant content
        assistant_content = []
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
                final_text += block.text
            elif block.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
                tool_uses.append(block)

        messages.append({"role": "assistant", "content": assistant_content})

        if response.stop_reason != "tool_use" or not tool_uses:
            break

        # Execute each tool and feed results back
        tool_results = []
        for tu in tool_uses:
            result = execute_tool(tu.name, tu.input, company_id, user_id)
            tool_trace.append({"tool": tu.name, "input": tu.input, "result": result})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(result, default=str, ensure_ascii=False),
            })
        messages.append({"role": "user", "content": tool_results})
        final_text = ""  # reset; final text only from last assistant turn

    return final_text, messages, tool_trace
