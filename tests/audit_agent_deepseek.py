#!/usr/bin/env python3
"""MARSOUD-AGENT-DEEPSEEK-02 (2026-08-06) — audit for the runtime
provider swap.

Before this ticket, the accountant agent's provider (Anthropic) was
imported at the top of accountant.py and its model came from Flask
config — flipping either needed a code change AND a redeploy. Now a
super-admin flips both from a settings page and the NEXT chat turn
lands on the new provider.

Checks
   1. default is anthropic (no PlatformSetting rows)
   2. deepseek setting drives the persona + returns "deepseek-reasoner"
   3. custom accountant_model wins over the default
   4. route picks the right provider class from the setting
   5. instant switch — no restart, single app_context
   6. missing DEEPSEEK_API_KEY surfaces cleanly
   7. usage log splits by provider
   8. admin route saves both settings
   9. admin route emits audit-log entry
  10. admin route rejects invalid provider
  11. sidebar link renders on other admin pages
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__AGDS_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import User
    from app.services.legal import get_terms_version
    # Super-admin user for the admin-route tests. The check itself
    # doesn't need company scoping since PlatformSetting is global.
    u = User(email=f"{PREFIX}super@audit.local",
             full_name="super", is_active=True,
             is_superadmin=True,
             terms_version=get_terms_version(),
             terms_accepted_at=datetime.utcnow())
    u.set_password("Passw0rd!audit1")
    db.session.add(u); db.session.flush()
    db.session.commit()
    _STATE["superadmin_uid"] = u.id


def _teardown():
    from app.models import User, PlatformSetting
    from sqlalchemy import text
    db.session.rollback()
    # Wipe every setting key this suite touches so a leftover doesn't
    # carry between checks or between suite runs.
    for k in ("accountant_provider", "accountant_model"):
        PlatformSetting.query.filter_by(key=k).delete()
    db.session.commit()
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"),
                           {"u": u.id})
    db.session.commit()


def _wipe_settings():
    from app.models import PlatformSetting
    for k in ("accountant_provider", "accountant_model"):
        PlatformSetting.query.filter_by(key=k).delete()
    db.session.commit()


def _set(key, value):
    from app.models import PlatformSetting
    row = PlatformSetting.query.filter_by(key=key).first()
    if row is None:
        row = PlatformSetting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. default provider is anthropic (no PlatformSetting rows)")
def _():
    from app.agent.base import get_accountant_provider_and_model
    _wipe_settings()
    prov, model = get_accountant_provider_and_model()
    assert prov == "anthropic", (
        f"default provider={prov!r}, expected 'anthropic' — a fresh "
        "deploy MUST NOT flip to DeepSeek automatically")
    assert model, "default model is empty"
    return f"({prov}, {model})"


@check("2. deepseek setting drives persona; default model is deepseek-reasoner")
def _():
    from app.agent.base import (
        get_accountant_provider_and_model, accountant_persona,
    )
    _wipe_settings()
    _set("accountant_provider", "deepseek")
    prov, model = get_accountant_provider_and_model()
    assert prov == "deepseek", f"prov={prov!r}"
    assert model == "deepseek-reasoner", (
        f"deepseek default model = {model!r}, expected 'deepseek-reasoner' "
        "— Zyad's callout: أقوى موديل reasoning عند DeepSeek")
    persona = accountant_persona()
    assert persona["model"] == "deepseek-reasoner"
    return f"model={persona['model']!r}"


@check("3. custom accountant_model wins over the default")
def _():
    from app.agent.base import get_accountant_provider_and_model
    _wipe_settings()
    _set("accountant_provider", "deepseek")
    _set("accountant_model", "deepseek-v4-flash")
    prov, model = get_accountant_provider_and_model()
    assert model == "deepseek-v4-flash", (
        f"custom model ignored; got {model!r}")
    return f"custom model {model!r} used"


@check("4. accountant.run_agent picks provider class from the setting")
def _():
    """Mock both provider __init__ to record which was called;
    stub run_agent_turn so we don't actually hit any API."""
    from unittest.mock import patch, MagicMock
    _wipe_settings()

    # Patch the accountant module's binding, not base's — the
    # accountant imported the function at module load, so
    # 'app.agent.accountant.get_accountant_provider_and_model' is
    # where the call resolves from.
    with patch("app.agent.accountant.get_accountant_provider_and_model",
               return_value=("deepseek", "deepseek-reasoner")):
        with patch("app.agent.accountant.DeepseekProvider") as mds, \
             patch("app.agent.accountant.AnthropicProvider") as manth, \
             patch("app.agent.accountant.run_agent_turn",
                   return_value=("", [], [])):
            from app.agent.accountant import run_agent
            run_agent([{"role": "user", "content": "hi"}], 1, 1)
    assert mds.called, "DeepseekProvider was not instantiated"
    assert not manth.called, (
        "AnthropicProvider was instantiated when setting was deepseek")

    with patch("app.agent.accountant.get_accountant_provider_and_model",
               return_value=("anthropic", "claude-x")):
        with patch("app.agent.accountant.DeepseekProvider") as mds, \
             patch("app.agent.accountant.AnthropicProvider") as manth, \
             patch("app.agent.accountant.run_agent_turn",
                   return_value=("", [], [])):
            from app.agent.accountant import run_agent
            run_agent([{"role": "user", "content": "hi"}], 1, 1)
    assert manth.called, "AnthropicProvider was not instantiated"
    assert not mds.called, (
        "DeepseekProvider was instantiated when setting was anthropic")
    return "each provider picked in its branch"


