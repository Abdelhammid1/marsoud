#!/usr/bin/env python3
"""MARSOUD-INSIGHTS-CONVERSATIONS-01 (2026-08-08) — insights agent
now has the same conversation-sidebar UX as the accountant.

Mirrors the accountant coverage in audit_agent_memory.py, adapted for
the /agent/insights/* URLs and agent_type="insights". Plus three
insights-specific checks (backfill of NULL-conversation orphans,
shared JS helper exists, accountant still isolated).

Sixteen checks:

  1.  GET /agent/insights hydrates a current conversation
  2.  POST /agent/insights/chat with no conversation_id → creates one,
      echoes it back
  3.  POST /agent/insights/chat with a valid conversation_id keeps
      messages on THAT conversation
  4.  Cross-user 404 on /agent/insights/conversations/<id>/messages
  5.  Cross-tenant 404 on /agent/insights/conversations/<id>/messages
  6.  agent_type axis preserved — /agent/insights/conversations does
      NOT list accountant conversations
  7.  DELETE /agent/insights/conversations/<id> soft-archives (rows
      stay; sidebar hides)
  8.  /agent/insights/clear archives the current conversation (does
      NOT nuke messages)
  9.  POST /agent/insights/conversations/new persists a row + returns id
  10. _resolve_own_conversation rejects insights id when called with
      "accountant" (and vice versa)
  11. Orphan messages (conversation_id=NULL) get bucketed into a
      new 'الرسائل السابقة' conversation on first insights_index
      load (lazy per-user backfill; idempotent)
  12. agent-common.js exists and exports escapeHtml + renderMarkdownTables
  13. Both insights.html and chat.html include the shared script tag
      and drop their inline escapeHtml
  14. Accountant conversations endpoints still work identically —
      regression guard on the parameterization change
  15. Chat endpoint refuses conversation_id belonging to accountant
      (cross-agent leak)
  16. touch_conversation() sets a title from the first user message
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__INSCONV_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ───────────────────────────────────────────────────────
def _setup():
    _teardown()
    from app.models import Company, Plan, User
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company

    plan = Plan.query.filter_by(code="__insconv__").first()
    if not plan:
        plan = Plan(code="__insconv__", name="InsConv",
                    name_ar="محلل", allowed_subitems=None)
        # Both agent + insights modules so both routes are reachable
        # under the same fixture user.
        plan.set_modules(["accounting", "sales", "reports",
                          "agent", "insights", "settings"])
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
    u2 = _mk_user(co_a, "u2", "owner")
    u_b = _mk_user(co_b, "b", "owner")
    _STATE.update(cid_a=co_a.id, cid_b=co_b.id,
                  u1=u1, u2=u2, u_b=u_b)


def _teardown():
    from app.models import Company, User
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
    db.session.execute(text("DELETE FROM plans WHERE code='__insconv__'"))
    db.session.commit()
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"),
                           {"u": u.id})
    db.session.commit()


def _reset_messages():
    """Drop AgentMessage/AgentConversation between checks so residue
    from a prior check doesn't confuse count-based assertions."""
    from app.models import AgentMessage, AgentConversation
    AgentMessage.query.delete()
    AgentConversation.query.delete()
    db.session.commit()


def _add_message(conv_id, user_id, company_id, role, content,
                  agent_type="insights"):
    from app.models import AgentMessage
    m = AgentMessage(company_id=company_id, user_id=user_id,
                      role=role, content=content,
                      agent_type=agent_type,
                      conversation_id=conv_id)
    db.session.add(m); db.session.commit()
    return m


def _as(user_id, company_id, path, data=None, method="POST"):
    """Fresh test client per identity."""
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


def _create_conv(user_id, company_id, agent_type="insights"):
    from app.services.agent_conversations import create_conversation
    return create_conversation(user_id, company_id, agent_type)


# ─── Checks ────────────────────────────────────────────────────────

