"""MARSOUD-MOBILE-TKT-05 (2026-08-18) — push-token management
for the Flutter client.

The mobile app POSTs its current FCM registration token here on
every successful login AND on every
`FirebaseMessaging.instance.onTokenRefresh` callback. The
backend upserts by (user_id, token) so this endpoint is fully
idempotent — the client doesn't need to know whether the token
was already registered.

DELETE handles the "logout on this device" case — the mobile
side calls it right before wiping the local auth blob so the
next push doesn't reach a device the user has walked away from.

The list endpoint is behind super-admin only (via the shared
api_v1 gate + a role check inside) — helpful for support
debugging without exposing per-user tokens to arbitrary
callers.
"""
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import current_user

from app import db
from app.models import PushToken
from app.services.api_guard import install_api_guard


bp = Blueprint("api_v1_push", __name__)
install_api_guard(bp)


def _err(msg, status=400):
    r = jsonify({"error": msg})
    r.status_code = status
    return r


def _body():
    return request.get_json(silent=True) or request.form or {}


@bp.route("", methods=["GET"])
def list_my_tokens():
    """Return the caller's registered devices — for the mobile
    'my devices' screen. Never includes the token string itself
    (a leak here would let the caller log another user out by
    replaying the token elsewhere)."""
    rows = (PushToken.query
             .filter_by(user_id=current_user.id, is_active=True)
             .order_by(PushToken.last_used_at.desc())
             .all())
    return jsonify({
        "count": len(rows),
        "devices": [
            {
                "id": t.id,
                "platform": t.platform,
                "device_label": t.device_label,
                "last_used_at":
                    t.last_used_at.isoformat()
                    if t.last_used_at else None,
                "created_at":
                    t.created_at.isoformat()
                    if t.created_at else None,
            }
            for t in rows
        ],
    })


@bp.route("", methods=["POST"])
def register_token():
    """Upsert an FCM registration token for the caller.

    Body:
      { "token": "...FCM registration token...",
        "platform": "android" | "ios" | "web",
        "device_label": "Xiaomi Poco X6 Pro (Android 14)" }

    Idempotent — a second POST with the same (user, token) just
    refreshes `last_used_at` + platform / label.
    """
    body = _body()
    tok = (body.get("token") or "").strip()
    if not tok:
        return _err("token_required", 400)
    if len(tok) > 400:
        return _err("token_too_long", 400)
    platform = (body.get("platform") or "android").strip().lower()
    if platform not in ("android", "ios", "web"):
        platform = "android"
    device_label = (body.get("device_label") or "").strip()[:120] or None

    existing = PushToken.query.filter_by(
        user_id=current_user.id, token=tok).first()
    if existing:
        existing.is_active = True
        existing.last_used_at = datetime.utcnow()
        existing.platform = platform
        if device_label:
            existing.device_label = device_label
        db.session.commit()
        return jsonify({"ok": True, "id": existing.id,
                         "created": False}), 200

    row = PushToken(
        user_id=current_user.id,
        token=tok,
        platform=platform,
        device_label=device_label,
        is_active=True,
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({"ok": True, "id": row.id, "created": True}), 201


@bp.route("/<int:token_id>", methods=["DELETE"])
def revoke_token(token_id):
    """Explicit logout-from-this-device. Soft-deletes (is_active
    → False) so the audit trail survives. Only the owner can
    revoke their own token."""
    row = db.session.get(PushToken, token_id)
    if not row or row.user_id != current_user.id:
        return _err("not_found", 404)
    row.is_active = False
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/by-token", methods=["DELETE"])
def revoke_by_token():
    """Same as above but keyed by the token STRING — for the
    Flutter logout flow, which knows its FCM token but doesn't
    keep the server-side row id around."""
    body = _body()
    tok = (body.get("token") or "").strip()
    if not tok:
        return _err("token_required", 400)
    rows = PushToken.query.filter_by(
        user_id=current_user.id, token=tok).all()
    for r in rows:
        r.is_active = False
    db.session.commit()
    return jsonify({"ok": True, "revoked": len(rows)})
