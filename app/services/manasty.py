"""MARSOUD-PUBLIC-CONTACT-FORM-01 (Abdelhamid 2026-07-24).

Single source of truth for "which company is Manasty in this
deployment?" Every route/service that needs to write into or gate
on Manasty imports from here — no hardcoded 8 scattered.
"""
from flask import current_app


def manasty_id():
    """Return the company id that represents Manasty in this
    deployment. Defaults to 8 (Abdelhamid's DB) but overridable via
    MANASTY_COMPANY_ID env var."""
    return int(current_app.config.get("MANASTY_COMPANY_ID", 8))


def manasty_owner_ids():
    """User ids of every owner/admin of Manasty. Used as the fallback
    inbox for the public contact-lead endpoint and the recipient list
    for its notifications. Returns [] when Manasty has none — the
    caller should treat that as "drop the notification, but don't
    crash the request"."""
    from app import db
    from app.models.user import user_companies
    from sqlalchemy import text
    rows = db.session.execute(text(
        "SELECT user_id FROM user_companies "
        "WHERE company_id = :c AND role IN ('owner', 'admin') "
        "ORDER BY (CASE WHEN role='owner' THEN 0 ELSE 1 END), user_id"
    ), {"c": manasty_id()}).fetchall()
    return [r[0] for r in rows]


def manasty_default_assignee_id():
    """The user id every new Lead should be assigned to. Prefers the
    explicit SUPPORT_INBOX_USER_ID env var, then any owner of Manasty,
    then any admin, else None (Lead schema requires this so the
    caller must handle None by rejecting or picking a system fallback)."""
    explicit = current_app.config.get("SUPPORT_INBOX_USER_ID")
    if explicit:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    ids = manasty_owner_ids()
    return ids[0] if ids else None
