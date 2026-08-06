"""MARSOUD-INSIGHTS-AGENT-01 (Batch 9 Ticket 6, 2026-08-01) —
Provider-agnostic agent loop.

Both the accountant (Anthropic) and the insights agent
(DeepSeek) go through `run_agent_turn()` with different
personas, tools, and providers. The loop is unchanged from the
old accountant.py — same iterations, same tool_use handling,
same quota-check-before-call, same usage-log-after-call.

Design notes:
- Persona goes FIRST in the system prompt so DeepSeek's
  automatic prompt-cache lands on the stable prefix.
- Company context appends below the persona so it varies
  per-tenant without breaking the cache.
- `execute_tool_fn` is injected so the accountant + insights
  can dispatch to different tool implementations without a
  global registry.
"""
from __future__ import annotations
import json
from flask import current_app
from app import db


def run_agent_turn(*, messages, company_id, user_id, persona,
                   provider, tools, execute_tool_fn,
                   company_context=None, max_iters=8):
    """Run one user turn end-to-end.

    Args:
        messages: list of role/content dicts (agent-native
            shape, Anthropic-style even for DeepSeek — the
            DeepseekProvider translates internally).
        company_id: int, tenant id (also passed to tools).
        user_id: int, the caller (for quota + audit).
        persona: dict {system_prompt: str, model: str,
            key: str}. `key` is stored as `AiTokenUsage.model`
            hint + optional log tag.
        provider: an AiProvider instance.
        tools: list of tool schemas in Anthropic shape.
        execute_tool_fn: callable(name, args, company_id,
            user_id) → JSON-serializable result.
        company_context: extra system-prompt text appended
            below the persona (per-tenant, cache-unfriendly).
        max_iters: safety cap on tool-use turns.

    Returns:
        (final_text, updated_messages, tool_trace)
    """
    system = persona["system_prompt"]
    if company_context:
        system += f"\n\nسياق الشركة الحالية:\n{company_context}"
    model = persona["model"]

    tool_trace = []
    final_text = ""

    for _ in range(max_iters):
        # Quota pre-flight (kept identical to old accountant.py
        # so existing tests + prod behaviour don't drift).
        try:
            from app.services.quotas import (
                check_quota, QUOTA_AI_TOKENS_MONTH,
                QuotaBlockedError,
            )
            from app.models import Company
            _co = (db.session.get(Company, company_id)
                    if company_id else None)
            if _co:
                check_quota(_co, QUOTA_AI_TOKENS_MONTH,
                             incoming=1, user_id=user_id)
        except QuotaBlockedError:
            raise
        except Exception:
            pass

        result, usage = provider.run_turn(
            system=system, messages=messages, tools=tools,
            model=model,
        )

        # Log actual token usage — provider_key + model land in
        # AiTokenUsage so the super-admin dashboard can split
        # accountant (anthropic) vs insights (deepseek).
        try:
            from app.services.quotas import log_ai_usage
            log_ai_usage(
                company_id=company_id, user_id=user_id,
                provider=provider.provider_key, model=model,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            )
        except Exception:
            pass

        # Persist the assistant's blocks back into `messages` in
        # the Anthropic-native shape so subsequent iterations —
        # and the DeepseekProvider's Anthropic-→-OpenAI shim —
        # both work off the same source of truth.
        messages.append({
            "role": "assistant",
            "content": result["assistant_blocks"],
        })
        final_text += result["text"]

        if (result["stop_reason"] != "tool_use"
                or not result["tool_uses"]):
            break

        # Execute each tool and feed results back.
        tool_results = []
        for tu in result["tool_uses"]:
            try:
                data = execute_tool_fn(
                    tu["name"], tu["input"],
                    company_id, user_id)
            except Exception as e:  # noqa: BLE001
                data = {"error": str(e)[:400]}
            tool_trace.append({"tool": tu["name"],
                                "input": tu["input"],
                                "result": data})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": json.dumps(data, default=str,
                                       ensure_ascii=False),
            })
        messages.append({"role": "user",
                         "content": tool_results})
        # Reset — the final assistant text should come from the
        # last iteration only.
        final_text = ""

    return final_text, messages, tool_trace


# ─── Persona registry ──────────────────────────────────────────
# MARSOUD-AGENT-DEEPSEEK-02 (2026-08-06) — the accountant used to be
# hardwired to AnthropicProvider + ANTHROPIC_MODEL from Flask config.
# A super-admin can now flip provider + model via PlatformSetting
# without a code change or a restart. Same shape the insights agent
# already uses (accountant reuses insights' knob mechanism, not the
# other way around — accountant is the higher-stakes agent so we
# want the RUNTIME rollback path available first).

_ACCOUNTANT_DEFAULT_MODEL_BY_PROVIDER = {
    "anthropic": "claude-sonnet-4-5",
    # DeepSeek's strongest reasoning model — Zyad flagged by name in
    # T2's ticket text: "أقوى موديل reasoning عند DeepSeek، مش موديل
    # الـ flash المستخدم في التحليلات". The setting-page lets the
    # operator type a different name if DeepSeek renames it.
    "deepseek": "deepseek-reasoner",
}


def get_accountant_provider_and_model():
    """Return (provider_key, model) from PlatformSetting.

    Defaults: provider = "anthropic" (no behaviour change on deploy;
    a fresh install must NOT flip to DeepSeek by accident).
    model = ANTHROPIC_MODEL from Flask config when provider is
    anthropic, else the per-provider default above.
    """
    from app.models.platform_setting import PlatformSetting

    prov_row = PlatformSetting.query.filter_by(
        key="accountant_provider").first()
    provider = (prov_row.value if prov_row else None) or "anthropic"
    if provider not in _ACCOUNTANT_DEFAULT_MODEL_BY_PROVIDER:
        # Anything the settings page could not have written — a data
        # fix, a stale value — falls back to anthropic. Safer than
        # picking whichever the string alphabetises to first.
        provider = "anthropic"

    model_row = PlatformSetting.query.filter_by(
        key="accountant_model").first()
    model = (model_row.value if model_row else None)
    if not model:
        if provider == "anthropic":
            model = current_app.config.get(
                "ANTHROPIC_MODEL",
                _ACCOUNTANT_DEFAULT_MODEL_BY_PROVIDER["anthropic"])
        else:
            model = _ACCOUNTANT_DEFAULT_MODEL_BY_PROVIDER[provider]
    return provider, model


def accountant_persona():
    """The 17-tool accountant persona. Model resolved via
    get_accountant_provider_and_model() so a super-admin flip lands
    on the NEXT turn without a restart."""
    from app.agent.prompts import SYSTEM_PROMPT
    _prov, model = get_accountant_provider_and_model()
    return {
        "key": "accountant",
        "system_prompt": SYSTEM_PROMPT,
        "model": model,
    }


def insights_persona():
    """The read-only insights persona (DeepSeek)."""
    from app.agent.insights_prompt import INSIGHTS_SYSTEM_PROMPT
    from app.models.platform_setting import PlatformSetting
    # Model override lives in PlatformSetting so a super-admin
    # can flip it without redeploying. Falls back to the
    # ticket's default.
    row = PlatformSetting.query.filter_by(
        key="insights_model").first() if PlatformSetting else None
    model = ((row.value if row else None)
             or "deepseek-v4-flash")
    return {
        "key": "insights",
        "system_prompt": INSIGHTS_SYSTEM_PROMPT,
        "model": model,
    }
