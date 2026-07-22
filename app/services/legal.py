"""MARSOUD-TERMS-CONSENT (Abdelhamid 2026-07-22).

Thin wrappers over platform_settings for the three legal keys:
  · terms_content_html
  · privacy_content_html
  · terms_version   (e.g. "v1.0")

Content is authored by super-admin via /admin/legal — we treat it
as trusted HTML (super-admin is the only writer). No sanitization
beyond what the admin edits in the form. Not to be exposed to any
untrusted role for editing.
"""
from datetime import datetime
from app import db


DEFAULT_TERMS_VERSION = "v1.0"
DEFAULT_TERMS_HTML = (
    "<p>يرجى من مالك المنصة إضافة نص الشروط والأحكام من "
    "<b>/admin/legal</b>. هذا نص افتراضي حتى يتم النشر الفعلي.</p>"
)
DEFAULT_PRIVACY_HTML = (
    "<p>يرجى من مالك المنصة إضافة نص سياسة الخصوصية من "
    "<b>/admin/legal</b>. هذا نص افتراضي حتى يتم النشر الفعلي.</p>"
)


def _get(key, default):
    try:
        from app.models import PlatformSetting
        row = PlatformSetting.query.filter_by(key=key).first()
        return row.value if row and row.value else default
    except Exception:
        return default


def has_published_legal():
    """True iff the super-admin has actually saved something under
    /admin/legal. When False, the middleware skips the terms nag —
    otherwise every audit fixture + every new install would be
    redirected to /re-accept-terms on the very first request."""
    try:
        from app.models import PlatformSetting
        return PlatformSetting.query.filter_by(
            key="terms_content_html").first() is not None
    except Exception:
        return False


def _set(key, value):
    from app.models import PlatformSetting
    row = PlatformSetting.query.filter_by(key=key).first()
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        db.session.add(PlatformSetting(
            key=key, value=value, updated_at=datetime.utcnow()))


def get_terms_version():
    return _get("terms_version", DEFAULT_TERMS_VERSION).strip() or DEFAULT_TERMS_VERSION


def get_terms_html():
    return _get("terms_content_html", DEFAULT_TERMS_HTML)


def get_privacy_html():
    return _get("privacy_content_html", DEFAULT_PRIVACY_HTML)


def set_legal(version, terms_html, privacy_html):
    _set("terms_version", (version or "").strip() or DEFAULT_TERMS_VERSION)
    _set("terms_content_html", terms_html or "")
    _set("privacy_content_html", privacy_html or "")
