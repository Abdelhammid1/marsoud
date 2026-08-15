"""MARSOUD-MOBILE-FLUTTER — JSON notifications endpoints.

Mounted at /api/v1/notifications. Uses the shared `/api/v1/*` bearer
gate installed by api_v1_bp before_request, so no auth boilerplate here.

Design:
- Every read is scoped by (current_user.id, g.active_company.id) —
  no cross-tenant leakage. Matches `notifications.py:212` (`/notifications/`)
  filtering.
- `/unread-count` is deliberately its own cheap endpoint — mobile polls it
  every 30s for the app-bar badge without pulling the full list.
"""
from flask import Blueprint, jsonify, request, g
from flask_login import current_user

from app import db
from app.models import Notification
from app.services import api_serializers as S
from app.services.api_guard import install_api_guard


bp = Blueprint("api_v1_notifications", __name__)
install_api_guard(bp)


def _err(msg, status=400):
    r = jsonify({"error": msg})
    r.status_code = status
    return r


def _base_query():
    """Own + active-company scope. Never returns notifications for
    another user or another company."""
    return Notification.query.filter(
        Notification.user_id == current_user.id,
        Notification.company_id == g.active_company.id,
    )


@bp.route("", methods=["GET"])
def list_():
    """?unread_only=1 to filter, ?limit=50 (max 200)."""
    unread_only = request.args.get("unread_only", "0") in ("1", "true", "yes")
    try:
        limit = min(200, max(1, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    q = _base_query()
    if unread_only:
        q = q.filter(Notification.read_at.is_(None))
    rows = q.order_by(Notification.created_at.desc()).limit(limit).all()
    return jsonify({
        "count": len(rows),
        "items": [S.notification_brief(n) for n in rows],
    })


@bp.route("/unread-count", methods=["GET"])
def unread_count():
    """Deliberately its own endpoint — cheap poll target for the badge."""
    n = _base_query().filter(Notification.read_at.is_(None)).count()
    return jsonify({"count": n})


@bp.route("/<int:notif_id>/read", methods=["POST"])
def mark_read(notif_id):
    from datetime import datetime
    n = _base_query().filter(Notification.id == notif_id).first()
    if not n:
        return _err("not_found", 404)
    if n.read_at is None:
        n.read_at = datetime.utcnow()
        db.session.commit()
    return jsonify({"ok": True, "notification": S.notification_brief(n)})


@bp.route("/read-all", methods=["POST"])
def mark_all_read():
    from datetime import datetime
    now = datetime.utcnow()
    updated = (_base_query()
               .filter(Notification.read_at.is_(None))
               .update({Notification.read_at: now},
                       synchronize_session=False))
    db.session.commit()
    return jsonify({"ok": True, "updated": updated})
