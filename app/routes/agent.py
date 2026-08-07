from flask import (
    Blueprint, render_template, request, jsonify, g, abort,
    current_app,
)
from flask_login import login_required, current_user
from app import db
from app.models import AgentMessage, AgentConversation
from app.agent.accountant import run_agent
from app.services.permissions import require_permission

bp = Blueprint("agent", __name__)


# ─── MARSOUD-AGENT-MEMORY-05 (2026-08-06) — history loading ────
# Every load now scopes to a SPECIFIC conversation, not the last N
# messages per user. This is the load-bearing fix — a two-month-old
# topic can no longer bleed into today's turn because it's in a
# different conversation. The legacy "last 20 per user" function is
# gone; if anything else in this codebase relied on it, it needs to
# resolve a conversation first.
def _load_conversation_history(conversation_id, limit=40):
    q = AgentMessage.query.filter_by(
        conversation_id=conversation_id,
    ).order_by(AgentMessage.created_at.desc()).limit(limit)
    rows = list(q)
    rows.reverse()
    return rows


def _load_history_legacy(company_id, user_id, agent_type, limit=20):
    """Legacy shape kept for the insights route only — insights hasn't
    been migrated to conversations yet (deferred per T5's Not-Included
    section). Delete when insights adopts the sidebar."""
    q = AgentMessage.query.filter_by(
        company_id=company_id, user_id=user_id,
        agent_type=agent_type,
    ).order_by(AgentMessage.created_at.desc()).limit(limit)
    rows = list(q)
    rows.reverse()
    return rows


def _load_history(company_id, user_id, agent_type, limit=20):
    """Backwards-compat wrapper for callers that still expect the
    pre-T5 shape (the insights route). New code uses
    _load_conversation_history."""
    return _load_history_legacy(company_id, user_id, agent_type, limit)


# ─── Accountant (existing) ─────────────────────────────────────
@bp.route("/")
@login_required
def index():
    """Accountant chat page. Loads the user's most recent open
    conversation as the initial view; the sidebar lists the rest."""
    from app.services.agent_conversations import (
        get_or_create_current_conversation, list_conversations_for,
    )
    conv = get_or_create_current_conversation(
        current_user.id, g.active_company.id, "accountant")
    history = _load_conversation_history(conv.id, limit=40)
    conversations = list_conversations_for(
        current_user.id, g.active_company.id, "accountant")
    return render_template(
        "agent/chat.html",
        history=history,
        conversations=conversations,
        active_conversation_id=conv.id,
    )


