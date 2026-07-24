"""MARSOUD-HELP-CENTER-01 (Abdelhamid 2026-07-24) — user-facing.

Public read routes for the help articles + private image stream.
Admin CRUD lives in app/routes/superadmin.py under /admin/help/.
"""
from flask import Blueprint, render_template, abort, send_file
from flask_login import login_required
from app import db
from app.models import HelpArticle
from app.services.help_media import (
    read_image_path, guess_mimetype, db_first,
    BLUEPRINT_TO_MODULE_KEY,
)

bp = Blueprint("help", __name__)


@bp.route("/")
@login_required
def index():
    """Landing page — every published article, ordered by module."""
    published = HelpArticle.query.filter_by(is_published=True) \
        .order_by(HelpArticle.display_order.asc(),
                   HelpArticle.title_ar.asc()).all()
    # Group by module_key for display. Users expect the same section
    # grouping as the sidebar — but the sidebar dict lives in Jinja,
    # not Python, so we just group alphabetically for now.
    grouped = {}
    for a in published:
        grouped.setdefault(a.module_key, []).append(a)
    return render_template("help/landing.html", grouped=grouped)


@bp.route("/<module_key>")
@login_required
def article(module_key):
    """The most-recent published article for the given module. 404
    (not a server error) when nothing is published for it."""
    a = db_first(module_key)
    if a is None:
        abort(404)
    return render_template("help/article.html", article=a,
                             preview=False)


@bp.route("/media/<int:media_id>")
@login_required
def media(media_id):
    """Stream an uploaded help image inline. Auth-only + nosniff so a
    disguised HTML file can't XSS us via the iframe embed."""
    from app.models import HelpArticleMedia, MEDIA_IMAGE
    m = db.session.get(HelpArticleMedia, media_id)
    if not m or m.type != MEDIA_IMAGE or not m.file_path:
        abort(404)
    p = read_image_path(m.file_path)
    if p is None:
        abort(404)
    resp = send_file(str(p), mimetype=guess_mimetype(m.file_path),
                      as_attachment=False,
                      download_name=m.caption or "help.png")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp
