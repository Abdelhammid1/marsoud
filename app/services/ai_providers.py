"""MARSOUD-INSIGHTS-AGENT-01 (Batch 9 Ticket 6, 2026-08-01) —
Provider abstraction so more than one agent can live in the app
without hard-wiring an Anthropic client into every call site.

Two concrete providers today:
  · AnthropicProvider — wraps `anthropic.Anthropic()`, preserves
    every quirk of the accountant loop unchanged.
  · DeepseekProvider — wraps `openai.OpenAI(base_url="https://
    api.deepseek.com")` since DeepSeek is OpenAI-SDK-compatible.
    Powers the insights agent on `deepseek-v4-flash`.

Both `run_turn()` methods return the SAME normalized shape so
the calling loop (`app/agent/base.py:run_agent_turn`) is
provider-agnostic:

    result = {
        "text": str,            # concatenated assistant text
        "tool_uses": [
            {"id": str, "name": str, "input": dict}, ...
        ],
        "stop_reason": str,     # "tool_use" | "end_turn" | ...
    }
    usage = {"input_tokens": int, "output_tokens": int}

Providers must NEVER touch db.session — the caller handles
persistence + quota logging.
"""
from __future__ import annotations
import json
import os


# ─── Base ───────────────────────────────────────────────────────
class AiProvider:
    """Abstract base — every subclass must implement run_turn()."""

    #: A short provider label recorded in AiTokenUsage.provider
    #: for cost analytics.
    provider_key: str = ""

    def run_turn(self, *, system, messages, tools, model,
                 max_tokens=4096):
        raise NotImplementedError


# ─── Anthropic ──────────────────────────────────────────────────
class AnthropicProvider(AiProvider):
    """Wraps the existing accountant call path. Reads
    ANTHROPIC_API_KEY from Flask config (which loads .env). Any
    change here MUST preserve the accountant's observable
    behaviour — see audit_accountant_regression.
    """

    provider_key = "anthropic"

    def __init__(self, api_key: str | None = None):
        from anthropic import Anthropic
        if not api_key:
            from flask import current_app
            api_key = current_app.config.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY غير مضبوط في .env")
        self._client = Anthropic(api_key=api_key)

    def run_turn(self, *, system, messages, tools, model,
                 max_tokens=4096):
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=messages,
        )
        text_parts = []
        tool_uses = []
        assistant_blocks = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
                assistant_blocks.append(
                    {"type": "text", "text": block.text})
            elif block.type == "tool_use":
                tool_uses.append({
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
                assistant_blocks.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
        usage_obj = getattr(resp, "usage", None)
        usage = {
            "input_tokens": getattr(usage_obj, "input_tokens", 0),
            "output_tokens": getattr(usage_obj, "output_tokens", 0),
        }
        return {
            "text": "".join(text_parts),
            "tool_uses": tool_uses,
            "stop_reason": getattr(resp, "stop_reason", None),
            "assistant_blocks": assistant_blocks,
        }, usage


# ─── DeepSeek (OpenAI-compatible endpoint) ──────────────────────
class DeepseekProvider(AiProvider):
    """DeepSeek serves an OpenAI-compatible REST surface, so we
    reuse the `openai` SDK with a base_url override. Tool schemas
    are converted from Anthropic's shape to OpenAI's on the way
    in; the response is normalized back to the shared shape on
    the way out.
    """

    provider_key = "deepseek"

    def __init__(self, api_key: str | None = None,
                 base_url: str = "https://api.deepseek.com"):
        from openai import OpenAI
        key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY غير مضبوط في .env")
        self._client = OpenAI(api_key=key, base_url=base_url)

    def _anthropic_to_openai_tools(self, tools):
        """Anthropic tool schema → OpenAI 'function' shape."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema",
                                          {"type": "object",
                                           "properties": {}}),
                },
            }
            for t in tools
        ]

    def _anthropic_to_openai_messages(self, system, messages):
        """Flatten the accountant's assistant/tool_result content
        blocks into OpenAI's role-based message list."""
        out = [{"role": "system", "content": system}]
        for m in messages:
            role = m["role"]
            content = m["content"]
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            if role == "assistant":
                # Assistant blocks might contain text + tool_use.
                text_parts = []
                tool_calls = []
                for b in content:
                    if b.get("type") == "text":
                        text_parts.append(b.get("text", ""))
                    elif b.get("type") == "tool_use":
                        tool_calls.append({
                            "id": b["id"],
                            "type": "function",
                            "function": {
                                "name": b["name"],
                                "arguments": json.dumps(
                                    b.get("input") or {},
                                    ensure_ascii=False),
                            },
                        })
                msg = {"role": "assistant",
                       "content": "".join(text_parts) or None}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                out.append(msg)
            elif role == "user":
                # User blocks might contain tool_result blocks.
                tool_msgs = []
                text_parts = []
                for b in content:
                    if b.get("type") == "tool_result":
                        tool_msgs.append({
                            "role": "tool",
                            "tool_call_id": b["tool_use_id"],
                            "content": b.get("content", ""),
                        })
                    elif b.get("type") == "text":
                        text_parts.append(b.get("text", ""))
                if text_parts:
                    out.append({"role": "user",
                                "content": "".join(text_parts)})
                out.extend(tool_msgs)
        return out

    def run_turn(self, *, system, messages, tools, model,
                 max_tokens=4096):
        openai_tools = self._anthropic_to_openai_tools(tools)
        openai_msgs = self._anthropic_to_openai_messages(
            system, messages)
        resp = self._client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=openai_msgs,
            tools=openai_tools or None,
        )
        choice = resp.choices[0]
        msg = choice.message
        text = msg.content or ""
        tool_uses = []
        assistant_blocks = []
        if text:
            assistant_blocks.append({"type": "text",
                                     "text": text})
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            tool_uses.append({
                "id": tc.id,
                "name": tc.function.name,
                "input": args,
            })
            assistant_blocks.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.function.name,
                "input": args,
            })
        # OpenAI reports finish_reason: "tool_calls" | "stop" | ...
        finish = choice.finish_reason
        stop_reason = ("tool_use" if finish == "tool_calls"
                       else "end_turn")
        usage_obj = getattr(resp, "usage", None)
        usage = {
            "input_tokens": getattr(usage_obj,
                                     "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage_obj,
                                      "completion_tokens", 0) or 0,
        }
        return {
            "text": text,
            "tool_uses": tool_uses,
            "stop_reason": stop_reason,
            "assistant_blocks": assistant_blocks,
        }, usage


def get_provider(key: str) -> AiProvider:
    """Factory. Extend when we add a 3rd provider."""
    key = (key or "").lower()
    if key == "anthropic":
        return AnthropicProvider()
    if key == "deepseek":
        return DeepseekProvider()
    raise ValueError(f"unknown AI provider: {key!r}")