@check("5. flip is instant — no restart between calls")
def _():
    from unittest.mock import patch
    from app.agent.base import get_accountant_provider_and_model
    _wipe_settings()

    # No setting → anthropic
    prov, _ = get_accountant_provider_and_model()
    assert prov == "anthropic"

    # Write deepseek → next read is deepseek
    _set("accountant_provider", "deepseek")
    prov, _ = get_accountant_provider_and_model()
    assert prov == "deepseek", (
        f"flip did not land: prov={prov!r}")

    # Flip back
    _set("accountant_provider", "anthropic")
    prov, _ = get_accountant_provider_and_model()
    assert prov == "anthropic", (
        f"flip back did not land: prov={prov!r}")
    return "3 flips, no restart"


@check("6. missing DEEPSEEK_API_KEY surfaces as RuntimeError")
def _():
    """DeepseekProvider.__init__ raises when the env var is empty.
    The accountant route lets it propagate to a flash — the audit
    just pins the raise happens, since going through the HTTP layer
    would need an active company + login fixture that's excessive
    for a provider-init check."""
    from app.agent.accountant import _provider_for
    saved = os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        try:
            _provider_for("deepseek")
        except RuntimeError as e:
            msg = str(e)
            assert "DEEPSEEK_API_KEY" in msg, (
                f"error message doesn't name the env var: {msg!r}")
        else:
            raise AssertionError(
                "DeepseekProvider silently succeeded without DEEPSEEK_API_KEY")
    finally:
        if saved is not None:
            os.environ["DEEPSEEK_API_KEY"] = saved
    return "RuntimeError names DEEPSEEK_API_KEY"


@check("7. usage log splits by provider (anthropic + deepseek rows)")
def _():
    """log_ai_usage already writes AiTokenUsage.provider — the ticket
    calls this out as our measurement path for savings. Pin the shape:
    a call with each provider produces one row per provider."""
    from app.models import AiTokenUsage
    from app.services.quotas import log_ai_usage
    log_ai_usage(company_id=1, user_id=1,
                 provider="anthropic", model="claude-x",
                 input_tokens=10, output_tokens=20)
    log_ai_usage(company_id=1, user_id=1,
                 provider="deepseek", model="deepseek-reasoner",
                 input_tokens=15, output_tokens=25)
    providers_seen = {r.provider for r
                      in AiTokenUsage.query.filter(
                          AiTokenUsage.provider.in_(
                              ["anthropic", "deepseek"]))
                      .order_by(AiTokenUsage.id.desc()).limit(2).all()}
    assert providers_seen >= {"anthropic", "deepseek"}, (
        f"providers seen: {providers_seen!r}")
    return "both providers recorded"


@check("8. admin POST saves both settings")
def _():
    from app.models import PlatformSetting
    _wipe_settings()
    with _STATE["app"].app_context():
        c = _STATE["app"].test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(_STATE["superadmin_uid"])
            s["_fresh"] = True
        r = c.post("/admin/ai-settings", data={
            "accountant_provider": "deepseek",
            "accountant_model": "deepseek-reasoner",
        }, follow_redirects=False)
    assert r.status_code in (302, 303), (
        f"admin POST returned {r.status_code}")
    prov_row = PlatformSetting.query.filter_by(
        key="accountant_provider").first()
    model_row = PlatformSetting.query.filter_by(
        key="accountant_model").first()
    assert prov_row and prov_row.value == "deepseek", (
        f"provider not saved: {prov_row!r}")
    assert model_row and model_row.value == "deepseek-reasoner", (
        f"model not saved: {model_row!r}")
    return "both keys present in DB"


@check("9. admin POST emits ai_settings_update audit log")
def _():
    from app.models import PlatformAuditLog
    _wipe_settings()
    PlatformAuditLog.query.filter_by(action="ai_settings_update").delete()
    db.session.commit()
    with _STATE["app"].app_context():
        c = _STATE["app"].test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(_STATE["superadmin_uid"])
            s["_fresh"] = True
        c.post("/admin/ai-settings", data={
            "accountant_provider": "anthropic",
            "accountant_model": "",
        }, follow_redirects=False)
    n = PlatformAuditLog.query.filter_by(
        action="ai_settings_update").count()
    assert n == 1, f"expected 1 audit row, got {n}"
    return "1 audit-log row written"


@check("10. admin POST rejects invalid provider — no write, flash error")
def _():
    from app.models import PlatformSetting
    _wipe_settings()
    _set("accountant_provider", "anthropic")   # baseline
    with _STATE["app"].app_context():
        c = _STATE["app"].test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(_STATE["superadmin_uid"])
            s["_fresh"] = True
        r = c.post("/admin/ai-settings", data={
            "accountant_provider": "openai",  # not in the two-item allowlist
            "accountant_model": "gpt-x",
        }, follow_redirects=False)
    assert r.status_code in (302, 303), (
        f"invalid POST returned {r.status_code}, expected redirect")
    prov_row = PlatformSetting.query.filter_by(
        key="accountant_provider").first()
    assert prov_row.value == "anthropic", (
        f"invalid provider was written; row={prov_row.value!r}")
    return "invalid provider refused"


@check("11. sidebar link to ai_settings renders on other admin pages")
def _():
    with _STATE["app"].app_context():
        c = _STATE["app"].test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(_STATE["superadmin_uid"])
            s["_fresh"] = True
        r = c.get("/admin/ai-usage")
    assert r.status_code == 200, f"ai-usage status={r.status_code}"
    body = r.get_data(as_text=True)
    assert "/admin/ai-settings" in body, (
        "sidebar link to ai_settings not present on admin pages — "
        "operators cannot navigate to the new screen")
    return "sidebar link found on /admin/ai-usage"


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    _STATE["app"] = app
    passed = failed = 0
    with app.app_context():
        _setup()
        try:
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                    failed += 1
        finally:
            _teardown()
            print("\n(fixture cleaned up)")
    print(f"\n----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
