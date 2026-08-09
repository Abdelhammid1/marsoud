"""MARSOUD-SUPERADMIN-CONTROL-01 T7 (2026-08-08) — AI Control Center.

Composers + PlatformSetting setters for /admin/ai-control. Every
write logs a platform_audit line so a hot config change is
traceable back to the actor.

Reads:
  providers_status()   env-var presence (never leaks the value)
  model_routing()      current + default per-persona provider/model
  fallback_order()     ordered list, empty = single-provider mode
  global_caps()        max_tokens + globally_disabled flag
  turn_log(...)        filtered AiTokenUsage rows

Writes (all log_platform_action):
  set_model_routing(persona, provider, model, actor_id)
  set_fallback_order(order_list, actor_id)
  set_max_tokens(n, actor_id)               # 256..32_000
  set_globally_disabled(flag, actor_id)
  set_insights_provider(provider, actor_id)  # optional
"""
import json
import os
from datetime import datetime, timedelta

from app import db
from app.models import AiTokenUsage
from app.services.subscription import _get_setting_raw, _set_setting_raw


# ─── PlatformSetting keys T7 owns ─────────────────────────────
KEY_FALLBACK_ORDER    = "ai_provider_fallback_order"
KEY_MAX_TOKENS        = "ai_max_tokens_per_turn"
KEY_GLOBALLY_DISABLED = "ai_globally_disabled"
KEY_INSIGHTS_PROVIDER = "insights_provider"

# Existing keys T7 surfaces / mutates too:
KEY_ACC_PROVIDER = "accountant_provider"
KEY_ACC_MODEL    = "accountant_model"
KEY_INS_MODEL    = "insights_model"

KNOWN_PROVIDERS = ("anthropic", "deepseek")
PERSONAS = ("accountant", "insights")


# ─── 1. Providers status (read-only env presence) ─────────────
def providers_status():
    """List of dicts describing each known AI env var. Value is
    NEVER surfaced — only whether it's set."""
    # Anthropic key lives in Flask config (config.py:30 reads .env
    # via os.environ then Flask config exposes it).
    from flask import current_app
    try:
        anthropic_present = bool(current_app.config.get("ANTHROPIC_API_KEY"))
    except Exception:
        anthropic_present = bool(os.environ.get("ANTHROPIC_API_KEY"))

    return [
        {
            "env_var": "ANTHROPIC_API_KEY",
            "present": anthropic_present,
            "description_ar": "مفتاح Anthropic (Claude).",
            "used_by": ["accountant (anthropic)"],
        },
        {
            "env_var": "DEEPSEEK_API_KEY",
            "present": bool(os.environ.get("DEEPSEEK_API_KEY")),
            "description_ar": "مفتاح DeepSeek للـ insights + accountant الافتراضي.",
            "used_by": ["insights", "accountant (deepseek fallback)"],
        },
        {
            "env_var": "DEEPSEEK_API_KEY_ACCOUNTANT",
            "present": bool(os.environ.get("DEEPSEEK_API_KEY_ACCOUNTANT")),
            "description_ar": "مفتاح DeepSeek منفصل لـ accountant (تتبّع فوترة).",
            "used_by": ["accountant (deepseek)"],
        },
        {
            "env_var": "ANTHROPIC_MODEL",
            "present": bool(os.environ.get("ANTHROPIC_MODEL")),
            "description_ar": "موديل افتراضي لو accountant_model فارغ.",
            "used_by": ["accountant (fallback default)"],
        },
    ]


# ─── 2. Model routing ─────────────────────────────────────────
def model_routing():
    """Current + default values per persona. Defaults mirror the
    hardcoded map at app/agent/base.py:186-193."""
    return {
        "accountant_provider": (_get_setting_raw(KEY_ACC_PROVIDER) or "anthropic"),
        "accountant_model":    (_get_setting_raw(KEY_ACC_MODEL) or ""),
        "accountant_default_by_provider": {
            "anthropic": "claude-sonnet-4-5",
            "deepseek":  "deepseek-reasoner",
        },
        "insights_provider":   (_get_setting_raw(KEY_INSIGHTS_PROVIDER) or "deepseek"),
        "insights_model":      (_get_setting_raw(KEY_INS_MODEL) or ""),
        "insights_default_by_provider": {
            "anthropic": "claude-sonnet-4-5",
            "deepseek":  "deepseek-v4-flash",
        },
    }


# ─── 3. Fallback order ────────────────────────────────────────
def fallback_order():
    """Parsed ordered list. Empty when unset or malformed."""
    raw = _get_setting_raw(KEY_FALLBACK_ORDER) or ""
    return _parse_order(raw)