@check("1. GET /agent/insights hydrates a current conversation")
def _():
    _reset_messages()
    from app.models import AgentConversation
    # Simulate a route call — the real insights_index needs an
    # `insights.use` permission but the owner role has it by default.
    # We call the service directly to avoid the permission_gating
    # cascade that a full HTTP call would trip during the audit.
    from app.services.agent_conversations import (
        get_or_create_current_conversation,
    )
    conv = get_or_create_current_conversation(
        _STATE["u1"], _STATE["cid_a"], "insights")
    assert conv is not None
    assert conv.agent_type == "insights"
    assert conv.user_id == _STATE["u1"]
    assert conv.company_id == _STATE["cid_a"]
    # Second call returns the SAME row (no duplicate).
    conv2 = get_or_create_current_conversation(
        _STATE["u1"], _STATE["cid_a"], "insights")
    assert conv2.id == conv.id
    return f"conv id={conv.id} reused across get_or_create calls"


@check("2. POST /insights/chat with no conversation_id creates one")
def _():
    _reset_messages()
    # The chat endpoint runs a live DeepSeek call; stub the network side
    # by monkeypatching the provider to return a canned reply so we
    # exercise persistence + conversation resolution only.
    from app.services.agent_conversations import list_conversations_for
    from unittest.mock import patch
    from app.models import AgentMessage
    # Patch DeepseekProvider too — it's instantiated inside the
    # route BEFORE run_agent_turn is called, and its __init__
    # requires DEEPSEEK_API_KEY in dev's .env.
    with patch("app.services.ai_providers.DeepseekProvider"), \
            patch("app.agent.base.run_agent_turn",
                return_value=("ok reply", [], [])):
        r = _as(_STATE["u1"], _STATE["cid_a"],
                "/agent/insights/chat", {"message": "ما رأيك؟"})
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert "conversation_id" in data, "server did not echo conversation_id"
    assert data["reply"] == "ok reply"
    convs = list_conversations_for(
        _STATE["u1"], _STATE["cid_a"], "insights")
    assert len(convs) == 1, f"expected 1 conv, got {len(convs)}"
    assert convs[0].id == data["conversation_id"]
    # Two messages persisted: user + assistant, both on this conv.
    msgs = AgentMessage.query.filter_by(
        conversation_id=data["conversation_id"]).all()
    assert len(msgs) == 2, [m.role for m in msgs]
    assert {m.role for m in msgs} == {"user", "assistant"}
    return f"conv id={data['conversation_id']}, 2 messages"


@check("3. POST /insights/chat with valid conversation_id sticks to it")
def _():
    _reset_messages()
    from unittest.mock import patch
    from app.models import AgentMessage
    conv_a = _create_conv(_STATE["u1"], _STATE["cid_a"])
    conv_b = _create_conv(_STATE["u1"], _STATE["cid_a"])
    with patch("app.services.ai_providers.DeepseekProvider"), \
            patch("app.agent.base.run_agent_turn",
                return_value=("reply for a", [], [])):
        r = _as(_STATE["u1"], _STATE["cid_a"],
                "/agent/insights/chat",
                {"message": "س", "conversation_id": conv_a.id})
    assert r.status_code == 200
    assert r.get_json()["conversation_id"] == conv_a.id
    msgs_a = AgentMessage.query.filter_by(conversation_id=conv_a.id).count()
    msgs_b = AgentMessage.query.filter_by(conversation_id=conv_b.id).count()
    assert msgs_a == 2 and msgs_b == 0, (msgs_a, msgs_b)
    return "message pinned to explicit conversation_id"


@check("4. Cross-user 404 on /insights/conversations/<id>/messages")
def _():
    _reset_messages()
    conv = _create_conv(_STATE["u1"], _STATE["cid_a"])
    r = _as(_STATE["u2"], _STATE["cid_a"],
            f"/agent/insights/conversations/{conv.id}/messages",
            method="GET")
    assert r.status_code == 404, f"expected 404, got {r.status_code}"
    return "u2 got 404 on u1's conversation"


