"""MARSOUD-SMTP-PER-COMPANY-01 (2026-10-18) — per-tenant SMTP config.

One row per company (UNIQUE on `company_id`).  When present AND
`is_enabled`, the central `send_email()` service uses this config
instead of the global `.env` SMTP settings.  Absent → falls back to
the global default; present-but-failing → NO silent fallback (the
operator is expected to notice + fix, not have the mail leave from
`no-reply@marsoud.app` without warning).

`password_encrypted` holds the raw Fernet ciphertext from
`app.services.crypto.encrypt`; it is never decrypted onto the ORM
object (callers go through `crypto.decrypt` + `.password_encrypted`
at the exact send-site).  This keeps a stray `repr()` or template
render from spilling the plaintext.
"""
from datetime import datetime

from app import db


# Allowed values for `encryption`.  Kept module-level so callers can
# use the names rather than magic strings.
SMTP_ENC_STARTTLS = "STARTTLS"
SMTP_ENC_SSL = "SSL"
SMTP_ENC_NONE = "NONE"
SMTP_ENC_CHOICES = (SMTP_ENC_STARTTLS, SMTP_ENC_SSL, SMTP_ENC_NONE)


class CompanySMTP(db.Model):
    __tablename__ = "company_smtp"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(
        db.Integer, db.ForeignKey("companies.id"),
        nullable=False, unique=True, index=True,
    )

    host = db.Column(db.String(255), nullable=False)
    port = db.Column(db.Integer, nullable=False)
    encryption = db.Column(
        db.String(16), nullable=False, default=SMTP_ENC_STARTTLS,
    )
    username = db.Column(db.String(255), nullable=True)
    password_encrypted = db.Column(db.LargeBinary, nullable=True)

    from_email = db.Column(db.String(255), nullable=False)
    from_name = db.Column(db.String(255), nullable=True)

    is_enabled = db.Column(db.Boolean, nullable=False, default=True)

    # Audit trail for the test-connection button.  Kept on the row
    # (not a separate table) because it's only ever one most-recent
    # entry — operators don't need a full test history.
    last_test_at = db.Column(db.DateTime, nullable=True)
    last_test_ok = db.Column(db.Boolean, nullable=True)
    last_test_error = db.Column(db.Text, nullable=True)

    # MARSOUD-SMTP-PER-COMPANY-01 — audit review follow-up for
    # ticket AC #8 ("خطأ ... وإظهاره للمستخدم المخوّل"): track the
    # last live-send attempt so the operator sees failures in the
    # UI, not just in logs.  Updated by `send_email` when the resolver
    # picked this row (source == "company").
    last_send_at = db.Column(db.DateTime, nullable=True)
    last_send_ok = db.Column(db.Boolean, nullable=True)
    last_send_error = db.Column(db.Text, nullable=True)

    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow,
        onupdate=datetime.utcnow, nullable=False)

    company = db.relationship(
        "Company",
        backref=db.backref("smtp_config", uselist=False),
    )

    def __repr__(self):
        # NEVER include host:port in the default repr — a stray log
        # of the model (e.g. SQLAlchemy session-identity dump) must
        # not leak which tenant uses which provider.
        return f"<CompanySMTP company_id={self.company_id}>"
