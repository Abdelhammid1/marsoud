"""MARSOUD-HELP-CENTER-01 (Abdelhamid 2026-07-24) — service layer.

  · Private image storage (mirror of user_files.py pattern).
  · YouTube / Vimeo URL parsing.
  · Blueprint → module_key mapping for the contextual "?" icon.
"""
import mimetypes
import os
import re
import uuid
from pathlib import Path
from flask import current_app


class HelpMediaError(Exception):
    """User-visible error raised by the admin CRUD path (bad URL,
    unsupported file type, over-size)."""


# ─── Storage ─────────────────────────────────────────────────────
ALLOWED_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "svg"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024   # 5 MiB per help image


def _root():
    """Absolute path to the private help-media directory. Lazily
    creates the tree on first access."""
    root = Path(current_app.root_path) / "private_uploads" / "help_media"
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_image(file_storage):
    """Persist a Werkzeug FileStorage as a private help image.
    Returns the opaque storage key (a UUID-prefixed filename)."""
    if not file_storage or not file_storage.filename:
        raise HelpMediaError("لم يُرفع أي ملف")
    ext = _ext(file_storage.filename)
    if ext not in ALLOWED_IMAGE_EXTS:
        raise HelpMediaError(
            "صيغة غير مدعومة. المسموح: PNG / JPG / GIF / WebP / SVG")
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_IMAGE_BYTES:
        raise HelpMediaError(
            f"الصورة أكبر من {MAX_IMAGE_BYTES // (1024*1024)} ميجا")
    if size <= 0:
        raise HelpMediaError("الملف فارغ")
    key = f"{uuid.uuid4().hex}.{ext}"
    dest = _root() / key
    file_storage.save(str(dest))
    return key


def read_image_path(key):
    """Resolve a storage key to an absolute path; 404 if missing."""
    if not key or "/" in key or "\\" in key or key.startswith("."):
        return None
    p = _root() / key
    return p if p.exists() else None


def _ext(filename):
    return (filename.rsplit(".", 1)[-1] or "").lower() \
        if "." in filename else ""


def guess_mimetype(key):
    return mimetypes.guess_type(key or "")[0] or "application/octet-stream"


# ─── Video URL extraction ────────────────────────────────────────
_YT = re.compile(
    r'(?:youtube\.com/(?:watch\?v=|embed/|v/)|youtu\.be/)([A-Za-z0-9_-]{11})'
)
_VM = re.compile(r'vimeo\.com/(?:video/)?(\d+)')


def extract_video(url):
    """Return ("YOUTUBE"|"VIMEO", video_id) or None for a bad URL."""
    if not url:
        return None
    m = _YT.search(url)
    if m:
        return ("YOUTUBE", m.group(1))
    m = _VM.search(url)
    if m:
        return ("VIMEO", m.group(1))
    return None


# ─── Blueprint → module_key mapping ──────────────────────────────
# Maps a Flask blueprint name (i.e. `request.endpoint.split('.')[0]`)
# to the module_key used by help articles. The contextual "?" icon
# in the header uses this to know which article to open. Blueprints
# not listed here have no matching help article — the "?" icon
# hides itself on those pages.
BLUEPRINT_TO_MODULE_KEY = {
    "dashboard":         "dashboard",
    "invoices":          "invoices",
    "customers":         "customers",
    "vendors":           "vendors",
    "vendor_bills":      "vendor_bills",
    "recurring_bills":   "recurring_bills",
    "products":          "products",
    "inventory":         "inventory",
    "pos":               "pos",
    "accounts":          "accounts",
    "journals":          "journals",
    "reports":           "reports",
    "party_ledger":      "party_ledger",
    "payroll":           "payroll",
    "hr":                "hr",
    "hr_ss":             "hr",
    # MARSOUD-ADVANCES — advances are part of the payroll story, so the
    # "?" icon on /advances/* points at the payroll article until one is
    # authored for advances specifically.
    "advances":          "payroll",
    "leads":             "crm",
    "crm":               "crm",
    "tasks":             "tasks",
    "projects":          "projects",
    "calendar":          "calendar",
    "assets":            "assets",
    "manufacturing":     "manufacturing",
    "refunds":           "refunds",
    "evaluations":       "evaluations",
    "agent":             "agent",
    "settings_roles":    "settings_roles",
    "settings_api_tokens": "settings_api_tokens",
    "settings_activity": "settings_activity",
    "settings_backup":   "settings_backup",
    "settings_usage":    "settings_usage",
    "user_files":        "user_files",
    "companies":         "companies",
    "portal_emp":        "attendance",
    "support":           "support",
}


def current_module_key():
    """The module_key for the current request's endpoint, or None."""
    from flask import request
    if not request or not request.endpoint:
        return None
    # MARSOUD-HELP-VIOLATIONS-01 — violation-policy views live inside
    # the "hr" blueprint, but deserve their own help article rather
    # than surfacing the general HR one.
    if request.endpoint.startswith("hr.violation"):
        return "violations"
    bp = request.endpoint.split(".", 1)[0]
    return BLUEPRINT_TO_MODULE_KEY.get(bp)


def has_published_article(module_key):
    """Cheap lookup used by the header context processor to decide
    whether to render the "?" icon."""
    from app.models import HelpArticle
    if not module_key:
        return False
    return db_first(module_key) is not None


def db_first(module_key):
    from app.models import HelpArticle
    return HelpArticle.query.filter_by(
        module_key=module_key, is_published=True
    ).order_by(HelpArticle.display_order.asc(),
                HelpArticle.id.desc()).first()
