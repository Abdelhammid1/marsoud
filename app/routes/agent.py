from flask import Blueprint, render_template, request, jsonify, g
from flask_login import login_required, current_user
from app import db
from app.models import AgentMessage
from app.agent.accountant import run_agent
from app.services.permissions import require_permission

bp = Blueprint("agent", __name__)


# ─── Shared history loader (agent_type aware) ─────────────────
def _load_history(company_id, user_id, agent_type, limit=20):
    """MARSOUD-INSIGHTS-AGENT-01 (Batch 9 Ticket 6, 2026-08-01)
    — filter by agent_type so the accountant and insights
    conversations never mix."""
    q = AgentMessage.query.filter_by(
        company_id=company_id, user_id=user_id,
        agent_type=agent_type,
    ).order_by(AgentMessage.created_at.desc()).limit(limit)
    rows = list(q)
    rows.reverse()
    return rows


# ─── Accountant (existing) ─────────────────────────────────────
@bp.route("/")
@login_required
def index():
    history = _load_history(
        g.active_company.id, current_user.id,
        "accountant", limit=40,
    )
    return render_template("agent/chat.html", history=history)


@bp.route("/chat", methods=["POST"])
@login_required
@require_permission("agent.use")
def chat():
    user_msg = (request.json or {}).get("message", "").strip()
    if not user_msg:
        return jsonify({"error": "رسالة فارغة"}), 400
    if not g.active_company:
        return jsonify({"error": "لا توجد شركة نشطة"}), 400

    # Persist user message (agent_type='accountant' via column default).
    db.session.add(AgentMessage(
        company_id=g.active_company.id, user_id=current_user.id,
        role="user", content=user_msg, agent_type="accountant",
    ))
    db.session.commit()

    history = _load_history(
        g.active_company.id, current_user.id, "accountant")

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
        db.session.add(AgentMessage(
            company_id=g.active_company.id, user_id=current_user.id,
            role="assistant", content=reply,
            agent_type="accountant",
        ))
        db.session.commit()
        return jsonify({"reply": reply, "tools": tool_trace})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/clear", methods=["POST"])
@login_required
@require_permission("agent.use")
def clear():
    AgentMessage.query.filter_by(
        company_id=g.active_company.id, user_id=current_user.id,
        agent_type="accountant",
    ).delete()
    db.session.commit()
    return jsonify({"ok": True})


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
        reply, _, tool_trace = run_agent_turn(
            messages=messages,
            company_id=g.active_company.id,
            user_id=current_user.id,
            persona=insights_persona(),
            provider=DeepseekProvider(),
            tools=INSIGHTS_TOOL_SCHEMAS,
            execute_tool_fn=execute_insights_tool,
            company_context=company_context,
            max_iters=5,
        )
        db.session.add(AgentMessage(
            company_id=g.active_company.id,
            user_id=current_user.id, role="assistant",
            content=reply, agent_type="insights",
        ))
        db.session.commit()
        return jsonify({"reply": reply, "tools": tool_trace})
    except Exception as e:  # noqa: BLE001
        # A DeepSeek failure MUST NOT affect the accountant.
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
