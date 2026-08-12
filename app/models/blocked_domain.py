"""MARSOUD-SIGNUP-AUTO-BLOCK (2026-08-12) — dynamically
learned blocklist for signup email domains.

A row here means every future signup POST from that email
domain is intercepted BEFORE any other bot_guard gate runs
(honeypot, rate-limit, turnstile all skipped). The pre-gate
check saves Cloudflare API calls on the known-bad path and
keeps the rate-limit bucket clean for the good users.

`is_active` + `unblocked_at` (soft-toggle, no DELETE)
preserves the full history of every auto-block ever made —
cheap forensic value; the table stays small anyway (one
row per unique bad domain).

The WHITELISTED_DOMAINS frozen set in
`app.services.signup_rejections` HARD-EXEMPTS the major
free-email providers from ever landing here, so a bot
using a burner gmail account cannot lock out every
legitimate gmail user.
"""
from datetime import datetime
import sqlalchemy as sa
from app import db


class BlockedDomain(db.Model):
    __tablename__ = "blocked_domains"

    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(120), unique=True,
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
                       name="fk_blocked_domain_unblocker"),
        nullable=True,
    )
    unblocked_by = db.relationship("User")

    def __repr__(self):
        state = "active" if self.is_active else "lifted"
        return (f"<BlockedDomain {self.domain!r} {state}>")
