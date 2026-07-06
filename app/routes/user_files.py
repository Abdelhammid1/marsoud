"""MARSOUD-USER-FILES — private per-user folder with in-app preview.

Routes:
  GET  /files/                 — my folder (list + upload form)
  POST /files/upload           — upload a file to my folder
  GET  /files/<id>             — detail page: preview + download link
  GET  /files/<id>/raw         — stream bytes inline (gated)
  GET  /files/<id>/download    — stream bytes as attachment (gated)
  POST /files/<id>/delete      — delete (owner only)
  GET  /files/user/<user_id>/  — admin: list another user's folder
                                    (requires users.view)

Access rules (enforced in _get_or_403):
  - Owner can read + delete their own files.
  - Users with permission `users.view` (owners/admins) can read any
    file in their company. They cannot delete other people's files.
"""
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    send_file, g, abort,
)
from flask_login import login_required, current_user

from app import db
from app.models import UserFile, User, Employee
from app.services.permissions import has_permission
from app.services.user_files import (
    save_user_file, delete_user_file, resolve_disk_path,
    files_for_user, used_quota_bytes,
    UserFileError, MAX_BYTES, MAX_USER_QUOTA_BYTES,
)

bp = Blueprint("user_files", __name__)


# ─── Helpers ────────────────────────────────────────────────────────────
def _get_or_403(file_id: int, *, need_write: bool = False) -> UserFile:
    """Fetch a UserFile, 404 if missing, 403 if the current user can't
    read (or write, when need_write) it. Consolidates the three-way
    check (own / admin-read / admin-write) so route bodies stay flat."""
    f = db.session.get(UserFile, file_id)
    if not f or f.company_id != g.active_company.id:
        abort(404)
    is_owner = (f.user_id == current_user.id)
    if need_write:
        # Only the owner may mutate (delete). Admin read-through does
        # NOT grant delete rights — that's a separate policy decision
        # and keeping it strict here means an admin can't nuke a
        # user's files by accident.
        if not is_owner:
            abort(403)
        return f
    if is_owner:
        return f
    if has_permission("users.view"):
        return f
    abort(403)


def _list_context_for(target_user: User):
    """Bundle the render-template kwargs shared between the /files/
    (own) and /files/user/<id>/ (admin) list views."""
    cid = g.active_company.id
    files = files_for_user(company_id=cid, user_id=target_user.id)
    # Show employee display name if the target user has an Employee
    # row in this company — otherwise fall back to full_name.
    emp = Employee.query.filter_by(
        company_id=cid, user_id=target_user.id,
    ).first()
    header_name = (emp.full_name if emp else target_user.full_name) \
        or target_user.email
    used = used_quota_bytes(company_id=cid, user_id=target_user.id)
    return {
        "files": files,
        "target_user": target_user,
        "header_name": header_name,
        "is_own_folder": target_user.id == current_user.id,
        "max_bytes": MAX_BYTES,
        # MARSOUD-USER-FILES-QUOTA — surface consumption so the user
        # sees "X of 500 MB" and knows when they're about to hit the
        # aggregate cap.
        "quota_used_bytes": used,
        "quota_max_bytes": MAX_USER_QUOTA_BYTES,
        "quota_pct": min(100, int(used * 100 / MAX_USER_QUOTA_BYTES))
                     if MAX_USER_QUOTA_BYTES else 0,
    }


# ─── Own folder ─────────────────────────────────────────────────────────
@bp.route("/")
@login_required
def index():
    return render_template(
        "user_files/index.html",
        **_list_context_for(current_user),
    )


@bp.route("/upload", methods=["POST"])
@login_required
def upload():
    fs = request.files.get("file")
    try:
        save_user_file(
            company_id=g.active_company.id,
            user_id=current_user.id,
            file_storage=fs,
        )
        flash("تم رفع الملف", "success")
    except UserFileError as e:
        flash(str(e), "error")
    return redirect(url_for("user_files.index"))


@bp.route("/<int:file_id>")
@login_required
def detail(file_id):
    f = _get_or_403(file_id)
    return render_template("user_files/detail.html", f=f,
                             is_owner=(f.user_id == current_user.id))


def _harden(resp):
    """MARSOUD-USER-FILES-SEC — attach the security headers we want
    on any user-uploaded byte stream:
      · X-Content-Type-Options: nosniff — forbid browsers from
        overriding the mimetype we declared (defends against an
        HTML file uploaded as .pdf being sniffed + rendered as
        HTML inside our iframe, where JS would have same-origin
        access).
      · Content-Security-Policy: sandbox — belt-and-braces on top
        of the iframe sandbox in the detail template. If the file
        is ever fetched via a route that bypasses that iframe,
        scripts/forms/plugins are still disabled at the response
        layer.
    """
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp


@bp.route("/<int:file_id>/raw")
@login_required
def raw(file_id):
    """Inline stream — used by the <img> / <iframe> preview embeds
    on the detail page. Sets Content-Disposition to inline so the
    browser renders the file rather than triggering a download."""
    f = _get_or_403(file_id)
    disk = resolve_disk_path(f)
    if not disk.exists():
        abort(404)
    resp = send_file(str(disk),
                      mimetype=f.mimetype or "application/octet-stream",
                      as_attachment=False, download_name=f.name)
    return _harden(resp)


@bp.route("/<int:file_id>/download")
@login_required
def download(file_id):
    """Force-download variant of /raw — same auth gate. Kept as a
    separate URL so the detail-page button reads naturally in server
    logs."""
    f = _get_or_403(file_id)
    disk = resolve_disk_path(f)
    if not disk.exists():
        abort(404)
    resp = send_file(str(disk),
                      mimetype=f.mimetype or "application/octet-stream",
                      as_attachment=True, download_name=f.name)
    return _harden(resp)


@bp.route("/<int:file_id>/delete", methods=["POST"])
@login_required
def delete(file_id):
    f = _get_or_403(file_id, need_write=True)
    delete_user_file(f)
    flash("تم حذف الملف", "success")
    # Admins deleting from another user's folder aren't allowed, so
    # this always returns to the owner's own folder.
    return redirect(url_for("user_files.index"))


# ─── Admin: view any user's folder in the company ──────────────────────
@bp.route("/user/<int:user_id>/")
@login_required
def user_folder(user_id):
    if user_id == current_user.id:
        return redirect(url_for("user_files.index"))
    if not has_permission("users.view"):
        abort(403)
    target = db.session.get(User, user_id)
    if not target:
        abort(404)
    # Confirm the target belongs to the active company (multi-tenant
    # safety net — user_id alone doesn't scope by company).
    from app.models import user_companies as uc_table
    joined = db.session.execute(db.select(uc_table).where(
        uc_table.c.user_id == user_id,
        uc_table.c.company_id == g.active_company.id,
    )).first()
    if not joined:
        abort(404)
    return render_template(
        "user_files/index.html",
        **_list_context_for(target),
    )
