"""MARSOUD-MOBILE-FLUTTER — JSON wrappers for files + support tickets.

Mounted at /api/v1/misc. Uses the shared /api/v1/* bearer + rate-limit
gate installed by app.services.api_guard.install_api_guard.

Two thin resources:

  · /files                    — the caller's own uploaded files
  · /support/tickets          — support tickets in the active company
  · /support/tickets/<id>     — one ticket + its (public) comments
  · /support/tickets/<id>/comments (POST) — add a customer reply

Both are strictly scoped: files by user_id, support tickets by
company_id + created_by_id, matching the web routes.
"""
from datetime import datetime

from flask import Blueprint, jsonify, request, g
from flask_login import current_user

from app import db
from app.models import UserFile
from app.models.support import (
    SupportTicket, SupportTicketComment, STATUS_OPEN, PRIORITY_MEDIUM,
)
from app.services import api_serializers as S
from app.services.api_guard import install_api_guard


bp = Blueprint("api_v1_misc", __name__)
install_api_guard(bp)


def _err(msg, status=400):
    r = jsonify({"error": msg})
    r.status_code = status
    return r


# ─── User files ────────────────────────────────────────────────────
def _file_brief(f):
    if not f:
        return None
    # MARSOUD-MOBILE-FILES-FIX-01 (2026-09-03) — the previous code
    # read `.is_preview_inline` + `.size_human` which don't exist on
    # UserFile. Every /misc/files call 500'd; mobile showed
    # "internal error". Real property names are `.is_previewable` +
    # `.size_display` (see app/models/user_file.py). The response
    # keys stay the mobile-friendly names the client already reads.
    return {
        "id": f.id,
        "filename": f.filename,
        "mimetype": f.mimetype,
        "size_bytes": f.size_bytes,
        "is_preview_inline": f.is_previewable,
        "size_human": f.size_display,
        "created_at": S.iso(f.created_at),
    }


@bp.route("/files", methods=["GET"])
def files_list():
    """Own files only. Web `user_files.index` also renders admin views
    of other users' folders; the mobile app deliberately doesn't."""
    rows = UserFile.query.filter_by(user_id=current_user.id).order_by(
        UserFile.created_at.desc()).limit(200).all()
    return jsonify({
        "count": len(rows),
        "files": [_file_brief(f) for f in rows],
    })


# ─── Support tickets ───────────────────────────────────────────────
def _ticket_brief(t):
    if not t:
        return None
    return {
        "id": t.id,
        "title": t.title,
        "status": t.status,
        "priority": t.priority,
        "created_at": S.iso(t.created_at),
        "updated_at": S.iso(t.updated_at),
        "resolved_at": S.iso(t.resolved_at),
        "comment_count": len(t.comments),
    }


def _ticket_full(t):
    base = _ticket_brief(t) or {}
    base.update({
        "description": t.description,
        "comments": [
            {
                "id": c.id,
                "content": c.content,
                "user_id": c.user_id,
                "user_name": c.user.full_name if c.user else None,
                "created_at": S.iso(c.created_at),
                "is_internal": c.is_internal,
            }
            for c in t.comments
            if not c.is_internal  # customer never sees internal notes
        ],
    })
    return base


@bp.route("/support/tickets", methods=["GET"])
def support_list():
    """Tickets the caller opened in the active company."""
    rows = (SupportTicket.query
            .filter_by(
                company_id=g.active_company.id,
                created_by_id=current_user.id,
            )
            .order_by(SupportTicket.created_at.desc())
            .limit(100).all())
    return jsonify({
        "count": len(rows),
        "tickets": [_ticket_brief(t) for t in rows],
    })


@bp.route("/support/tickets", methods=["POST"])
def support_create():
    body = request.get_json(silent=True) or request.form
    title = (body.get("title") or "").strip()
    description = (body.get("description") or "").strip()
    priority = (body.get("priority") or PRIORITY_MEDIUM).strip()
    if not title or not description:
        return _err("title_and_description_required", 400)
    t = SupportTicket(
        company_id=g.active_company.id,
        created_by_id=current_user.id,
        title=title[:200],
        description=description,
        status=STATUS_OPEN,
        priority=priority,
    )
    db.session.add(t)
    db.session.commit()
    return jsonify({"ok": True, "ticket": _ticket_brief(t)}), 201


@bp.route("/support/tickets/<int:ticket_id>", methods=["GET"])
def support_detail(ticket_id):
    t = SupportTicket.query.filter_by(
        id=ticket_id,
        company_id=g.active_company.id,
        created_by_id=current_user.id,
    ).first()
    if not t:
        return _err("not_found", 404)
    return jsonify({"ticket": _ticket_full(t)})


@bp.route("/support/tickets/<int:ticket_id>/comments", methods=["POST"])
def support_comment(ticket_id):
    t = SupportTicket.query.filter_by(
        id=ticket_id,
        company_id=g.active_company.id,
        created_by_id=current_user.id,
    ).first()
    if not t:
        return _err("not_found", 404)
    body = request.get_json(silent=True) or request.form
    content = (body.get("content") or "").strip()
    if not content:
        return _err("content_required", 400)
    c = SupportTicketComment(
        ticket_id=t.id,
        company_id=g.active_company.id,
        user_id=current_user.id,
        content=content,
        is_internal=False,
    )
    db.session.add(c)
    t.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "ticket": _ticket_full(t)}), 201
