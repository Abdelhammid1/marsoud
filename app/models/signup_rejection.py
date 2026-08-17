"""MARSOUD-SIGNUP-AUTO-BLOCK (2026-08-12) — one row per
rejected /register POST. Cheap forensics + the fuel for
auto-learning the dynamic blocked_domains list.

Written by `app.services.signup_rejections.record_rejection`
best-effort — a DB hiccup here must never 500 the signup
form.

Reason is enforced at the DB level via CHECK constraint so
a stray typo in a wire-in point surfaces as an IntegrityError
during dev, not as garbage rows in production.

MARSOUD-BOT-REGISTRATION-VISIBILITY (2026-08-17) — TKT-17.
Added `email` (full email, not just domain) and
`honeypot_value` (the string the bot typed into the hidden
`website` field). Both nullable to keep backward compat with
rows written before this ticket, and to keep the write
best-effort. `reason` CHECK now also allows the new
`"bot_immediate"` value the honeypot path emits when it
triggers an immediate domain-block on a non-whitelisted
domain.
"""
from datetime import datetime
import sqlalchemy as sa
from app import db


class SignupRejection(db.Model):
    __tablename__ = "signup_rejections"
    __table_args__ = (
        db.CheckConstraint(
            "reason IN ('honeypot','rate_limit',"
            "'spam_domain','turnstile','blocked_domain',"
            # MARSOUD-BOT-REGISTRATION-VISIBILITY — new value
            # for the immediate-block branch. The DB constraint
            # is updated in migration
            # m0n3o6p9q2r5_bot_registration_visibility.py.
            "'blocked_email','bot_immediate')",
            name="ck_signup_rejection_reason",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    email_domain = db.Column(db.String(120), nullable=False,
                              index=True)
    # MARSOUD-BOT-REGISTRATION-VISIBILITY — persist the full
    # email (not just the domain) so the super-admin can act
    # on a specific address and so `is_email_blocked` has
    # something to key on.
    email = db.Column(db.String(150), nullable=True, index=True)
    # MARSOUD-BOT-REGISTRATION-VISIBILITY — the raw string the
    # bot typed into the hidden `website` honeypot. Free-form
    # Text so a bot pasting a full URL doesn't get truncated.
    honeypot_value = db.Column(db.Text, nullable=True)
    reason = db.Column(db.String(24), nullable=False, index=True)
    ip_address = db.Column(db.String(45))
    created_at = db.Column(
        db.DateTime, nullable=False,
        default=datetime.utcnow,
        server_default=sa.func.current_timestamp(),
        index=True,
    )

    def __repr__(self):
        return (f"<SignupRejection id={self.id} "
                f"domain={self.email_domain!r} "
                f"reason={self.reason}>")
