#!/usr/bin/env python3
"""MARSOUD-AGENT-MEMORY-05 (2026-08-06) — audit for the conversation
boundary layer.

Pre-ticket, the agent loaded the last 20 messages per (user,
company, agent_type) with no time boundary and no notion of
conversations. This suite pins that:

  · every load scopes to a specific conversation
  · a new conversation is a clean slate
  · a two-month-old message can't bleed into today's turn
  · cross-user / cross-tenant refuses
  · agent_type axis preserved (accountant ≠ insights)
  · retention setting drives auto-delete
  · legacy pre-migration messages got bucketed
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__AGMEM_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import Company, Plan, User
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company

    plan = Plan.query.filter_by(code="__agmem__").first()
    if not plan:
        plan = Plan(code="__agmem__", name="AgMem", name_ar="ذاكرة",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "reports",
                          "agent", "settings"])
        db.session.add(plan); db.session.flush()

    def _mk_co(suffix):
        co = Company(name=f"{PREFIX}CO_{suffix}__", base_currency="SAR",
                     plan_id=plan.id, timezone="Asia/Riyadh")
        db.session.add(co); db.session.flush()
        co.intended_plan_id = plan.id
        db.session.commit()
        ensure_roles_ready_for_company(co.id)
        return co

    def _mk_user(co, tag, role):
        u = User(email=f"{PREFIX}{co.id}_{tag}@audit.local",
                 full_name=f"{tag}-{role}", is_active=True,
                 terms_version=get_terms_version(),
                 terms_accepted_at=datetime.utcnow())
        u.set_password("Passw0rd!audit1")
        db.session.add(u); db.session.flush()
        set_membership_role(u.id, co.id, role)
        return u.id

    co_a = _mk_co("A")
    co_b = _mk_co("B")
    u1 = _mk_user(co_a, "u1", "owner")
    u2 = _mk_user(co_a, "u2", "accountant")
    u_b = _mk_user(co_b, "b", "owner")

    _STATE.update(cid_a=co_a.id, cid_b=co_b.id,
                  u1=u1, u2=u2, u_b=u_b)


def _teardown():
    from app.models import Company, User, PlatformSetting
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(
                        text(f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        db.session.execute(text("DELETE FROM companies WHERE id=:c"),
                           {"c": cid})
        db.session.commit()
    db.session.execute(text("DELETE FROM plans WHERE code='__agmem__'"))
    PlatformSetting.query.filter_by(
        key="agent_conversation_retention_days").delete()
    db.session.commit()
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"),
                           {"u": u.id})
    db.session.commit()


def _reset():
    from app.models import (AgentConversation, AgentMessage,
                            PlatformSetting)
    AgentMessage.query.delete()
    AgentConversation.query.delete()
    PlatformSetting.query.filter_by(
        key="agent_conversation_retention_days").delete()
    db.session.commit()


def _add_message(conv_id, user_id, company_id, role, content,
                  agent_type="accountant"):
    from app.models import AgentMessage
    m = AgentMessage(company_id=company_id, user_id=user_id,
                      role=role, content=content,
                      agent_type=agent_type,
                      conversation_id=conv_id)
    db.session.add(m); db.session.commit()
    return m


def _set(key, value):
    from app.models import PlatformSetting
    row = PlatformSetting.query.filter_by(key=key).first()
    if row is None:
        row = PlatformSetting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()


def _post_as(user_id, company_id, path, data=None, method="POST"):
    """Fresh app_context per identity switch — same trap all prior
    audit suites hit if they skip this."""
    with _STATE["app"].app_context():
        c = _STATE["app"].test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(user_id)
            s["_fresh"] = True
            s["active_company_id"] = company_id
        if method == "POST":
            return c.post(path, json=data or {}, follow_redirects=False)
        if method == "DELETE":
            return c.delete(path, follow_redirects=False)
        return c.get(path, follow_redirects=False)


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. new conversation creates a clean scope")
def _():
    from app.services.agent_conversations import create_conversation
    from app.routes.agent import _load_conversation_history
    _reset()
    conv = create_conversation(_STATE["u1"], _STATE["cid_a"])
    _add_message(conv.id, _STATE["u1"], _STATE["cid_a"],
                  "user", "hello")
    _add_message(conv.id, _STATE["u1"], _STATE["cid_a"],
                  "assistant", "hi")
    hist = _load_conversation_history(conv.id)
    assert len(hist) == 2
    return f"conv #{conv.id}: {len(hist)} messages"


@check("2. old conversation's messages do NOT leak into a new one")
def _():
    from app.services.agent_conversations import create_conversation
    from app.routes.agent import _load_conversation_history
    _reset()
    a = create_conversation(_STATE["u1"], _STATE["cid_a"])
    for i in range(5):
        _add_message(a.id, _STATE["u1"], _STATE["cid_a"],
                      "user", f"A-msg-{i}")
    b = create_conversation(_STATE["u1"], _STATE["cid_a"])
    _add_message(b.id, _STATE["u1"], _STATE["cid_a"],
                  "user", "B-only")
    hist_b = _load_conversation_history(b.id)
    contents = {m.content for m in hist_b}
    assert contents == {"B-only"}, (
        f"CONTEXT LEAK: B got {contents}")
    return f"B={len(hist_b)} · A-msgs invisible"


@check("3. loading conversation A returns A's messages only")
def _():
    from app.services.agent_conversations import create_conversation
    from app.routes.agent import _load_conversation_history
    _reset()
    a = create_conversation(_STATE["u1"], _STATE["cid_a"])
    b = create_conversation(_STATE["u1"], _STATE["cid_a"])
    _add_message(a.id, _STATE["u1"], _STATE["cid_a"], "user", "A1")
    _add_message(b.id, _STATE["u1"], _STATE["cid_a"], "user", "B1")
    hist_a = _load_conversation_history(a.id)
    assert [m.content for m in hist_a] == ["A1"]
    return "A load returns exactly A"


@check("4. get_or_create_current_conversation is per-user")
def _():
    from app.services.agent_conversations import (
        get_or_create_current_conversation,
    )
    _reset()
    c1 = get_or_create_current_conversation(
        _STATE["u1"], _STATE["cid_a"])
    c2 = get_or_create_current_conversation(
        _STATE["u2"], _STATE["cid_a"])
    assert c1.id != c2.id, (
        "cross-user reuse: u1 and u2 got the same conversation")
    # Same user, same call → same conversation (reuse the open one)
    c1_again = get_or_create_current_conversation(
        _STATE["u1"], _STATE["cid_a"])
    assert c1_again.id == c1.id
    return "u1 ≠ u2; same u1 reuses"


@check("5. cross-user leak refused via /conversations/<id>/messages")
def _():
    from app.services.agent_conversations import create_conversation
    _reset()
    conv_u1 = create_conversation(_STATE["u1"], _STATE["cid_a"])
    _add_message(conv_u1.id, _STATE["u1"], _STATE["cid_a"],
                  "user", "secret")
    r = _post_as(_STATE["u2"], _STATE["cid_a"],
                 f"/agent/conversations/{conv_u1.id}/messages",
                 method="GET")
    assert r.status_code == 404, (
        f"u2 got u1's messages: status={r.status_code}")
    return "u2 → 404 on u1's conversation"


@check("6. cross-tenant leak refused")
def _():
    from app.services.agent_conversations import create_conversation
    _reset()
    # A conversation from company B for user u_b
    conv_b = create_conversation(_STATE["u_b"], _STATE["cid_b"])
    # u1 signed into A tries to fetch it
    r = _post_as(_STATE["u1"], _STATE["cid_a"],
                 f"/agent/conversations/{conv_b.id}/messages",
                 method="GET")
    assert r.status_code == 404, (
        f"CROSS-TENANT LEAK: got status {r.status_code}")
    return "A cannot fetch B's conversation"


@check("7. agent_type axis preserved — accountant sidebar excludes insights")
def _():
    from app.services.agent_conversations import (
        create_conversation, list_conversations_for,
    )
    _reset()
    acc = create_conversation(_STATE["u1"], _STATE["cid_a"],
                                agent_type="accountant")
    ins = create_conversation(_STATE["u1"], _STATE["cid_a"],
                                agent_type="insights")
    lst = list_conversations_for(_STATE["u1"], _STATE["cid_a"],
                                    agent_type="accountant")
    ids = {c.id for c in lst}
    assert acc.id in ids and ins.id not in ids, (
        f"agent_type mixed: {ids}, ins.id={ins.id}")
    return "accountant list excludes insights"


@check("8. retention setting drives expiry (1 day → 2-day-old gone)")
def _():
    from app.services.agent_conversations import (
        create_conversation, expire_old_conversations,
    )
    from app.models import AgentConversation, AgentMessage
    _reset()
    _set("agent_conversation_retention_days", "1")
    conv = create_conversation(_STATE["u1"], _STATE["cid_a"])
    _add_message(conv.id, _STATE["u1"], _STATE["cid_a"],
                  "user", "old")
    # Backdate
    conv.last_message_at = datetime.utcnow() - timedelta(days=2)
    db.session.commit()
    conv_id = conv.id  # save before delete; the ORM refuses to
    # touch conv.id after a delete (ObjectDeletedError).
    result = expire_old_conversations()
    assert result.get("deleted_conversations", 0) >= 1
    db.session.expire_all()
    assert db.session.get(AgentConversation, conv_id) is None
    assert AgentMessage.query.filter_by(
        conversation_id=conv_id).count() == 0
    return f"expired conv + {result['deleted_messages']} messages"


@check("9. retention = 0 disables expiry")
def _():
    from app.services.agent_conversations import (
        create_conversation, expire_old_conversations,
    )
    from app.models import AgentConversation
    _reset()
    _set("agent_conversation_retention_days", "0")
    conv = create_conversation(_STATE["u1"], _STATE["cid_a"])
    _add_message(conv.id, _STATE["u1"], _STATE["cid_a"],
                  "user", "keep me")
    conv.last_message_at = datetime.utcnow() - timedelta(days=365)
    db.session.commit()
    result = expire_old_conversations()
    assert "skipped" in result, f"expiry didn't skip: {result}"
    assert db.session.get(AgentConversation, conv.id) is not None
    return "retention=0 → old conv untouched"


@check("10. legacy messages (post-migration) have non-null conversation_id")
def _():
    """The migration backfilled every legacy AgentMessage with a
    bucket AgentConversation. This check ties into that guarantee
    by walking the existing DB and asserting no NULL conversation_id
    rows remain."""
    from app.models import AgentMessage
    n = AgentMessage.query.filter(
        AgentMessage.conversation_id.is_(None)).count()
    assert n == 0, (
        f"{n} legacy messages still have NULL conversation_id — the "
        "migration backfill didn't cover them")
    return "0 orphan messages"


@check("11. soft-delete hides from sidebar but keeps messages")
def _():
    from app.services.agent_conversations import (
        create_conversation, archive_conversation,
        list_conversations_for,
    )
    from app.models import AgentMessage
    _reset()
    conv = create_conversation(_STATE["u1"], _STATE["cid_a"])
    _add_message(conv.id, _STATE["u1"], _STATE["cid_a"],
                  "user", "kept")
    archive_conversation(conv)
    sidebar = list_conversations_for(
        _STATE["u1"], _STATE["cid_a"])
    ids = {c.id for c in sidebar}
    assert conv.id not in ids, (
        "archived conv still in sidebar")
    # Messages still there
    assert AgentMessage.query.filter_by(
        conversation_id=conv.id).count() == 1
    return "sidebar excludes archived; message row lingers"


@check("12. /conversations/new endpoint returns id, persists row")
def _():
    from app.models import AgentConversation
    _reset()
    r = _post_as(_STATE["u1"], _STATE["cid_a"],
                 "/agent/conversations/new")
    assert r.status_code == 200, (
        f"status={r.status_code} body={r.get_data(as_text=True)[:200]}")
    data = r.get_json()
    assert data.get("id"), f"no id in response: {data}"
    row = db.session.get(AgentConversation, data["id"])
    assert row is not None
    assert row.user_id == _STATE["u1"]
    assert row.company_id == _STATE["cid_a"]
    return f"new conv #{data['id']} persisted"


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
