"""MARSOUD-AI-ACTION-FRAMEWORK-01 (2026-09-03) — HTTP endpoints.

`POST /api/ai-actions/propose`        — validate + persist PENDING.
`POST /api/ai-actions/<id>/confirm`   — freshness check + execute.

Both endpoints are JSON in / JSON out. Both require login and rely
on `g.active_company` for tenancy isolation. Per-action permission
gates live inside the action spec, not at the route level, so a
Phase-1 ticket adding `add_vendor_bill` can require
`vendor_bills.create` without touching this file.
"""
from flask import Blueprint, jsonify, request, g
from flask_login import login_required, current_user

from app.services.ai_actions import (
    AiActionError, propose as _propose, confirm as _confirm,
)


bp = Blueprint("ai_actions", __name__)


@bp.route("/propose", methods=["POST"])
@login_required
def propose():
    body = request.get_json(silent=True) or {}
    action_type = (body.get("action_type") or "").strip()
    payload = body.get("payload") or {}
    if not action_type:
        return jsonify({"error": "action_type مطلوب"}), 400
    try:
        result = _propose(
            action_type=action_type,
            payload=payload,
            company_id=g.active_company.id,
            user_id=current_user.id,
        )
    except AiActionError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result), 201


@bp.route("/<int:intent_id>/confirm", methods=["POST"])
@login_required
def confirm(intent_id):
    result, status = _confirm(
        intent_id=intent_id,
        actor_user_id=current_user.id,
        active_company_id=g.active_company.id,
    )
    return jsonify(result), status