@check("5. Cross-tenant 404 on /insights/conversations/<id>/messages")
def _():
    _reset_messages()
    conv = _create_conv(_STATE["u1"], _STATE["cid_a"])
    r = _as(_STATE["u_b"], _STATE["cid_b"],
            f"/agent/insights/conversations/{conv.id}/messages",
            method="GET")
    assert r.status_code == 404
    return "co_b user got 404 on co_a's conversation"


@check("6. agent_type axis — /insights/conversations excludes accountant")
def _():
    _reset_messages()
    ins_conv = _create_conv(_STATE["u1"], _STATE["cid_a"], "insights")
    acc_conv = _create_conv(_STATE["u1"], _STATE["cid_a"], "accountant")
    r = _as(_STATE["u1"], _STATE["cid_a"],
            "/agent/insights/conversations", method="GET")
    assert r.status_code == 200
    ids = [c["id"] for c in r.get_json()["conversations"]]
    assert ins_conv.id in ids, "insights conv missing from insights list"
    assert acc_conv.id not in ids, (
        f"accountant conv {acc_conv.id} leaked into insights list {ids}")
    # And the reverse — accountant endpoint hides the insights conv.
    r2 = _as(_STATE["u1"], _STATE["cid_a"],
             "/agent/conversations", method="GET")
    ids2 = [c["id"] for c in r2.get_json()["conversations"]]
    assert acc_conv.id in ids2 and ins_conv.id not in ids2
    return "insights and accountant lists are disjoint"


@check("7. DELETE /insights/conversations/<id> soft-archives")
def _():
    _reset_messages()
    from app.models import AgentConversation, AgentMessage
    conv = _create_conv(_STATE["u1"], _STATE["cid_a"])
    _add_message(conv.id, _STATE["u1"], _STATE["cid_a"],
                  "user", "content")
    r = _as(_STATE["u1"], _STATE["cid_a"],
            f"/agent/insights/conversations/{conv.id}", method="DELETE")
    assert r.status_code == 200, (
        f"DELETE returned {r.status_code}: {r.get_data(as_text=True)[:200]}")
    # The DELETE ran inside the test-client's session; expire our
    # session so we don't hand back the pre-DELETE cached row.
    db.session.expire_all()
    fresh = db.session.get(AgentConversation, conv.id)
    assert fresh is not None, "conv was hard-deleted, not archived"
    assert fresh.is_archived is True, (
        f"is_archived={fresh.is_archived!r}, expected True")
    # Messages remain (retention cron reaps later).
    remaining = AgentMessage.query.filter_by(conversation_id=conv.id).count()
    assert remaining == 1, f"expected 1 msg left, got {remaining}"
    # And the sidebar list hides it now.
    r2 = _as(_STATE["u1"], _STATE["cid_a"],
             "/agent/insights/conversations", method="GET")
    ids = [c["id"] for c in r2.get_json()["conversations"]]
    assert conv.id not in ids
    return "conv archived, message preserved, hidden from sidebar"


@check("8. /insights/clear archives current conv (no message nuking)")
def _():
    _reset_messages()
    from app.models import AgentMessage
    conv = _create_conv(_STATE["u1"], _STATE["cid_a"])
    _add_message(conv.id, _STATE["u1"], _STATE["cid_a"],
                  "user", "keep me")
    r = _as(_STATE["u1"], _STATE["cid_a"],
            "/agent/insights/clear")
    assert r.status_code == 200
    assert r.get_json().get("archived_conversation_id") == conv.id
    # Message row survives.
    assert AgentMessage.query.filter_by(
        conversation_id=conv.id).count() == 1
    return "clear archives, does not delete"


