"""MARSOUD-BOT-REGISTRATION-VISIBILITY (2026-08-17) — per-email
blocklist. Sibling of `blocked_domains`.

Why per-email as well as per-domain? Bots frequently use a burner
gmail / outlook address to sign up. Blocking the whole
`gmail.com` domain would lock out every legitimate user. TKT-17
asks for the SAME email to be permanently blocked when it trips
the honeypot — even (especially) on whitelisted domains.

Written by `signup_rejections.block_email(email, reason)` at the
same moment `record_rejection("honeypot", ...)` runs, so every
honeypot trip permanently locks the exact address that submitted
it. Checked by `is_email_blocked(email)` from `auth.register`
BEFORE any bot_guard gate, mirroring `is_domain_blocked`.

`is_active` + `unblocked_at` = soft-toggle so the super-admin can
lift a false positive without losing the audit trail.
"""
from datetime import datetime
import sqlalchemy as sa
from app import db


class BlockedEmail(db.Model):
    __tablename__ = "blocked_emails"

    id = db.Column(db.Integer, primary_key=True)
    # Stored lower-cased so lookups are O(1) case-insensitive.
    email = db.Column(db.String(150), unique=True,
                       nullable=False, index=True)
    blocked_at = db.Column(
        db.DateTime, nullable=False,
        default=datetime.utcnow,
        server_default=sa.func.current_timestamp(),
    )
    reason = db.Column(db.String(200))
    is_active = db.Column(db.Boolean, nullable=False,
                           default=True, index=True)
    unblocked_at = db.Column(db.DateTime)
    unblocked_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id",
                       name="fk_blocked_email_unblocker"),
        nullable=True,
    )
    unblocked_by = db.relationship("User")

    def __repr__(self):
        state = "active" if self.is_active else "lifted"
        return f"<BlockedEmail {self.email!r} {state}>"
