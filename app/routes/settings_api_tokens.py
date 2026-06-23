"""MARSOUD-API-V1 — token-management UI (/settings/api-tokens).

Owner-only (gated by users.manage). Lists + creates + revokes API
tokens for the logged-in user. Raw token is rendered once at creation
in a flash payload that survives a single redirect, never again.
"""
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, request, g
from flask_login import login_required, current_user

from app import db
from app.models import ApiToken
from app.services.api_tokens import generate_token, revoke_token
from app.services.permissions import require_permission

bp = Blueprint("settings_api_tokens", __name__)


@bp.route("/", methods=["GET"])
@login_required
@require_permission("users.manage")
def index():
    raw_token = request.args.get("show_raw")    # passed once after create
    raw_token_name = request.args.get("name")
    tokens = ApiToken.query.filter_by(user_id=current_user.id).order_by(
        ApiToken.revoked_at.is_(None).desc(),
        ApiToken.created_at.desc(),
    ).all()
    return render_template(
        "settings/api_tokens.html",
        tokens=tokens,
        raw_token=raw_token,
        raw_token_name=raw_token_name,
    )


@bp.route("/new", methods=["POST"])
@login_required
@require_permission("users.manage")
def create():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("اسم المفتاح مطلوب", "error")
        return redirect(url_for("settings_api_tokens.index"))
    try:
        raw, tok = generate_token(current_user, name)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("settings_api_tokens.index"))
    flash(f"تم إنشاء المفتاح '{tok.name}'. انسخه الآن — لن يُعرض مرة أخرى.",
          "success")
    return redirect(url_for(
        "settings_api_tokens.index",
        show_raw=raw, name=tok.name,
    ))


@bp.route("/<int:token_id>/revoke", methods=["POST"])
@login_required
@require_permission("users.manage")
def revoke(token_id):
    tok = db.session.get(ApiToken, token_id)
    if not tok or tok.user_id != current_user.id:
        flash("المفتاح غير موجود", "error")
        return redirect(url_for("settings_api_tokens.index"))
    revoke_token(tok)
    flash(f"تم إلغاء '{tok.name}'", "info")
    return redirect(url_for("settings_api_tokens.index"))