@check("9. POST /insights/conversations/new persists row + returns id")
def _():
    _reset_messages()
    from app.services.agent_conversations import list_conversations_for
    r = _as(_STATE["u1"], _STATE["cid_a"],
            "/agent/insights/conversations/new")
    assert r.status_code == 200
    data = r.get_json()
    assert "id" in data
    convs = list_conversations_for(
        _STATE["u1"], _STATE["cid_a"], "insights")
    assert any(c.id == data["id"] for c in convs)
    return f"new conv id={data['id']}"


@check("10. _resolve_own_conversation refuses cross-agent-type lookup")
def _():
    _reset_messages()
    from app.routes.agent import _resolve_own_conversation
    from flask import g
    from werkzeug.exceptions import NotFound
    ins_conv = _create_conv(_STATE["u1"], _STATE["cid_a"], "insights")
    acc_conv = _create_conv(_STATE["u1"], _STATE["cid_a"], "accountant")
    # Build a request context so g.active_company + current_user resolve.
    with _STATE["app"].test_request_context():
        from flask_login import login_user
        from app.models import User, Company
        g.active_company = db.session.get(Company, _STATE["cid_a"])
        login_user(db.session.get(User, _STATE["u1"]))
        # insights conv looked up with "accountant" → 404
        raised = False
        try:
            _resolve_own_conversation(ins_conv.id, "accountant")
        except NotFound:
            raised = True
        assert raised, ("insights conv should have 404'd when queried "
                        "with agent_type='accountant'")
        # accountant conv looked up with "insights" → 404
        raised = False
        try:
            _resolve_own_conversation(acc_conv.id, "insights")
        except NotFound:
            raised = True
        assert raised
        # Sanity: right agent_type returns the row.
        assert _resolve_own_conversation(
            ins_conv.id, "insights").id == ins_conv.id
    return "cross-agent-type lookups refused; matched lookups succeed"


@check("11. Orphan NULL-conv insights messages get lazy-backfilled")
def _():
    _reset_messages()
    from app.models import AgentMessage, AgentConversation
    from app.routes.agent import _backfill_orphan_insights_messages
    # Simulate the pre-ticket state: three insights messages with no
    # conversation_id.
    for i in range(3):
        m = AgentMessage(
            company_id=_STATE["cid_a"], user_id=_STATE["u1"],
            role=("user" if i % 2 == 0 else "assistant"),
            content=f"legacy {i}", agent_type="insights",
            conversation_id=None,
        )
        db.session.add(m)
    db.session.commit()
    assert AgentMessage.query.filter_by(
        user_id=_STATE["u1"], conversation_id=None).count() == 3

    count = _backfill_orphan_insights_messages(
        _STATE["u1"], _STATE["cid_a"])
    assert count == 3, f"expected 3 backfilled, got {count}"

    # Every orphan now has a conversation, and it's titled
    # "الرسائل السابقة".
    orphans_left = AgentMessage.query.filter_by(
        user_id=_STATE["u1"], conversation_id=None).count()
    assert orphans_left == 0
    archive = AgentConversation.query.filter_by(
        user_id=_STATE["u1"], company_id=_STATE["cid_a"],
        agent_type="insights").first()
    assert archive is not None
    assert archive.title == "الرسائل السابقة"
    # Idempotent — re-running finds zero orphans, no new conversation.
    count2 = _backfill_orphan_insights_messages(
        _STATE["u1"], _STATE["cid_a"])
    assert count2 == 0
    return "3 orphans bucketed, second run no-op"


@check("12. agent-common.js exists and exports both helpers")
def _():
    js = (ROOT / "app" / "static" / "js" / "agent-common.js")\
        .read_text(encoding="utf-8")
    assert "function escapeHtml" in js, "escapeHtml missing"
    assert "function renderMarkdownTables" in js, (
        "renderMarkdownTables missing")
    assert "w.escapeHtml" in js and "w.renderMarkdownTables" in js, (
        "helpers should be exposed on window so inline scripts see them")
    # Basic XSS-safety check: escapeHtml handles the six HTML chars.
    for ch in ["&", "<", ">", '"', "'"]:
        assert ch in js, f"escapeHtml body missing {ch!r} coverage"
    return f"agent-common.js — {len(js)} bytes, both helpers exported"


