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


# MARSOUD-CONSENT-AUDIT-LOG (Abdelhamid 2026-07-22) — append-only
# history writer. Called from every acceptance path.
def record_consent(user, *, source, company_id=None,
                    consent_type="terms",
                    document_version=None,
                    request=None):
    """Append a ConsentEvent row. Best-effort — a logging failure
    never blocks the user from proceeding with signup / re-accept."""
    try:
        from app.models import ConsentEvent
        ip = None
        ua = None
        if request is not None:
            ip = request.remote_addr
            ua = (request.headers.get("User-Agent") or "")[:500]
        version = document_version or get_terms_version()
        db.session.add(ConsentEvent(
            user_id=user.id, company_id=company_id,
            consent_type=consent_type,
            document_version=version,
            ip_address=ip, user_agent=ua,
            source=source,
        ))
        # Caller commits — this lets a single request record multiple
        # consent events (terms + privacy) atomically with the rest of
        # the acceptance transaction.
    except Exception:
        from flask import current_app
        try:
            current_app.logger.exception(
                "record_consent failed for user=%s source=%s",
                getattr(user, "id", "?"), source)
        except Exception:
            pass


def users_missing_current_version():
    """MARSOUD-CONSENT-AUDIT-LOG — cross-tenant report used by
    /admin/consent. Users whose LATEST ConsentEvent for `terms` is
    NOT the current published version (or who have none at all).
    Excludes super-admins because they're never nagged by the
    terms middleware."""
    from app.models import User, ConsentEvent
    from sqlalchemy import func
    current = get_terms_version()
    # Latest per-user version (subquery: MAX(created_at) per user).
    latest_subq = db.session.query(
        ConsentEvent.user_id,
        func.max(ConsentEvent.created_at).label("last_at"),
    ).filter(
        ConsentEvent.consent_type == "terms"
    ).group_by(ConsentEvent.user_id).subquery()
    # Join back to get the version at that latest event.
    latest_versions = db.session.query(
        ConsentEvent.user_id, ConsentEvent.document_version
    ).join(
        latest_subq,
        (ConsentEvent.user_id == latest_subq.c.user_id) &
        (ConsentEvent.created_at == latest_subq.c.last_at)
    ).all()
    accepted_current = {uid for uid, v in latest_versions
                        if v == current}
    return User.query.filter(
        User.is_superadmin == False,
        ~User.id.in_(accepted_current),
    ).all()


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
