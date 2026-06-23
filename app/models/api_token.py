"""MARSOUD-API-V1 — ApiToken model.

One row per active or revoked bearer token. The raw token is never
stored; only its SHA-256 hash + a short visible prefix for UI display.
"""
from datetime import datetime
from app import db


class ApiToken(db.Model):
    __tablename__ = "api_tokens"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name = db.Column(db.String(120), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False,
                           unique=True, index=True)
    token_prefix = db.Column(db.String(20), nullable=False)
    scopes = db.Column(db.Text, nullable=False, default="*")
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)
    last_used_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", backref=db.backref(
        "api_tokens",
        order_by="ApiToken.created_at.desc()",
        cascade="all, delete-orphan",
    ))

    @property
    def is_active(self):
        return self.revoked_at is None
