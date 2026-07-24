"""MARSOUD-SUPPORT-TICKETS-01 (Abdelhamid 2026-07-24).

Narrow cross-tenant permission model + attachment storage for the
support tickets feature. This is the ONLY route family in the app
that legitimately reads across companies — every check here is
deliberately strict.
"""
import os
import uuid
from functools import wraps
from pathlib import Path
from flask import abort, current_app
from flask_login import current_user

from app import db


class SupportAttachmentError(Exception):
    """Raised for over-size / unsupported / empty attachment uploads."""


ALLOWED_ATTACHMENT_EXTS = {
    "pdf", "png", "jpg", "jpeg", "gif", "webp", "heic",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "txt", "csv", "zip", "rar", "7z",
}
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024   # 15 MiB — larger than
                                           # help_media because bug
                                           # reports often include
                                           # screenshots or logs.


def _root():
    root = Path(current_app.root_path) / "private_uploads" / "support_attachments"
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_attachment(file_storage, ticket_id):
    """Persist an uploaded file under /support_attachments/<ticket_id>/.
    Returns the storage key (relative to the root) — save it in
    `SupportTicketComment.attachment_url`."""
    if not file_storage or not file_storage.filename:
        return None, None
    ext = _ext(file_storage.filename)
    if ext not in ALLOWED_ATTACHMENT_EXTS:
        raise SupportAttachmentError(
            "صيغة الملف غير مدعومة")
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_ATTACHMENT_BYTES:
        raise SupportAttachmentError(
            f"الملف أكبر من {MAX_ATTACHMENT_BYTES // (1024*1024)} ميجا")
    if size <= 0:
        raise SupportAttachmentError("الملف فارغ")
    dest_dir = _root() / str(int(ticket_id))
    dest_dir.mkdir(parents=True, exist_ok=True)
    key = f"{uuid.uuid4().hex}.{ext}"
    dest = dest_dir / key
    file_storage.save(str(dest))
    return f"{int(ticket_id)}/{key}", file_storage.filename


def read_attachment_path(key):
    """Resolve a storage key to an absolute path; None if invalid."""
    if not key or ".." in key or key.startswith("/") \
            or key.startswith("\\"):
        return None
    p = _root() / key
    return p if p.exists() else None


def _ext(filename):
    return (filename.rsplit(".", 1)[-1] or "").lower() \
        if "." in filename else ""


# ─── Cross-tenant permission ─────────────────────────────────────
def is_support_agent(user=None):
    """True if the current user (a) is a member of Manasty and
    (b) holds `support.manage_tickets` inside Manasty. This is the
    ONE check that bypasses g.active_company scoping."""
    user = user or current_user
    if not user or not getattr(user, "is_authenticated", False):
        return False
    from app.services.manasty import manasty_id
    from app.services.permissions import (
        get_user_role, has_permission,
    )
    from app.models import Company
    manasty = db.session.get(Company, manasty_id())
    if not manasty:
        return False
    role = get_user_role(user.id, manasty.id)
    if role is None:
        return False
    # has_permission normally uses g.active_company; we pass manasty
    # explicitly so this works even when the user is browsing
    # another company at the moment.
    return has_permission("support.manage_tickets",
                            user=user, company=manasty)


def support_agent_required(fn):
    """Decorator for /support-admin/* routes. 403 on failure."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_support_agent():
            abort(403)
        return fn(*args, **kwargs)
    return wrapper