@bp.route("/chat", methods=["POST"])
@login_required
@require_permission("agent.use")
def chat():
    user_msg = (request.json or {}).get("message", "").strip()
    if not user_msg:
        return jsonify({"error": "رسالة فارغة"}), 400
    if not g.active_company:
        return jsonify({"error": "لا توجد شركة نشطة"}), 400

    # MARSOUD-AGENT-MEMORY-05 (2026-08-06) — resolve the conversation
    # first. The client sends conversation_id when it has one; the
    # first turn (or a stale client) sends nothing and we
    # get-or-create the current one. Cross-user + cross-tenant
    # verified here before anything else touches the row.
    from app.services.agent_conversations import (
        get_or_create_current_conversation, touch_conversation,
    )
    cid_from_body = (request.json or {}).get("conversation_id")
    conv = None
    if cid_from_body:
        conv = db.session.get(AgentConversation, int(cid_from_body))
        if (not conv
                or conv.user_id != current_user.id
                or conv.company_id != g.active_company.id
                or conv.agent_type != "accountant"
                or conv.is_archived):
            return jsonify({"error": "المحادثة غير موجودة"}), 404
    if conv is None:
        conv = get_or_create_current_conversation(
            current_user.id, g.active_company.id, "accountant")

    # Persist user message with conversation_id.
    db.session.add(AgentMessage(
        company_id=g.active_company.id, user_id=current_user.id,
        role="user", content=user_msg, agent_type="accountant",
        conversation_id=conv.id,
    ))
    db.session.commit()
    touch_conversation(conv, first_user_text=user_msg)

    history = _load_conversation_history(conv.id, limit=40)

    messages = [{"role": m.role, "content": m.content}
                for m in history if m.role in ("user", "assistant")]

    # MARSOUD-AGENT-CONTEXT-01 (2026-08-06) — the three-line block
    # this replaced didn't tell the agent what "today" was in the
    # company's timezone (so after-midnight-Riyadh queries returned
    # yesterday's rows), didn't say which modules the plan enabled,
    # and didn't surface the ACTUAL account codes for this company —
    # letting the prompt fall back to hardcoded DEFAULT_COA codes
    # that were wrong for any tenant who edited their tree.
    from app.agent.company_context import build_company_context
    company_context = build_company_context(g.active_company)

    try:
        reply, _, tool_trace = run_agent(
            messages, g.active_company.id, current_user.id,
            company_context=company_context,
        )
        # MARSOUD-AGENT-SAFETY-03 (2026-08-06) — persist the tool
        # trace on the assistant message so a later audit can
        # answer "what did the agent actually do?" — the exact
        # question that had no answer during the company-37
        # incident. Legacy rows stay NULL.
        import json as _json
        db.session.add(AgentMessage(
            company_id=g.active_company.id, user_id=current_user.id,
            role="assistant", content=reply,
            agent_type="accountant",
            conversation_id=conv.id,
            tool_trace=_json.dumps(
                tool_trace or [], ensure_ascii=False, default=str),
        ))
        db.session.commit()
        touch_conversation(conv)
        return jsonify({
            "reply": reply,
            "tools": tool_trace,
            "conversation_id": conv.id,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── MARSOUD-AGENT-MEMORY-05 (2026-08-06) — conversation endpoints ───
def _resolve_own_conversation(conv_id):
    """Fetch a conversation but ONLY if it belongs to the current
    user + active company + accountant agent_type. Returns the row
    or aborts 404. Same isolation shape AgentMessage would refuse."""
    c = db.session.get(AgentConversation, conv_id)
    if (not c
            or c.user_id != current_user.id
            or c.company_id != g.active_company.id
            or c.agent_type != "accountant"):
        abort(404)
    return c


@bp.route("/conversations", methods=["GET"])
@login_required
@require_permission("agent.use")
def conversations_list():
    from app.services.agent_conversations import list_conversations_for
    convs = list_conversations_for(
        current_user.id, g.active_company.id, "accountant")
    return jsonify({
        "conversations": [
            {"id": c.id,
             "title": c.title or "محادثة جديدة",
             "last_message_at": c.last_message_at.isoformat()
             if c.last_message_at else None,
             "created_at": c.created_at.isoformat()
             if c.created_at else None}
            for c in convs
        ],
    })


@bp.route("/conversations/new", methods=["POST"])
@login_required
@require_permission("agent.use")
def conversations_new():
    from app.services.agent_conversations import create_conversation
    if not g.active_company:
        return jsonify({"error": "لا توجد شركة نشطة"}), 400
    conv = create_conversation(
        current_user.id, g.active_company.id, "accountant")
    return jsonify({"id": conv.id, "title": conv.title or "محادثة جديدة"})


@bp.route("/conversations/<int:conv_id>/messages", methods=["GET"])
@login_required
@require_permission("agent.use")
def conversations_messages(conv_id):
    conv = _resolve_own_conversation(conv_id)
    msgs = _load_conversation_history(conv.id, limit=200)
    import json as _json
    return jsonify({
        "id": conv.id,
        "title": conv.title or "محادثة جديدة",
        "messages": [
            {"role": m.role, "content": m.content,
             "tool_trace": _json.loads(m.tool_trace)
             if m.tool_trace else None,
             "created_at": m.created_at.isoformat()
             if m.created_at else None}
            for m in msgs
        ],
    })


@bp.route("/conversations/<int:conv_id>", methods=["DELETE"])
@login_required
@require_permission("agent.use")
def conversations_delete(conv_id):
    """Soft-delete — mark archived. Retention cron hard-deletes
    later. Two-step so a future 'restore archive' feature is
    reachable without changing the delete path."""
    from app.services.agent_conversations import archive_conversation
    conv = _resolve_own_conversation(conv_id)
    archive_conversation(conv)
    return jsonify({"ok": True, "id": conv.id, "archived": True})


# ─── MARSOUD-AGENT-SAFETY-03 (2026-08-06) — proposal execute/cancel ───
@bp.route("/proposal/<int:pid>/execute", methods=["POST"])
@login_required
@require_permission("agent.write")
def proposal_execute(pid):
    """Confirm a pending write proposal. gated on agent.write
    (separate from agent.use — a read-only agent user hits 403 here
    without losing chat access)."""
    from app.services.agent_safety import execute_proposal
    if not g.active_company:
        return jsonify({"error": "لا توجد شركة نشطة"}), 400
    result, status = execute_proposal(
        pid, actor_user_id=current_user.id,
        active_company_id=g.active_company.id)
    return jsonify(result), status


@bp.route("/proposal/<int:pid>/cancel", methods=["POST"])
@login_required
@require_permission("agent.use")
def proposal_cancel(pid):
    """Cancel a pending proposal. Same gate as chat itself — anyone
    who can use the agent can cancel proposals they created."""
    from app.services.agent_safety import cancel_proposal
    if not g.active_company:
        return jsonify({"error": "لا توجد شركة نشطة"}), 400
    result, status = cancel_proposal(
        pid, actor_user_id=current_user.id,
        active_company_id=g.active_company.id)
    return jsonify(result), status


@bp.route("/clear", methods=["POST"])
@login_required
@require_permission("agent.use")
def clear():
    """MARSOUD-AGENT-MEMORY-05 (2026-08-06) — reinterpreted.
    Pre-T5 this deleted every message the user had ever sent to
    the accountant. That is a nuclear button; the ticket asked for
    a "new conversation" button instead — archive the current
    conversation so it drops off the sidebar and the next message
    lands in a fresh one. The archive-sweep cron eventually
    hard-deletes; the user's history is preserved until then and
    the sidebar shows every non-archived chat."""
    from app.services.agent_conversations import (
        get_or_create_current_conversation, archive_conversation,
    )
    conv = get_or_create_current_conversation(
        current_user.id, g.active_company.id, "accountant")
    archive_conversation(conv)
    return jsonify({"ok": True, "archived_conversation_id": conv.id})


# ─── Insights (new, DeepSeek) ──────────────────────────────────
@bp.route("/insights")
@login_required
@require_permission("insights.use")
def insights_index():
    history = _load_history(
        g.active_company.id, current_user.id,
        "insights", limit=40,
    )
    return render_template("agent/insights.html", history=history)


@bp.route("/insights/chat", methods=["POST"])
@login_required
@require_permission("insights.use")
def insights_chat():
    user_msg = (request.json or {}).get("message", "").strip()
    if not user_msg:
        return jsonify({"error": "رسالة فارغة"}), 400
    if not g.active_company:
        return jsonify({"error": "لا توجد شركة نشطة"}), 400

    db.session.add(AgentMessage(
        company_id=g.active_company.id, user_id=current_user.id,
        role="user", content=user_msg, agent_type="insights",
    ))
    db.session.commit()

    history = _load_history(
        g.active_company.id, current_user.id, "insights")
    messages = [{"role": m.role, "content": m.content}
                for m in history if m.role in ("user", "assistant")]

    company_context = (
        f"اسم الشركة: {g.active_company.name}\n"
        f"العملة الأساسية: {g.active_company.base_currency}\n"
        f"معرّف الشركة (للفلترة الداخلية فقط): "
        f"{g.active_company.id}\n"
    )

    try:
        from app.agent.base import run_agent_turn, insights_persona
        from app.agent.insights_tools import (
            INSIGHTS_TOOL_SCHEMAS, execute_insights_tool,
        )
        from app.services.ai_providers import DeepseekProvider
        # MARSOUD-INSIGHTS-AGENT-PROFESSIONAL (2026-08-06) —
        # max_iters bumped from 5 → 8 (matches accountant). With
        # ~40 tools + 3 composites, a legitimate drill-down (open
        # composite → cross-check on one atomic → close) needs 6+
        # provider turns. The old 5-cap was silently truncating
        # those chains: the loop reset `final_text = ""` on the
        # last non-terminating iter and the user got an empty
        # reply. See app/agent/base.py:130.
        reply, _, tool_trace = run_agent_turn(
            messages=messages,
            company_id=g.active_company.id,
            user_id=current_user.id,
            persona=insights_persona(),
            provider=DeepseekProvider(),
            tools=INSIGHTS_TOOL_SCHEMAS,
            execute_tool_fn=execute_insights_tool,
            company_context=company_context,
            max_iters=8,
        )
        # MARSOUD-INSIGHTS-AGENT-PROFESSIONAL — persist tool_trace
        # on the assistant row (accountant already does this; the
        # insights route was just missing it). Enables the audit
        # + latency dashboard to answer "what did the analyst run
        # yesterday? how long did each tool take?".
        import json as _json
        db.session.add(AgentMessage(
            company_id=g.active_company.id,
            user_id=current_user.id, role="assistant",
            content=reply, agent_type="insights",
            tool_trace=(_json.dumps(tool_trace, default=str,
                                    ensure_ascii=False)
                        if tool_trace else None),
        ))
        db.session.commit()
        return jsonify({"reply": reply, "tools": tool_trace})
    except Exception as e:  # noqa: BLE001
        # A DeepSeek failure MUST NOT affect the accountant.
        # Log the traceback so dev doesn't lose it — the Arabic
        # response body swallows it otherwise.
        try:
            current_app.logger.exception(
                "insights turn failed for co=%s user=%s",
                g.active_company.id if g.active_company else None,
                current_user.id)
        except Exception:
            pass
        return jsonify({
            "error": ("حصل خطأ في مزوّد الذكاء الاصطناعي — "
                      "المحاسب الذكي مايتأثرش. حاول تاني بعد شوية. "
                      f"({str(e)[:200]})"),
        }), 500


@bp.route("/insights/clear", methods=["POST"])
@login_required
@require_permission("insights.use")
def insights_clear():
    AgentMessage.query.filter_by(
        company_id=g.active_company.id, user_id=current_user.id,
        agent_type="insights",
    ).delete()
    db.session.commit()
    return jsonify({"ok": True})
