"""MARSOUD-RICH-TEXT-IMAGE-UPLOAD-01 (2026-09-23) — pasted-image
upload endpoint for the Quill editor.

Before this ticket: when a user Ctrl+V'd an image into a task/lead
description, Quill embedded it as a `data:image/png;base64,...` URI
inline in the HTML.  A 400 KB screenshot became 550 KB of base64
inside the DB, ballooned every subsequent request that fetched the
description, and could not be referenced or cached separately.
Abdelhamid's screenshot showed him pasting Teams screenshots into
a ticket description one after another, exactly the workflow the
base64 path degrades on.

This endpoint accepts one image at a time (multipart `file`) and
returns a URL the editor JS drops back into the document.  The
image lives on disk under `/static/uploads/rich-text/<company>/
<yyyy-mm>/<uuid>.<ext>` — same shape as the other tenant uploads,
scoped by company_id so a tenant switch never leaks another
tenant's images.

Guardrails:
  · Login required + company context required (401 / 400 otherwise).
  · Content-type must start with `image/`.
  · Extension in {jpg, jpeg, png, gif, webp}.
  · Size ≤ 10 MB.
  · Uploaded file NAME never trusts the browser — we use a UUID
    plus the sniffed extension.  No path components make it into
    the on-disk filename.

The sanitizer in `app/services/rich_text.py` allows `<img>` only
when the src is a same-origin `/static/uploads/rich-text/...` path,
so a saved description carries only images that came through this
endpoint.  Base64 img srcs still get stripped.
"""
import os
import uuid
from datetime import datetime
from pathlib import Path

from flask import (
    Blueprint, current_app, g, jsonify, request,
)
from flask_login import login_required


bp = Blueprint("rich_text", __name__)


# 10 MB — big enough for a screenshot with UI chrome, small enough
# to fail loud before we chew disk on a runaway paste.
_MAX_BYTES = 10 * 1024 * 1024

# Sniffed from the mimetype; the browser-supplied filename extension
# is IGNORED.
_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/jpg":  "jpg",
    "image/png":  "png",
    "image/gif":  "gif",
    "image/webp": "webp",
}


def _err(message, code=400):
    return jsonify({"ok": False, "error": message}), code


@bp.route("/rich-text-image", methods=["POST"])
@login_required
def upload_rich_text_image():
    """Save one pasted/attached image and return its URL."""
    if not getattr(g, "active_company", None):
        return _err("no active company", 400)

    file_storage = request.files.get("file")
    if file_storage is None or not file_storage.filename:
        return _err("file required", 400)

    mimetype = (file_storage.mimetype or "").lower().strip()
    ext = _MIME_EXT.get(mimetype)
    if not ext:
        return _err(f"unsupported image type: {mimetype}", 415)

    # Read to a bounded buffer so a lying Content-Length doesn't
    # write hundreds of megabytes to disk before we notice.  A
    # single read then a length check is safe because we're already
    # in memory for typical screenshot sizes; Flask's
    # MAX_CONTENT_LENGTH catches the multi-hundred-MB case earlier.
    data = file_storage.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        return _err("file exceeds 10 MB", 413)
    if not data:
        return _err("empty file", 400)

    now = datetime.utcnow()
    company_id = g.active_company.id
    subdir = f"{now.year:04d}-{now.month:02d}"
    rel_dir = Path("static") / "uploads" / "rich-text" \
                / str(company_id) / subdir
    abs_dir = Path(current_app.root_path) / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)

    fname = f"{uuid.uuid4().hex}.{ext}"
    on_disk = abs_dir / fname
    with open(on_disk, "wb") as fh:
        fh.write(data)

    # Relative URL — the sanitizer's img-src allowlist matches
    # `/static/uploads/rich-text/...` exactly, and the browser
    # resolves it against the current origin.  Absolute URL
    # (SITE_URL) is added only when the caller explicitly asked
    # for it via ?absolute=1 (mobile use case, deferred).
    url = "/" + rel_dir.as_posix() + "/" + fname
    return jsonify({
        "ok": True,
        "url": url,
        "size": len(data),
        "mimetype": mimetype,
    }), 201