def _parse_order(raw):
    if not raw:
        return []
    try:
        if raw.startswith("["):
            parsed = json.loads(raw)
        else:
            parsed = raw.split(",")
    except Exception:
        return []
    out = []
    for n in parsed:
        n = (n or "").strip().lower()
        if n in KNOWN_PROVIDERS and n not in out:
            out.append(n)
    return out


# ─── 4. Global caps ───────────────────────────────────────────
def global_caps():
    raw_mt = _get_setting_raw(KEY_MAX_TOKENS)
    try:
        mt = int(raw_mt) if raw_mt else 4096
    except (TypeError, ValueError):
        mt = 4096
    raw_kill = (_get_setting_raw(KEY_GLOBALLY_DISABLED) or "").lower()
    return {
        "max_tokens_per_turn": mt,
        "globally_disabled":   raw_kill in ("1", "true", "yes", "on"),
    }


# ─── 5. Turn log ──────────────────────────────────────────────
def turn_log(*, company_id=None, user_id=None, provider=None,
              model=None, hours=24, limit=100):
    """Latest AiTokenUsage rows newest-first, filtered."""
    cutoff = datetime.utcnow() - timedelta(hours=max(1, int(hours)))
    q = AiTokenUsage.query.filter(AiTokenUsage.created_at >= cutoff)
    if company_id:
        q = q.filter(AiTokenUsage.company_id == int(company_id))
    if user_id:
        q = q.filter(AiTokenUsage.user_id == int(user_id))
    if provider:
        q = q.filter(AiTokenUsage.provider == provider)
    if model:
        q = q.filter(AiTokenUsage.model == model)
    return (q.order_by(AiTokenUsage.created_at.desc())
             .limit(int(limit)).all())


# ─── Setters ───────────────────────────────────────────────────
def _audit(action, actor_id, details):
    try:
        from app.services.superadmin import log_platform_action
        log_platform_action(action, actor_id=actor_id, details=details)
    except Exception:
        # Audit failure must never break the setter.
        pass


def set_model_routing(*, persona, provider, model, actor_id):
    persona = (persona or "").lower()
    if persona not in PERSONAS:
        raise ValueError(f"unknown persona: {persona!r}")
    p = (provider or "").lower().strip()
    if p not in KNOWN_PROVIDERS:
        raise ValueError(
            f"unknown provider: {provider!r} "
            f"(known: {', '.join(KNOWN_PROVIDERS)})")
    model = (model or "").strip()
    if persona == "accountant":
        _set_setting_raw(KEY_ACC_PROVIDER, p)
        _set_setting_raw(KEY_ACC_MODEL, model)
    else:
        _set_setting_raw(KEY_INSIGHTS_PROVIDER, p)
        _set_setting_raw(KEY_INS_MODEL, model)
    db.session.commit()
    _audit("ai_control_model_routing", actor_id,
           f"persona={persona} provider={p} model={model or '(default)'}")


def set_fallback_order(order_list, actor_id):
    if not isinstance(order_list, (list, tuple)):
        raise ValueError("order_list must be a list")
    cleaned = []
    for name in order_list:
        n = (name or "").strip().lower()
        if not n:
            continue
        if n not in KNOWN_PROVIDERS:
            raise ValueError(
                f"unknown provider in fallback list: {name!r}")
        if n not in cleaned:
            cleaned.append(n)
    _set_setting_raw(KEY_FALLBACK_ORDER, json.dumps(cleaned))
    db.session.commit()
    _audit("ai_control_fallback", actor_id,
           f"order={cleaned}")


def set_max_tokens(n, actor_id):
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise ValueError("max_tokens must be integer")
    if not (256 <= n <= 32_000):
        raise ValueError(
            "max_tokens must be between 256 and 32000")
    _set_setting_raw(KEY_MAX_TOKENS, str(n))
    db.session.commit()
    _audit("ai_control_max_tokens", actor_id, f"n={n}")


def set_globally_disabled(flag, actor_id):
    flag = bool(flag)
    _set_setting_raw(KEY_GLOBALLY_DISABLED, "true" if flag else "false")
    db.session.commit()
    _audit("ai_control_kill_switch", actor_id,
           f"disabled={flag}")


def set_insights_provider(provider, actor_id):
    p = (provider or "").lower().strip()
    if p and p not in KNOWN_PROVIDERS:
        raise ValueError(f"unknown provider: {provider!r}")
    _set_setting_raw(KEY_INSIGHTS_PROVIDER, p)
    db.session.commit()
    _audit("ai_control_insights_provider", actor_id,
           f"provider={p or '(default)'}")
