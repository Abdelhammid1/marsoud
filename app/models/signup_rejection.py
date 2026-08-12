"""MARSOUD-SIGNUP-AUTO-BLOCK (2026-08-12) — one row per
rejected /register POST. Cheap forensics + the fuel for
auto-learning the dynamic blocked_domains list.

Written by `app.services.signup_rejections.record_rejection`
best-effort — a DB hiccup here must never 500 the signup
form.

Reason is enforced at the DB level via CHECK constraint so
a stray typo in a wire-in point surfaces as an IntegrityError
during dev, not as garbage rows in production.
"""
from datetime import datetime
import sqlalchemy as sa
from app import db


class SignupRejection(db.Model):
    __tablename__ = "signup_rejections"
    __table_args__ = (
        db.CheckConstraint(
            "reason IN ('honeypot','rate_limit',"
            "'spam_domain','turnstile','blocked_domain')",
            name="ck_signup_rejection_reason",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    email_domain = db.Column(db.String(120), nullable=False,
                              index=True)
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