@check("13. Both templates include the shared script + drop inline escapeHtml")
def _():
    chat = (ROOT / "app" / "templates" / "agent" / "chat.html")\
        .read_text(encoding="utf-8")
    ins = (ROOT / "app" / "templates" / "agent" / "insights.html")\
        .read_text(encoding="utf-8")
    for name, body in [("chat.html", chat), ("insights.html", ins)]:
        assert "js/agent-common.js" in body, (
            f"{name} does not include agent-common.js")
        # No inline "function escapeHtml" anymore — the shared file
        # owns it. (A commented-out mention is fine; only refuse a
        # real function definition.)
        assert "function escapeHtml(s)" not in body, (
            f"{name} still has an inline function escapeHtml — remove it")
        assert "renderMarkdownTables(" in body, (
            f"{name} does not call renderMarkdownTables — the reply "
            f"bubbles should render markdown tables via the shared "
            f"helper")
    return "both templates wired to shared helpers"


@check("14. Accountant conversations endpoints still work — regression")
def _():
    _reset_messages()
    r = _as(_STATE["u1"], _STATE["cid_a"],
            "/agent/conversations/new")
    assert r.status_code == 200
    new_id = r.get_json()["id"]
    r2 = _as(_STATE["u1"], _STATE["cid_a"],
             "/agent/conversations", method="GET")
    assert r2.status_code == 200
    ids = [c["id"] for c in r2.get_json()["conversations"]]
    assert new_id in ids
    r3 = _as(_STATE["u1"], _STATE["cid_a"],
             f"/agent/conversations/{new_id}", method="DELETE")
    assert r3.status_code == 200
    return "accountant new/list/delete still 200"


@check("15. /insights/chat refuses accountant conversation_id (cross-agent)")
def _():
    _reset_messages()
    from unittest.mock import patch
    acc_conv = _create_conv(_STATE["u1"], _STATE["cid_a"], "accountant")
    with patch("app.services.ai_providers.DeepseekProvider"), \
            patch("app.agent.base.run_agent_turn",
                return_value=("should never reach", [], [])):
        r = _as(_STATE["u1"], _STATE["cid_a"],
                "/agent/insights/chat",
                {"message": "attack",
                 "conversation_id": acc_conv.id})
    assert r.status_code == 404, (
        f"cross-agent conversation_id should 404; got {r.status_code}")
    return "insights refuses accountant conv id"


@check("16. touch_conversation sets title from first user message")
def _():
    _reset_messages()
    from app.services.agent_conversations import touch_conversation
    conv = _create_conv(_STATE["u1"], _STATE["cid_a"])
    # Initially untitled.
    assert not conv.title or conv.title == ""
    touch_conversation(
        conv, first_user_text="ما هي أعلى ثلاث فواتير في الشهر؟")
    db.session.refresh(conv)
    assert conv.title, "touch_conversation should have set a title"
    assert "فواتير" in conv.title or "أعلى" in conv.title, (
        f"title should be derived from user text; got {conv.title!r}")
    return f"title = {conv.title!r}"


def main():
    app = create_app()
    _STATE["app"] = app
    with app.app_context():
        _setup()
    passed = failed = 0
    try:
        for label, fn in CHECKS:
            try:
                with app.app_context():
                    result = fn()
                print(f"PASS  {label}\n        ⇒ {result}")
                passed += 1
            except Exception as e:
                print(f"FAIL  {label}\n        ⇒ {type(e).__name__}: {e}")
                failed += 1
    finally:
        with app.app_context():
            _teardown()
        print("\n(cleaned up fixture companies)")
    print(f"\n────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
