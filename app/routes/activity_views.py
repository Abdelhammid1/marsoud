"""MARSOUD-ACTLOG-01 — read-only activity + session views.

Two blueprints share most of the rendering logic via a single helper:
  /admin/activity      super-admin, cross-company
  /settings/activity   owner of the active company, own-company only
"""
from datetime import datetime, timedelta

from flask import (
    Blueprint, render_template, request, g, redirect, url_for, flash,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    UserActivityLog, UserSession, User, Company, ACTION_TYPES,
)
from app.services.permissions import require_permission
from app.services.superadmin import superadmin_required


# Both blueprints share names that the templates can url_for against.
admin_activity_bp = Blueprint("admin_activity", __name__)
settings_activity_bp = Blueprint("settings_activity", __name__)


# ─── Shared filter parsing ──────────────────────────────────────────────
def _parse_filters():
    """Extract filter args from the request — used by both pages."""
    f = {
        "user_id": request.args.get("user_id", type=int),
        "action": (request.args.get("action") or "").strip().upper() or None,
        "entity_type": (request.args.get("entity_type") or "").strip()
                        or None,
        "device_type": (request.args.get("device_type") or "").strip().upper()
                        or None,
        "range": (request.args.get("range") or "7d").lower(),
        "from": (request.args.get("from") or "").strip() or None,
        "to": (request.args.get("to") or "").strip() or None,
    }
    # Translate quick range to (from, to)
    now = datetime.utcnow()
    if not f["from"] and not f["to"]:
        deltas = {"today": timedelta(days=1), "7d": timedelta(days=7),
                  "30d": timedelta(days=30), "90d": timedelta(days=90)}
        d = deltas.get(f["range"], timedelta(days=7))
        f["_start"] = now - d
        f["_end"] = now
    else:
        try:
            f["_start"] = (datetime.fromisoformat(f["from"])
                            if f["from"] else now - timedelta(days=7))
        except ValueError:
            f["_start"] = now - timedelta(days=7)
        try:
            f["_end"] = (datetime.fromisoformat(f["to"]) if f["to"] else now)
        except ValueError:
            f["_end"] = now
    return f


def _apply_filters(q_activity, q_sessions, f, *, company_scope=None):
    """Apply filter dict to BOTH the activity + sessions queries.
    company_scope=None means no company filter (super-admin)."""
    if company_scope is not None:
        q_activity = q_activity.filter(
            UserActivityLog.company_id == company_scope)
        q_sessions = q_sessions.filter(
            UserSession.company_id == company_scope)
    if f["user_id"]:
        q_activity = q_activity.filter(
            UserActivityLog.user_id == f["user_id"])
        q_sessions = q_sessions.filter(
            UserSession.user_id == f["user_id"])
    if f["action"]:
        q_activity = q_activity.filter(
            UserActivityLog.action_type == f["action"])
    if f["entity_type"]:
        q_activity = q_activity.filter(
            UserActivityLog.entity_type == f["entity_type"])
    if f["device_type"]:
        q_activity = q_activity.filter(
            UserActivityLog.device_type == f["device_type"])
        q_sessions = q_sessions.filter(
            UserSession.device_type == f["device_type"])
    if f.get("_start"):
        q_activity = q_activity.filter(
            UserActivityLog.created_at >= f["_start"])
        q_sessions = q_sessions.filter(
            UserSession.login_at >= f["_start"])
    if f.get("_end"):
        q_activity = q_activity.filter(
            UserActivityLog.created_at <= f["_end"])
        q_sessions = q_sessions.filter(
            UserSession.login_at <= f["_end"])
    return q_activity, q_sessions


def _render_activity_page(template, *, company_scope=None):
    f = _parse_filters()
    activity_q = UserActivityLog.query
    sessions_q = UserSession.query
    activity_q, sessions_q = _apply_filters(
        activity_q, sessions_q, f, company_scope=company_scope,
    )
    activities = activity_q.order_by(
        UserActivityLog.created_at.desc()
    ).limit(500).all()
    sessions = sessions_q.order_by(UserSession.login_at.desc()).limit(200).all()
    # Build the user filter dropdown
    if company_scope is not None:
        from app.models.user import user_companies
        rows = db.session.execute(
            user_companies.select().where(
                user_companies.c.company_id == company_scope,
            )
        ).fetchall()
        member_ids = {r.user_id for r in rows}
        all_users = User.query.filter(User.id.in_(member_ids)).order_by(
            User.full_name).all()
    else:
        all_users = User.query.order_by(User.full_name).all()
    # Action + entity dropdown sources
    all_actions = list(ACTION_TYPES)
    entity_types = sorted({a.entity_type for a in activities
                            if a.entity_type})
    return render_template(
        template,
        activities=activities, sessions=sessions,
        users=all_users, actions=all_actions, entity_types=entity_types,
        filters=f,
    )


# ─── Super-admin: cross-company ─────────────────────────────────────────
@admin_activity_bp.route("/")
@login_required
@superadmin_required
def index():
    return _render_activity_page("admin/activity.html", company_scope=None)


# ─── Owner: own-company only ────────────────────────────────────────────
def _is_owner_of_active_company():
    from app.models.user import user_companies
    if not g.get("active_company"):
        return False
    row = db.session.execute(
        user_companies.select().where(
            (user_companies.c.user_id == current_user.id) &
            (user_companies.c.company_id == g.active_company.id) &
            (user_companies.c.role == "owner")
        )
    ).first()
    return row is not None


@settings_activity_bp.route("/")
@login_required
def index():
    if not _is_owner_of_active_company():
        flash("هذه الصفحة للمالك فقط", "error")
        return redirect(url_for("dashboard.index"))
    return _render_activity_page(
        "settings/activity.html",
        company_scope=g.active_company.id,
    )


# ─── Super-admin toggle for VIEW logging ────────────────────────────────
@admin_activity_bp.route("/toggle-view-logging", methods=["POST"])
@login_required
@superadmin_required
def toggle_view_logging():
    from app.services.activity import (
        view_logging_enabled, set_view_logging_enabled,
    )
    new_val = not view_logging_enabled()
    set_view_logging_enabled(new_val)
    flash(
        f"تسجيل صفحات VIEW: {'مفعّل' if new_val else 'معطّل'}",
        "success",
    )
    return redirect(url_for("admin_activity.index"))
