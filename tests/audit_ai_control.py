#!/usr/bin/env python3
"""MARSOUD-SUPERADMIN-CONTROL-01 T7 (2026-08-08) — AI Control Center audit.

Twelve checks covering:
  · composers (providers_status / model_routing / fallback_order
    / global_caps / turn_log)
  · setters (validation + persistence + audit)
  · provider-factory rewire (resolve_provider / kill switch /
    fallback / max-tokens clamp)
  · route smoke render
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
PREFIX = "__T7_"
_STATE = {}
# PlatformSetting keys T7 owns / touches — wiped between checks.
_T7_KEYS = (
    "ai_provider_fallback_order",
    "ai_max_tokens_per_turn",
    "ai_globally_disabled",
    "insights_provider",
    "accountant_provider",
    "accountant_model",
    "insights_model",
)


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _p(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


# ─── Fixture ───────────────────────────────────────────────────
def _setup():
    _teardown()
    from app.models import Company, Plan, User, UserStatus
    from app.models.user import user_companies
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code="__t7__").first()
    if not plan:
        plan = Plan(code="__t7__", name="T7", name_ar="T7",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "agent"])
        db.session.add(plan); db.session.flush()

    c = Company(name=f"{PREFIX}CO", base_currency="EGP",
                 subdomain="t7",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=(
                     datetime.utcnow() + timedelta(days=365)),
                 intended_plan_id=plan.id, plan_id=plan.id)
    db.session.add(c); db.session.flush()

    sa = User(
        email=f"{PREFIX}sa@x.test", full_name="super admin",
        is_active=True, is_superadmin=True,
        status=UserStatus.ACTIVE.value,
        email_verified_at=datetime.utcnow(),
        terms_version="TEST",
        password_hash=generate_password_hash(
            "x", method="pbkdf2:sha256"))
    db.session.add(sa); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=sa.id, company_id=c.id, role="owner"))
    db.session.commit()

    _STATE.update(company_id=c.id, plan_id=plan.id,
                   superadmin_id=sa.id)


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        # Wipe T7 PlatformSetting keys so previous check's writes
        # don't bleed into the next.
        for k in _T7_KEYS:
            conn.execute(text(
                "DELETE FROM platform_settings WHERE key = :k"),
                {"k": k})
        # Wipe our fixture ai_token_usage rows.
        conn.execute(text(
            "DELETE FROM ai_token_usage WHERE user_id IN "
            "(SELECT id FROM users WHERE email LIKE '__T7_%@x.test')"))
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__T7_%'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM ai_token_usage WHERE company_id = :c"),
                {"c": cid})
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    try:
                        conn.execute(text(
                            f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                            {"c": cid})
                    except Exception:
                        pass
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE '__T7_%@x.test'"))
        pids = [r[0] for r in conn.execute(text(
            "SELECT id FROM plans WHERE code = '__t7__'"))]
        for pid in pids:
            conn.execute(text(
                "DELETE FROM quotas WHERE plan_id = :p"), {"p": pid})
        conn.execute(text(
            "DELETE FROM plans WHERE code = '__t7__'"))


# ─── Checks ────────────────────────────────────────────────────
@check("1. providers_status returns 4 rows without leaking key values")
def _():
    from app.services.ai_control import providers_status
    _setup()
    rows = providers_status()
    assert len(rows) == 4, len(rows)
    env_vars = {r["env_var"] for r in rows}
    assert env_vars == {"ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
                         "DEEPSEEK_API_KEY_ACCOUNTANT", "ANTHROPIC_MODEL"}, env_vars
    for r in rows:
        assert isinstance(r["present"], bool)
        assert "used_by" in r and isinstance(r["used_by"], list)
        # The value MUST never appear in the response.
        assert "value" not in r
        val = os.environ.get(r["env_var"])
        if val:
            assert val not in str(r), \
                f"key value leaked in {r['env_var']}"


@check("2. model_routing returns keys for both personas + defaults")
def _():
    from app.services.ai_control import model_routing
    _setup()
    m = model_routing()
    for k in ("accountant_provider", "accountant_model",
              "accountant_default_by_provider",
              "insights_provider", "insights_model",
              "insights_default_by_provider"):
        assert k in m, f"missing {k}"


@check("3. set_fallback_order persists + fallback_order returns it")
def _():
    from app.services.ai_control import (
        set_fallback_order, fallback_order,
    )
    _setup()
    set_fallback_order(["deepseek", "anthropic"],
                        actor_id=_STATE["superadmin_id"])
    assert fallback_order() == ["deepseek", "anthropic"]


@check("4. set_fallback_order refuses unknown providers")
def _():
    from app.services.ai_control import set_fallback_order
    _setup()
    got = None
    try:
        set_fallback_order(["unknown"],
                            actor_id=_STATE["superadmin_id"])
    except ValueError as e:
        got = e
    assert got is not None, "expected ValueError"


@check("5. set_max_tokens persists + global_caps reflects it")
def _():
    from app.services.ai_control import set_max_tokens, global_caps
    _setup()
    set_max_tokens(1024, actor_id=_STATE["superadmin_id"])
    caps = global_caps()
    assert caps["max_tokens_per_turn"] == 1024, caps


@check("6. set_max_tokens refuses values below 256")
def _():
    from app.services.ai_control import set_max_tokens
    _setup()
    got = None
    try:
        set_max_tokens(50, actor_id=_STATE["superadmin_id"])
    except ValueError as e:
        got = e
    assert got is not None, "expected ValueError"


@check("7. set_globally_disabled -> kill_switch_active True")
def _():
    from app.services.ai_control import set_globally_disabled
    from app.services.ai_providers import kill_switch_active
    _setup()
    set_globally_disabled(True, actor_id=_STATE["superadmin_id"])
    assert kill_switch_active() is True


@check("8. resolve_provider raises when kill switch is on")
def _():
    from app.services.ai_control import set_globally_disabled
    from app.services.ai_providers import resolve_provider
    _setup()
    set_globally_disabled(True, actor_id=_STATE["superadmin_id"])
    got = None
    try:
        resolve_provider("anthropic")
    except RuntimeError as e:
        got = e
    assert got is not None, "expected RuntimeError"
    assert "معطّل" in str(got)


@check("9. resolve_provider walks fallback chain when preferred raises")
def _():
    from unittest.mock import patch
    from app.services.ai_control import set_fallback_order
    from app.services import ai_providers
    _setup()
    set_fallback_order(["deepseek", "anthropic"],
                        actor_id=_STATE["superadmin_id"])

    # Sentinel provider objects to identify which branch resolved.
    class _AnthOK:
        pass

    def _boom_ds(*a, **kw):
        raise RuntimeError("DEEPSEEK_API_KEY غير مضبوط")

    with patch.object(ai_providers, "DeepseekProvider", _boom_ds), \
         patch.object(ai_providers, "AnthropicProvider",
                       lambda *a, **kw: _AnthOK()):
        got = ai_providers.resolve_provider("deepseek")

    assert isinstance(got, _AnthOK), \
        f"fallback did not reach anthropic; got {type(got).__name__}"


@check("10. get_max_tokens_setting clamps out-of-range values")
def _():
    from app.services.subscription import _set_setting_raw
    from app.services.ai_providers import get_max_tokens_setting
    _setup()

    _set_setting_raw("ai_max_tokens_per_turn", "100"); db.session.commit()
    assert get_max_tokens_setting() == 256, "below-256 not clamped up"

    _set_setting_raw("ai_max_tokens_per_turn", "999999"); db.session.commit()
    assert get_max_tokens_setting() == 32000, "above-32000 not clamped down"

    _set_setting_raw("ai_max_tokens_per_turn", "hello"); db.session.commit()
    assert get_max_tokens_setting() == 4096, "malformed did not fall back"


@check("11. turn_log filters by hours window")
def _():
    from app.services.ai_control import turn_log
    from app.models import AiTokenUsage
    _setup()
    now = datetime.utcnow()
    for i in range(3):
        db.session.add(AiTokenUsage(
            company_id=_STATE["company_id"],
            user_id=_STATE["superadmin_id"],
            provider="anthropic", model="claude-sonnet-4-5",
            input_tokens=100 + i, output_tokens=200 + i,
            total_tokens=300 + i,
            created_at=now - timedelta(minutes=i * 10)))
    for i in range(2):
        db.session.add(AiTokenUsage(
            company_id=_STATE["company_id"],
            user_id=_STATE["superadmin_id"],
            provider="anthropic", model="claude-sonnet-4-5",
            input_tokens=1, output_tokens=1, total_tokens=2,
            created_at=now - timedelta(hours=3 + i)))
    db.session.commit()

    rows = turn_log(company_id=_STATE["company_id"], hours=1)
    assert len(rows) == 3, f"expected 3, got {len(rows)}"
    # Newest-first.
    for a, b in zip(rows, rows[1:]):
        assert a.created_at >= b.created_at


@check("12. GET /admin/ai-control renders 200 with every card marker")
def _():
    _setup()
    from flask import g
    try:
        g.pop("_login_user", None)
    except (KeyError, AttributeError):
        pass
    db.session.expire_all()
    db.session.remove()

    app = _STATE["app"]
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(_STATE["superadmin_id"])
        s["_fresh"] = True

    r = client.get("/admin/ai-control")
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)
    for marker in ("⚡", "🧠", "🔁", "🎛", "📊", "🧬"):
        assert marker in body, f"card marker {marker!r} missing"


# ─── Runner ────────────────────────────────────────────────────
def main():
    app = create_app()
    _STATE["app"] = app
    passed = failed = 0
    failures = []
    with app.app_context():
        for label, fn in CHECKS:
            try:
                # Composer checks need a request context for
                # url_for(); route-smoke check 12 uses test_client
                # which pushes its own — keep them out of the
                # outer ctx to avoid cached-current_user leakage.
                if label.startswith("12."):
                    fn()
                else:
                    with app.test_request_context("/admin/ai-control"):
                        fn()
            except AssertionError as e:
                failed += 1
                failures.append((label, str(e)))
                _p(f"  [FAIL] {label}: {e}")
                continue
            except Exception as e:
                failed += 1
                failures.append((label, f"{type(e).__name__}: {e}"))
                _p(f"  [ERROR] {label}: {type(e).__name__}: {e}")
                continue
            passed += 1
            _p(f"  [OK] {label}")
        _teardown()
    _p("")
    _p(f"audit_ai_control: {passed} passed, {failed} failed")
    if failures:
        for label, err in failures:
            _p(f"  - {label} :: {err}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
